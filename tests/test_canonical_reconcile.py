"""Tests for canonical_key reconciliation against the GHRA registry."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from bridge_db import config
from bridge_db.canonical_reconcile import reconcile_canonical_keys


def _registry(tmp_path: Path) -> Path:
    reg = tmp_path / "project-registry.json"
    reg.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "canonical_key": "operant-public",
                        "display_name": "operant-public",
                        "repo_full_name": "saagpatel/operant",
                        "bridge_project_names": ["OPERANT", "operant-public"],
                        "aliases": ["bridge:OPERANT", "notion:OPERANT"],
                    },
                    {
                        "canonical_key": "portfolio-health",
                        "display_name": "portfolio-health",
                        "repo_full_name": "saagpatel/portfolio-code-health",
                        "bridge_project_names": [
                            "portfolio-code-health",
                            "portfolio-health",
                        ],
                        "aliases": ["bridge:portfolio-code-health"],
                    },
                ],
                "resolution_overrides": {
                    "OPERANT": "saagpatel/operant",
                    "portfolio-code-health": "portfolio-health",
                },
            }
        ),
        encoding="utf-8",
    )
    return reg


@pytest.mark.asyncio
async def test_reconcile_canonical_keys_corrects_disagreements_and_nulls_unmatched(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "PROJECT_REGISTRY_PATH", _registry(tmp_path))
    await db.execute(
        """
        INSERT INTO activity_log (source, timestamp, project_name, summary, canonical_key)
        VALUES
            ('cc', '2026-07-03', 'OPERANT', 'old slug', 'operant-public'),
            ('cc', '2026-07-03', 'unknown-project', 'bad slug', 'made-up-slug')
        """
    )
    await db.execute(
        """
        INSERT INTO pending_handoffs (project_name, canonical_key)
        VALUES ('portfolio-code-health', 'portfolio-health')
        """
    )
    await db.commit()

    result = await reconcile_canonical_keys(db, audit=False)

    assert result.registry_present is True
    assert result.rows_checked == 3
    assert result.rows_updated == 3
    assert result.disagreements_resolved == 2
    assert result.unresolvable_rows == 1
    assert result.unresolvable_nulled == 1

    activity = await db.execute(
        "SELECT project_name, canonical_key FROM activity_log ORDER BY id"
    )
    activity_rows = await activity.fetchall()
    assert [tuple(row) for row in activity_rows] == [
        ("OPERANT", "saagpatel/operant"),
        ("unknown-project", None),
    ]

    handoff = await db.execute(
        "SELECT project_name, canonical_key FROM pending_handoffs"
    )
    handoff_row = await handoff.fetchone()
    assert handoff_row is not None
    assert tuple(handoff_row) == (
        "portfolio-code-health",
        "saagpatel/portfolio-code-health",
    )


@pytest.mark.asyncio
async def test_reconcile_canonical_keys_noops_when_registry_absent(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "PROJECT_REGISTRY_PATH", tmp_path / "missing.json")
    await db.execute(
        """
        INSERT INTO activity_log (source, timestamp, project_name, summary, canonical_key)
        VALUES ('cc', '2026-07-03', 'OPERANT', 'old slug', 'operant-public')
        """
    )
    await db.commit()

    result = await reconcile_canonical_keys(db, audit=False)

    assert result.registry_present is False
    assert result.rows_updated == 0
    row = await (await db.execute("SELECT canonical_key FROM activity_log")).fetchone()
    assert row is not None
    assert row["canonical_key"] == "operant-public"
