"""RecoveryAnchorV1 creation and fail-closed verification coverage."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from bridge_db import recovery
from bridge_db.db import SCHEMA_VERSION, open_db


async def _source_database(tmp_path: Path) -> Path:
    db_path = tmp_path / "bridge.db"
    db = await open_db(db_path)
    await db.execute(
        "INSERT INTO context_sections (section_name, owner, content) "
        "VALUES ('career', 'codex', 'private sentinel value')"
    )
    await db.commit()
    await db.close()
    return db_path


async def test_create_anchor_is_private_and_disposable_recovery_verifies(
    tmp_path: Path,
) -> None:
    db_path = await _source_database(tmp_path)

    result = recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    anchor = recovery.recovery_anchor_path(db_path)
    manifest = json.loads(
        (anchor / recovery.RECOVERY_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert result["state"] == "verified"
    assert result["ready"] is True
    assert result["digest_ok"] is True
    assert result["integrity_ok"] is True
    assert result["semantic_readback_ok"] is True
    assert manifest["schema"] == "RecoveryAnchorV1"
    assert manifest["source_schema_version"] == SCHEMA_VERSION
    assert manifest["semantic_readback"]["row_counts"]["context_sections"] == 1
    assert "private sentinel value" not in json.dumps(result)
    assert anchor.stat().st_mode & 0o777 == 0o700
    assert (
        anchor.joinpath(recovery.RECOVERY_DATABASE_NAME).stat().st_mode & 0o777 == 0o600
    )
    assert (
        anchor.joinpath(recovery.RECOVERY_MANIFEST_NAME).stat().st_mode & 0o777 == 0o600
    )


async def test_anchor_creation_preserves_existing_verified_bundle(
    tmp_path: Path,
) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    with pytest.raises(FileExistsError):
        recovery.create_recovery_anchor(
            db_path,
            expected_schema_version=SCHEMA_VERSION,
        )


async def test_anchor_inventory_becomes_stale_after_source_insert(
    tmp_path: Path,
) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    with sqlite3.connect(db_path) as changed:
        changed.execute(
            "INSERT INTO activity_log "
            "(source, timestamp, project_name, summary) VALUES (?, ?, ?, ?)",
            ("codex", "2026-07-18T09:00:00Z", "bridge-db", "after anchor"),
        )

    result = recovery.recovery_anchor_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert result["state"] == "stale"
    assert result["ready"] is False
    assert result["source_current"] is False
    assert "source_changed_since_anchor" in result["errors"]


async def test_anchor_inventory_becomes_stale_after_same_count_update(
    tmp_path: Path,
) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    with sqlite3.connect(db_path) as changed:
        changed.execute(
            "UPDATE context_sections SET content = ? WHERE section_name = ?",
            ("changed after anchor", "career"),
        )

    result = recovery.recovery_anchor_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert result["state"] == "stale"
    assert result["source_current"] is False


async def test_anchor_inventory_includes_bridge_sidecar_state(
    tmp_path: Path,
) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    with sqlite3.connect(db_path) as changed:
        changed.execute(
            "INSERT INTO bridge_file_export_state "
            "(singleton, exported_content_sha256) VALUES (1, ?)",
            ("a" * 64,),
        )

    result = recovery.recovery_anchor_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert result["state"] == "stale"
    assert result["source_current"] is False


async def test_anchor_inventory_includes_autoincrement_sequence_state(
    tmp_path: Path,
) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    with sqlite3.connect(db_path) as changed:
        cursor = changed.execute(
            "INSERT INTO activity_log "
            "(source, timestamp, project_name, summary) VALUES (?, ?, ?, ?)",
            ("codex", "2026-07-18T09:00:00Z", "bridge-db", "transient"),
        )
        changed.execute("DELETE FROM activity_log WHERE id = ?", (cursor.lastrowid,))

    result = recovery.recovery_anchor_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert result["state"] == "stale"
    assert result["source_current"] is False


async def test_anchor_supports_sqlite_uri_characters_in_source_path(
    tmp_path: Path,
) -> None:
    special = tmp_path / "bridge#operator?.db"
    db = await open_db(special)
    await db.close()

    recovery.create_recovery_anchor(
        special,
        expected_schema_version=SCHEMA_VERSION,
    )
    result = recovery.recovery_anchor_inventory(
        special,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert result["state"] == "verified"
    assert result["ready"] is True
    assert result["source_current"] is True


async def test_anchor_detects_backup_tampering(tmp_path: Path) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    database = recovery.recovery_anchor_path(db_path) / recovery.RECOVERY_DATABASE_NAME
    with database.open("ab") as handle:
        handle.write(b"tamper")

    result = recovery.recovery_anchor_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert result["state"] == "invalid"
    assert result["ready"] is False
    assert "digest_mismatch" in result["errors"]
    assert "byte_size_mismatch" in result["errors"]


async def test_anchor_detects_truncated_backup(tmp_path: Path) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    database = recovery.recovery_anchor_path(db_path) / recovery.RECOVERY_DATABASE_NAME
    os.truncate(database, 128)

    result = recovery.recovery_anchor_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert result["state"] == "invalid"
    assert "digest_mismatch" in result["errors"]
    assert "disposable_recovery_failed" in result["errors"]


async def test_anchor_detects_missing_metadata(tmp_path: Path) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    manifest = recovery.recovery_anchor_path(db_path) / recovery.RECOVERY_MANIFEST_NAME
    manifest.unlink()

    result = recovery.recovery_anchor_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert result["state"] == "invalid"
    assert result["errors"] == ["metadata_missing"]


async def test_anchor_detects_manifest_digest_mismatch(tmp_path: Path) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    manifest_path = (
        recovery.recovery_anchor_path(db_path) / recovery.RECOVERY_MANIFEST_NAME
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = recovery.recovery_anchor_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert result["state"] == "invalid"
    assert result["digest_ok"] is False
    assert "digest_mismatch" in result["errors"]


async def test_anchor_detects_incompatible_schema_even_with_rebound_digest(
    tmp_path: Path,
) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    anchor = recovery.recovery_anchor_path(db_path)
    database = anchor / recovery.RECOVERY_DATABASE_NAME
    with sqlite3.connect(database) as changed:
        changed.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    manifest_path = anchor / recovery.RECOVERY_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = hashlib.sha256(database.read_bytes()).hexdigest()
    manifest["backup_bytes"] = database.stat().st_size
    manifest["source_schema_version"] = SCHEMA_VERSION + 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = recovery.recovery_anchor_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert result["state"] == "invalid"
    assert result["digest_ok"] is True
    assert result["errors"] == ["schema_incompatible"]


async def test_anchor_atomic_publication_failure_leaves_no_partial_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = await _source_database(tmp_path)
    anchor = recovery.recovery_anchor_path(db_path)

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated publication failure")

    monkeypatch.setattr(recovery.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated publication failure"):
        recovery.create_recovery_anchor(
            db_path,
            expected_schema_version=SCHEMA_VERSION,
        )

    assert not anchor.exists()
    assert list(tmp_path.glob(f".{anchor.name}.tmp-*")) == []


async def test_anchor_refuses_publication_when_source_changes_after_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = await _source_database(tmp_path)
    anchor = recovery.recovery_anchor_path(db_path)
    original_verify = recovery.verify_recovery_anchor
    source_changed = False

    def verify_then_change_source(
        candidate: Path,
        *,
        expected_schema_version: int,
    ) -> dict[str, object]:
        nonlocal source_changed
        result = original_verify(
            candidate,
            expected_schema_version=expected_schema_version,
        )
        if candidate != anchor and result["ready"] and not source_changed:
            with sqlite3.connect(db_path) as changed:
                changed.execute(
                    "INSERT INTO activity_log "
                    "(source, timestamp, project_name, summary) VALUES (?, ?, ?, ?)",
                    (
                        "codex",
                        "2026-07-18T10:00:00Z",
                        "bridge-db",
                        "concurrent commit",
                    ),
                )
            source_changed = True
        return result

    monkeypatch.setattr(recovery, "verify_recovery_anchor", verify_then_change_source)

    with pytest.raises(
        RuntimeError,
        match="source database changed during recovery anchor creation",
    ):
        recovery.create_recovery_anchor(
            db_path,
            expected_schema_version=SCHEMA_VERSION,
        )

    assert source_changed is True
    assert not anchor.exists()
    assert list(tmp_path.glob(f".{anchor.name}.tmp-*")) == []
