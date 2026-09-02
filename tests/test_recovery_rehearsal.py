"""Disposable restore, reconstruction, and rollback rehearsal coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bridge_db import config, recovery
from bridge_db.db import (
    SCHEMA_VERSION,
    content_sha256,
    fts_text_for_section,
    insert_activity_row,
    open_db,
    upsert_fts_entry,
)
from bridge_db.recovery_rehearsal import rehearse_recovery
from bridge_db.tools.export import build_markdown


def _write_registry(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "canonical_key": "bridge-db",
                        "display_name": "BridgeDB",
                        "repo_full_name": "example/bridge-db",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


async def _current_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    export_matches: bool = True,
    corrupt_fts: bool = False,
    canonical_key: str = "example/bridge-db",
    pending_projection: bool = False,
    include_activity: bool = True,
) -> Path:
    registry = tmp_path / "project-registry.json"
    _write_registry(registry)
    monkeypatch.setattr(config, "PROJECT_REGISTRY_PATH", registry)
    db_path = tmp_path / "bridge.db"
    db = await open_db(db_path)
    await db.execute(
        "INSERT INTO context_sections (section_name, owner, content) "
        "VALUES ('career', 'claude_ai', 'rehearsal fixture')"
    )
    await upsert_fts_entry(
        db,
        "section",
        "career",
        fts_text_for_section("career", "rehearsal fixture"),
    )
    activity_id = 0
    if include_activity:
        activity = await insert_activity_row(
            db,
            source="codex",
            timestamp="2026-08-23",
            project_name="BridgeDB",
            summary="rehearsal fixture",
            canonical_key=canonical_key,
        )
        activity_id = activity.activity_id
        assert activity_id > 0
    if corrupt_fts:
        await db.execute(
            "UPDATE content_index SET text = 'corrupt but identity-preserving' "
            "WHERE source_type = 'activity' AND source_id = ?",
            (str(activity_id),),
        )
    content = await build_markdown(db)
    await db.execute(
        "INSERT INTO bridge_file_export_state (singleton, exported_content_sha256) "
        "VALUES (1, ?)",
        (content_sha256(content) if export_matches else "0" * 64,),
    )
    if pending_projection:
        await db.execute(
            "INSERT INTO bridge_projection_jobs (reason, target_key) VALUES (?, ?)",
            ("rehearsal fixture", "bridge-markdown"),
        )
    await db.commit()
    await db.close()
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    return db_path


async def test_recovery_rehearsal_proves_restore_reconstruction_and_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = await _current_anchor(tmp_path, monkeypatch)

    result = await rehearse_recovery(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert result["ready"] is True, result
    assert result["live_mutation_performed"] is False
    assert result["restore"]["schema_compatible"] is True
    assert result["fts"] == {
        "ok": True,
        "expected": 2,
        "indexed": 2,
        "missing": 0,
        "orphaned": 0,
        "content_mismatched": 0,
        "sources": result["fts"]["sources"],
    }
    assert result["ownership"]["preserved_after_rollback"] is True
    assert result["source_mappings"]["drift_count"] == 0
    assert result["projection_export_reconstruction"]["ready"] is True
    assert result["rollback"]["ready"] is True
    assert result["cleanup"] == "temporary_artifacts_removed"


async def test_recovery_rehearsal_rejects_export_state_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = await _current_anchor(tmp_path, monkeypatch, export_matches=False)

    result = await rehearse_recovery(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert result["ready"] is False
    assert result["projection_export_reconstruction"]["ready"] is False
    assert (
        result["projection_export_reconstruction"]["database_export_matches"]
        is False
    )


async def test_recovery_rehearsal_rejects_fts_content_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = await _current_anchor(tmp_path, monkeypatch, corrupt_fts=True)

    result = await rehearse_recovery(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert result["ready"] is False
    assert result["fts"]["content_mismatched"] == 1
    assert result["fts"]["ok"] is False


async def test_recovery_rehearsal_rejects_symlink_project_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = await _current_anchor(tmp_path, monkeypatch)
    real_registry = tmp_path / "real-project-registry.json"
    _write_registry(real_registry)
    linked_registry = tmp_path / "linked-project-registry.json"
    linked_registry.symlink_to(real_registry)
    monkeypatch.setattr(config, "PROJECT_REGISTRY_PATH", linked_registry)

    result = await rehearse_recovery(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert result["ready"] is False
    assert result["errors"] == ["project_registry_not_regular"]


async def test_recovery_rehearsal_requires_current_verified_anchor(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "bridge.db"
    db = await open_db(db_path)
    await db.close()

    result = await rehearse_recovery(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert result["ready"] is False
    assert result["backup_selection"]["ready"] is False
    assert result["errors"] == ["current_verified_anchor_required"]


async def test_recovery_rehearsal_rejects_source_mapping_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = await _current_anchor(
        tmp_path,
        monkeypatch,
        canonical_key="wrong-owner/bridge-db",
    )

    result = await rehearse_recovery(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert result["ready"] is False
    assert result["source_mappings"]["drift_count"] == 1
    assert result["source_mappings"]["ready"] is False


async def test_recovery_rehearsal_rejects_pending_projection_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = await _current_anchor(
        tmp_path,
        monkeypatch,
        pending_projection=True,
    )

    result = await rehearse_recovery(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert result["ready"] is False
    assert result["projection_export_reconstruction"]["pending_projection_jobs"] == 1
    assert result["projection_export_reconstruction"]["ready"] is False


async def test_recovery_rehearsal_passes_with_empty_source_tables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid registry and no rows to map is ready, not a failed rehearsal."""
    db_path = await _current_anchor(tmp_path, monkeypatch, include_activity=False)

    result = await rehearse_recovery(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert result["source_mappings"]["registry_present"] is True
    assert result["source_mappings"]["drift_count"] == 0
    assert result["source_mappings"]["ready"] is True
    assert result["ready"] is True, result
