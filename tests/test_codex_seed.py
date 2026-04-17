"""Tests for the private Codex baseline seed entrypoint."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bridge_db import config
from bridge_db.codex_seed import apply_manifest, load_manifest
from bridge_db.db import open_db


def make_manifest() -> dict[str, object]:
    snapshot_payload = {
        "infrastructure": "- Automations: 17 active",
        "automation_digest": "- Runtime health: healthy",
        "active_projects": "- ResumeEvolver",
    }
    return {
        "fingerprint": "2f7765f0a535ffce7f64a314294f5bc3eb0f4c6452860ea06073dd9406f25d0a",
        "snapshot_date": "2026-04-14",
        "snapshot_payload": snapshot_payload,
        "baseline_activity": {
            "caller": "codex",
            "timestamp": "2026-04-14",
            "project_name": "bridge-baseline-seed",
            "summary": "Seeded Codex baseline from reconciled truth.",
            "tags": ["BASELINE", "CODEX-STATE", "TRUTH-RECONCILED"],
        },
    }


def make_variant_manifest() -> dict[str, object]:
    manifest = make_manifest()
    manifest["snapshot_payload"] = {
        "infrastructure": "- Automations: 17 active\n- MCP servers: 13 connected",
        "automation_digest": "- Runtime health: healthy",
        "active_projects": "- ResumeEvolver",
    }
    manifest["baseline_activity"] = {
        "caller": "codex",
        "timestamp": "2026-04-14",
        "project_name": "bridge-baseline-seed",
        "summary": "Seeded Codex baseline from corrected reconciled truth.",
        "tags": ["BASELINE", "CODEX-STATE", "TRUTH-RECONCILED"],
    }
    return manifest


def test_load_manifest_requires_keys(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"fingerprint": "x"}), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest missing required keys"):
        load_manifest(path)


def test_load_manifest_rejects_mismatched_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    manifest = make_manifest()
    manifest["fingerprint"] = "wrong"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint does not match"):
        load_manifest(path)


@pytest.mark.asyncio
async def test_codex_seed_dry_run_reports_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "bridge.db"
    bridge_path = tmp_path / "bridge.md"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge_path)

    db = await open_db(db_path)
    await db.close()

    result = await apply_manifest(make_manifest(), dry_run=True)
    assert result["snapshot_write"] == "would_insert"
    assert result["activity_write"] == "would_insert"


@pytest.mark.asyncio
async def test_codex_seed_apply_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "bridge.db"
    bridge_path = tmp_path / "bridge.md"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge_path)

    db = await open_db(db_path)
    await db.close()

    first = await apply_manifest(make_manifest(), dry_run=False)
    second = await apply_manifest(make_manifest(), dry_run=False)

    assert first["snapshot_write"] == "inserted"
    assert first["activity_write"] == "inserted"
    assert second["snapshot_write"] == "skipped_identical"
    assert second["activity_write"] == "skipped_duplicate"
    assert bridge_path.exists()

    db = await open_db(db_path)
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM system_snapshots WHERE system='codex'")
        snapshot_row = await cursor.fetchone()
        assert snapshot_row is not None
        assert snapshot_row[0] == 1

        cursor = await db.execute(
            "SELECT COUNT(*) FROM activity_log WHERE source='codex' AND project_name='bridge-baseline-seed'"
        )
        activity_row = await cursor.fetchone()
        assert activity_row is not None
        assert activity_row[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_codex_seed_skips_duplicate_baseline_activity_for_same_day_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "bridge.db"
    bridge_path = tmp_path / "bridge.md"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge_path)

    db = await open_db(db_path)
    await db.close()

    first = await apply_manifest(make_manifest(), dry_run=False)
    second = await apply_manifest(make_variant_manifest(), dry_run=False)

    assert first["activity_write"] == "inserted"
    assert second["activity_write"] == "skipped_duplicate"
    assert second["snapshot_write"] == "inserted"

    db = await open_db(db_path)
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM activity_log WHERE source='codex' AND timestamp='2026-04-14' AND project_name='bridge-baseline-seed'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1
    finally:
        await db.close()