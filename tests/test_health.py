"""Tests for the health MCP tool."""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx

from bridge_db import config, recovery
from bridge_db.db import (
    SCHEMA_VERSION,
    _backup_db_file,  # pyright: ignore[reportPrivateUsage]
    fts_text_for_activity,
    fts_text_for_handoff,
    fts_text_for_section,
    fts_text_for_snapshot,
    insert_activity_row,
    upsert_fts_entry,
)
from bridge_db.tools import health as mod


@pytest.fixture
def fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    mod.register(cap)
    return cap.fns


def _replace_test_anchor(db_path: Path) -> None:
    shutil.rmtree(recovery.recovery_anchor_path(db_path), ignore_errors=True)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )


@pytest.fixture(autouse=True)
async def patch_db_path(
    db: aiosqlite.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point health filesystem inputs at isolated deterministic fixtures."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    bridge = tmp_path / "bridge.md"
    bridge.write_text("# BridgeDB\n", encoding="utf-8")
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge)
    await db.execute(
        "INSERT INTO bridge_file_export_state (singleton, exported_content_sha256) "
        "VALUES (1, 'test-export-state')"
    )
    await db.commit()
    await _backup_db_file(db, "health-fixture")
    _replace_test_anchor(config.DB_PATH)


async def test_health_returns_ok_on_healthy_db(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    # Create the DB file so db_exists=True
    (tmp_path / "test.db").touch()
    ctx = make_ctx(db)
    result = await fns["health"](ctx=ctx)
    assert result["ok"] is True
    assert result["db_exists"] is True
    assert result["schema_version"] == SCHEMA_VERSION
    assert result["evidence_lifecycle"]["audit_degraded"] is False
    assert result["evidence_lifecycle"]["destructive_actions"] == "approval_required"
    assert (
        result["evidence_lifecycle"]["acknowledgements"]["authority"]
        == "review_only_no_cleanup_authority"
    )


async def test_health_degrades_when_recovery_evidence_is_missing(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
) -> None:
    for path in tmp_path.glob("test.db.*.bak*"):
        path.unlink()
    shutil.rmtree(recovery.recovery_anchor_path(tmp_path / "test.db"))

    result = await fns["health"](ctx=make_ctx(db))

    assert result["ok"] is False
    assert result["storage_ok"] is False
    assert result["evidence_lifecycle"]["migration_backups"]["count"] == 0
    assert result["evidence_lifecycle"]["legacy_backup_provenance_ok"] is False
    assert result["evidence_lifecycle"]["backup_integrity_ok"] is True
    status = await fns["status"](ctx=make_ctx(db))
    assert status["signals"]["migration_backup_integrity_ok"] is True
    assert result["evidence_lifecycle"]["current_recovery_anchor"]["state"] == "missing"
    assert result["evidence_lifecycle"]["recovery_integrity_ok"] is False


async def test_health_degrades_without_current_anchor_even_with_verified_legacy_backup(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
) -> None:
    shutil.rmtree(recovery.recovery_anchor_path(tmp_path / "test.db"))

    result = await fns["health"](ctx=make_ctx(db))

    assert result["evidence_lifecycle"]["legacy_backup_provenance_ok"] is True
    assert result["evidence_lifecycle"]["current_recovery_anchor"]["state"] == "missing"
    assert result["evidence_lifecycle"]["recovery_integrity_ok"] is False
    assert result["storage_ok"] is False


async def test_health_degrades_on_durable_audit_failure_receipt(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
) -> None:
    (tmp_path / "test.db").touch()
    config.AUDIT_FAILURE_LOG_PATH.write_text(
        '{"kind":"audit_write_failure","status":"open"}\n',
        encoding="utf-8",
    )

    result = await fns["health"](ctx=make_ctx(db))

    assert result["ok"] is False
    assert result["storage_ok"] is False
    assert result["evidence_lifecycle"]["audit_degraded"] is True
    assert result["evidence_lifecycle"]["audit_failures"]["state"] == "degraded"


async def test_health_degrades_on_open_evidence_disposition(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
) -> None:
    (tmp_path / "test.db").touch()
    config.EVIDENCE_DISPOSITION_LOG_PATH.write_text(
        '{"transaction_id":"tx-open","status":"prepared"}\n',
        encoding="utf-8",
    )

    result = await fns["health"](ctx=make_ctx(db))

    assert result["ok"] is False
    assert result["storage_ok"] is False
    assert result["evidence_lifecycle"]["disposition_degraded"] is True
    assert result["evidence_lifecycle"]["dispositions"]["open_count"] == 1


async def test_health_accepts_completed_evidence_disposition(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
) -> None:
    (tmp_path / "test.db").touch()
    config.EVIDENCE_DISPOSITION_LOG_PATH.write_text(
        '{"transaction_id":"tx-complete","status":"prepared"}\n'
        '{"transaction_id":"tx-complete","status":"completed"}\n',
        encoding="utf-8",
    )

    result = await fns["health"](ctx=make_ctx(db))

    assert result["ok"] is True
    assert result["storage_ok"] is True
    assert result["evidence_lifecycle"]["disposition_degraded"] is False
    assert result["evidence_lifecycle"]["dispositions"]["completed_count"] == 1


async def test_health_separates_verified_current_anchor_from_legacy_uncertainty(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    shutil.rmtree(recovery.recovery_anchor_path(db_path))
    legacy = tmp_path / "test.db.pre-v1.bak"
    legacy.write_bytes(db_path.read_bytes())

    without_anchor = await fns["health"](ctx=make_ctx(db))
    assert without_anchor["storage_ok"] is False
    assert (
        without_anchor["evidence_lifecycle"]["current_recovery_anchor"]["state"]
        == "missing"
    )
    assert (
        without_anchor["evidence_lifecycle"]["migration_backups"]["provenance_state"]
        == "readable_but_unknown"
    )

    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    with_anchor = await fns["health"](ctx=make_ctx(db))

    assert with_anchor["storage_ok"] is True
    assert with_anchor["evidence_lifecycle"]["current_recovery_ready"] is True
    assert (
        with_anchor["evidence_lifecycle"]["current_recovery_anchor"]["state"]
        == "verified"
    )
    assert with_anchor["evidence_lifecycle"]["legacy_backup_provenance_ok"] is False
    assert with_anchor["evidence_lifecycle"]["backup_integrity_ok"] is False
    assert with_anchor["evidence_lifecycle"]["recovery_integrity_ok"] is True
    assert (
        with_anchor["evidence_lifecycle"]["migration_backups"][
            "provenance_unverified_count"
        ]
        == 1
    )
    assert legacy.exists()


async def test_health_degrades_when_current_anchor_is_invalid(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    shutil.rmtree(recovery.recovery_anchor_path(db_path))
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    manifest = recovery.recovery_anchor_path(db_path) / recovery.RECOVERY_MANIFEST_NAME
    manifest.write_text("{}", encoding="utf-8")

    result = await fns["health"](ctx=make_ctx(db))

    assert result["storage_ok"] is False
    assert result["evidence_lifecycle"]["current_recovery_ready"] is False
    assert result["evidence_lifecycle"]["current_recovery_anchor"]["state"] == "invalid"


async def test_health_degrades_when_current_anchor_is_stale(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    shutil.rmtree(recovery.recovery_anchor_path(db_path))
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary) "
        "VALUES ('codex', '2026-07-18', 'bridge-db', 'after anchor')"
    )
    await db.commit()

    result = await fns["health"](ctx=make_ctx(db))

    assert result["storage_ok"] is False
    assert result["evidence_lifecycle"]["current_recovery_ready"] is False
    assert result["evidence_lifecycle"]["current_recovery_anchor"]["state"] == "stale"
    assert (
        "source_changed_since_anchor"
        in result["evidence_lifecycle"]["current_recovery_anchor"]["errors"]
    )


async def test_health_row_counts_reflect_data(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / "test.db").touch()
    await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary) "
        "VALUES ('cc', '2026-04-14', 'P', 'S')"
    )
    await db.commit()
    ctx = make_ctx(db)
    result = await fns["health"](ctx=ctx)
    assert result["ok"] is False
    assert result["row_counts"]["activity_log"] == 1
    assert result["row_counts"]["context_sections"] == 0
    assert result["row_counts"]["pending_handoffs"] == 0
    assert result["row_counts"]["system_snapshots"] == 0
    assert result["row_counts"]["cost_records"] == 0
    assert result["fts_index"]["ok"] is False
    assert result["fts_index"]["missing"] == 1
    assert result["fts_index"]["orphaned"] == 0


async def test_health_source_trust_breakdown(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / "test.db").touch()
    await db.execute(
        "INSERT INTO pending_handoffs (project_name, source_trust) VALUES ('A', 'operator')"
    )
    await db.execute(
        "INSERT INTO pending_handoffs (project_name, source_trust) VALUES ('B', 'agent')"
    )
    await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, source_trust) "
        "VALUES ('cc', '2026-06-10', 'P', 'S', 'ingested')"
    )
    await db.execute(
        "INSERT INTO context_sections (section_name, owner, content, source_trust) "
        "VALUES ('career', 'claude_ai', 'x', 'operator')"
    )
    await db.execute(
        "INSERT INTO system_snapshots (system, snapshot_date, data, source_trust) "
        "VALUES ('cc', '2026-06-10', '{}', 'agent')"
    )
    await db.commit()
    ctx = make_ctx(db)
    result = await fns["health"](ctx=ctx)

    breakdown = result["source_trust_breakdown"]
    assert set(breakdown) == {
        "context_sections",
        "activity_log",
        "system_snapshots",
        "pending_handoffs",
    }
    # every table carries all three levels, default-zero-filled
    for table_counts in breakdown.values():
        assert set(table_counts) == {"operator", "agent", "ingested"}
    # each table's seeded level is counted in the right bucket
    assert breakdown["pending_handoffs"] == {"operator": 1, "agent": 1, "ingested": 0}
    assert breakdown["activity_log"]["ingested"] == 1
    assert breakdown["context_sections"]["operator"] == 1
    assert breakdown["system_snapshots"]["agent"] == 1


async def test_status_pending_handoffs_by_trust(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / "test.db").touch()
    await db.execute(
        "INSERT INTO pending_handoffs (project_name, source_trust) VALUES ('A', 'operator')"
    )
    await db.execute(
        "INSERT INTO pending_handoffs (project_name, source_trust, status) "
        "VALUES ('B', 'agent', 'pending')"
    )
    # A cleared agent handoff must NOT count — the signal is pending-scoped.
    await db.execute(
        "INSERT INTO pending_handoffs (project_name, source_trust, status) "
        "VALUES ('C', 'agent', 'cleared')"
    )
    await db.commit()
    ctx = make_ctx(db)
    result = await fns["status"](ctx=ctx)

    assert result["pending_handoffs_by_trust"] == {
        "operator": 1,
        "agent": 1,
        "ingested": 0,
    }


async def test_health_unprocessed_shipped_count(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / "test.db").touch()
    # One SHIPPED + one SHIPPED+PROCESSED
    await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags) "
        "VALUES ('cc', '2026-04-14', 'A', 'S', ?)",
        (json.dumps(["SHIPPED"]),),
    )
    await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags) "
        "VALUES ('cc', '2026-04-14', 'B', 'S', ?)",
        (json.dumps(["SHIPPED", "PROCESSED"]),),
    )
    await db.commit()
    ctx = make_ctx(db)
    result = await fns["health"](ctx=ctx)
    assert result["unprocessed_shipped_count"] == 1


async def test_health_actionable_unprocessed_excludes_dispositions(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / "test.db").touch()
    cursor = await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags) "
        "VALUES ('cc', '2026-06-13', 'fable-outputs', 'artifact', ?) RETURNING id",
        (json.dumps(["SHIPPED"]),),
    )
    disposed_row = await cursor.fetchone()
    assert disposed_row is not None
    disposed_id = int(disposed_row[0])
    await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags) "
        "VALUES ('cc', '2026-06-13', 'personal-ops', 'merged', ?)",
        (json.dumps(["SHIPPED"]),),
    )
    await db.execute(
        "UPDATE activity_log SET sync_disposition = 'unsynced_by_policy', "
        "sync_reason = 'experimental artifact', sync_disposition_by = 'codex', "
        "synced_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
        (disposed_id,),
    )
    await db.commit()

    result = await fns["health"](ctx=make_ctx(db))

    assert result["unprocessed_shipped_count"] == 2
    assert result["actionable_unprocessed_shipped_count"] == 1


async def test_health_counts_processed_shipped_without_receipts(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / "test.db").touch()
    # One legacy-processed event without a receipt and one receipt-backed event.
    cursor = await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags) "
        "VALUES ('cc', '2026-04-14', 'A', 'S', ?) RETURNING id",
        (json.dumps(["SHIPPED", "PROCESSED"]),),
    )
    receiptless_row = await cursor.fetchone()
    assert receiptless_row is not None
    receiptless_id = int(receiptless_row[0])
    cursor = await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags) "
        "VALUES ('cc', '2026-04-14', 'B', 'S', ?) RETURNING id",
        (json.dumps(["SHIPPED", "PROCESSED"]),),
    )
    receipted_row = await cursor.fetchone()
    assert receipted_row is not None
    receipted_id = int(receipted_row[0])
    await db.execute(
        "UPDATE activity_log SET sync_disposition = 'synced', "
        "sync_downstream_system = 'notion', "
        "sync_downstream_ref = 'https://notion.so/example', "
        "sync_disposition_by = 'codex', "
        "synced_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
        (receipted_id,),
    )
    await db.commit()

    result = await fns["health"](ctx=make_ctx(db))

    assert receiptless_id != receipted_id
    assert result["processed_shipped_without_receipt_count"] == 1


async def test_health_orphan_metrics_flag_disposition_malformation(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    """The v14 orphan metrics are the compensating detection control for the
    field requirements the old NOT NULL columns enforced: a 'synced' row missing
    downstream proof, and a policy disposition missing its reason (both of which
    the nullable sync_* columns no longer prevent at the SQL layer)."""
    (tmp_path / "test.db").touch()
    # A clean SHIPPED row keeps the metrics at zero.
    await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags) "
        "VALUES ('cc', '2026-07-01', 'clean', 's', ?)",
        (json.dumps(["SHIPPED"]),),
    )
    await db.commit()
    clean = await fns["health"](ctx=make_ctx(db))
    assert clean["receipt_orphan_count"] == 0
    assert clean["disposition_orphan_count"] == 0

    # Malformed states written directly (bypassing record_disposition, which
    # would reject them) must be flagged.
    await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags, "
        "sync_disposition) VALUES ('cc', '2026-07-01', 'synced-no-proof', 's', ?, 'synced')",
        (json.dumps(["SHIPPED"]),),
    )
    await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags, "
        "sync_disposition) VALUES ('cc', '2026-07-01', 'policy-no-reason', 's', ?, "
        "'unsynced_by_policy')",
        (json.dumps(["SHIPPED"]),),
    )
    await db.commit()

    result = await fns["health"](ctx=make_ctx(db))
    assert result["receipt_orphan_count"] == 1  # synced row lacks downstream proof
    assert result["disposition_orphan_count"] == 1  # policy row lacks a reason


async def test_health_bridge_file_info(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "test.db").touch()
    bridge = tmp_path / "bridge.md"
    bridge.write_text("# test")
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge)
    ctx = make_ctx(db)
    result = await fns["health"](ctx=ctx)
    assert result["bridge_file_exists"] is True
    assert isinstance(result["bridge_file_age_seconds"], float)
    assert result["bridge_file_age_seconds"] >= 0


async def test_health_bridge_file_missing(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "test.db").touch()
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", tmp_path / "nonexistent.md")
    ctx = make_ctx(db)
    result = await fns["health"](ctx=ctx)
    assert result["ok"] is False
    assert result["bridge_file_exists"] is False
    assert result["bridge_file_age_seconds"] is None


async def test_status_returns_compact_operator_summary(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "test.db").touch()
    bridge = tmp_path / "bridge.md"
    bridge.write_text(
        "## Career & Professional Target\nCareer notes\n", encoding="utf-8"
    )
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge)

    await db.execute(
        "INSERT INTO context_sections (section_name, owner, content) VALUES (?, ?, ?)",
        ("career", "claude_ai", "Career notes"),
    )
    await upsert_fts_entry(
        db,
        "section",
        "career",
        fts_text_for_section("career", "Career notes"),
    )
    await db.execute(
        "INSERT INTO system_snapshots (system, snapshot_date, data) VALUES (?, ?, ?)",
        ("cc", "2026-04-17", '{"active_projects":"- bridge-db"}'),
    )
    await upsert_fts_entry(
        db,
        "snapshot",
        "1",
        fts_text_for_snapshot('{"active_projects":"- bridge-db"}'),
    )
    cursor = await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags) "
        "VALUES ('cc', '2026-04-17', 'bridge-db', 'checked operator status', ?) RETURNING id",
        (json.dumps(["SHIPPED"]),),
    )
    activity_row = await cursor.fetchone()
    assert activity_row is not None
    await upsert_fts_entry(
        db,
        "activity",
        str(activity_row[0]),
        fts_text_for_activity("bridge-db", "checked operator status", None),
    )
    await db.commit()
    _replace_test_anchor(tmp_path / "test.db")

    ctx = make_ctx(db)
    result = await fns["status"](ctx=ctx)

    assert result["ok"] is True
    assert result["overall"] == "healthy"
    assert result["row_counts"]["context_sections"] == 1
    assert result["signals"]["pending_handoffs"] == 0
    assert result["signals"]["unprocessed_shipped"] == 1
    assert result["signals"]["actionable_unprocessed_shipped"] == 1
    assert result["signals"]["dispositioned_unprocessed_shipped"] == 0
    assert result["signals"]["processed_shipped_without_receipt"] == 0
    assert result["signals"]["fts_missing"] == 0
    assert result["signals"]["fts_orphaned"] == 0
    assert result["fts_index"]["ok"] is True
    assert result["latest_snapshots"]["cc"] == "2026-04-17"
    assert result["latest_activity"]["cc"] == "2026-04-17 (bridge-db)"


async def test_status_latest_activity_uses_server_recorded_time(
    db: aiosqlite.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BDB-DS-059-R1: status cannot be pinned by logical event time."""
    await _make_status_health_ready(tmp_path, monkeypatch)
    await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags, created_at) "
        "VALUES ('cc', '9999-12-31T23:59:59Z', 'ForgedFuture', 'older', '[]', "
        "'2026-07-13T12:00:00Z')"
    )
    await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags, created_at) "
        "VALUES ('cc', '2026-07-14T12:00:00Z', 'CurrentEvidence', 'newer', '[]', "
        "'2026-07-14T12:00:00Z')"
    )
    await db.commit()

    result = await mod.collect_status_summary(db, now=FIXED_NOW)

    assert result["latest_activity"]["cc"] == ("2026-07-14T12:00:00Z (CurrentEvidence)")


FIXED_NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


async def _make_status_health_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "test.db").touch()
    bridge = tmp_path / "bridge.md"
    bridge.write_text("# test", encoding="utf-8")
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge)


async def _seed_snapshot(
    db: aiosqlite.Connection,
    system: str,
    snapshot_date: str,
    created_at: str,
    data: str = "{}",
) -> None:
    cursor = await db.execute(
        "INSERT INTO system_snapshots (system, snapshot_date, data, created_at) "
        "VALUES (?, ?, ?, ?)",
        (system, snapshot_date, data, created_at),
    )
    snapshot_id = cursor.lastrowid
    assert snapshot_id is not None
    await upsert_fts_entry(
        db, "snapshot", str(snapshot_id), fts_text_for_snapshot(data)
    )


async def _seed_activity(
    db: aiosqlite.Connection,
    source: str,
    created_at: str,
    tags: list[str] | None = None,
) -> int:
    cursor = await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags, created_at) "
        "VALUES (?, '2026-07-07', ?, 'status check', ?, ?)",
        (source, f"{source}-project", json.dumps(tags or []), created_at),
    )
    activity_id = cursor.lastrowid
    assert activity_id is not None
    await upsert_fts_entry(
        db,
        "activity",
        str(activity_id),
        fts_text_for_activity(f"{source}-project", "status check", None, tags or []),
    )
    return int(activity_id)


async def _seed_handoff(
    db: aiosqlite.Connection,
    status: str,
    dispatched_at: str,
    picked_up_at: str | None = None,
) -> None:
    cursor = await db.execute(
        "INSERT INTO pending_handoffs (project_name, status, dispatched_at, picked_up_at) "
        "VALUES ('bridge-db', ?, ?, ?)",
        (status, dispatched_at, picked_up_at),
    )
    handoff_id = cursor.lastrowid
    assert handoff_id is not None
    await upsert_fts_entry(
        db,
        "handoff",
        str(handoff_id),
        fts_text_for_handoff("bridge-db", None, None, None),
    )


async def test_status_freshness_reports_fresh_snapshots(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _make_status_health_ready(tmp_path, monkeypatch)
    await _seed_snapshot(db, "cc", "2026-07-07", "2026-07-07T11:00:00Z")
    await _seed_snapshot(db, "codex", "2026-07-07", "2026-07-07T10:00:00Z")
    await db.commit()

    result = await mod.collect_status_summary(db, now=FIXED_NOW)

    assert result["freshness"]["thresholds_hours"] == {
        "snapshot_stale_after": 48.0,
        "activity_quiet_after": 72.0,
        "pending_handoff_stale_after": 168.0,
        "active_handoff_stale_after": 72.0,
    }
    assert result["freshness"]["snapshots"]["cc"] == {
        "state": "fresh",
        "owner": "cc",
        "latest_snapshot_date": "2026-07-07",
        "latest_created_at": "2026-07-07T11:00:00Z",
        "age_hours": 1.0,
        "superseding_activity_id": None,
        "next_action": "none",
    }
    assert result["freshness"]["snapshots"]["codex"]["state"] == "fresh"


async def test_status_marks_snapshot_superseded_by_newer_same_source_activity(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _make_status_health_ready(tmp_path, monkeypatch)
    await _seed_snapshot(db, "cc", "2026-07-07", "2026-07-07T10:00:00Z")
    await _seed_snapshot(db, "codex", "2026-07-07", "2026-07-07T11:00:00Z")
    activity_id = await _seed_activity(db, "cc", "2026-07-07T11:30:00Z")
    await db.commit()
    _replace_test_anchor(tmp_path / "test.db")

    result = await mod.collect_status_summary(db, now=FIXED_NOW)

    cc = result["freshness"]["snapshots"]["cc"]
    assert cc["state"] == "superseded"
    assert cc["superseding_activity_id"] == activity_id
    assert cc["next_action"] == "cc_refresh_snapshot"
    assert result["storage_health"] == "healthy"
    assert result["operating_state"] == "stale"


async def test_status_ignores_lifecycle_only_activity_for_snapshot_supersession(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _make_status_health_ready(tmp_path, monkeypatch)
    await _seed_snapshot(db, "cc", "2026-07-07", "2026-07-07T11:00:00Z")
    await _seed_snapshot(db, "codex", "2026-07-07", "2026-07-07T11:00:00Z")
    await _seed_activity(
        db,
        "cc",
        "2026-07-07T11:30:00Z",
        tags=["session-boundary"],
    )
    await db.commit()
    _replace_test_anchor(tmp_path / "test.db")

    result = await mod.collect_status_summary(db, now=FIXED_NOW)

    cc = result["freshness"]["snapshots"]["cc"]
    assert cc["state"] == "fresh"
    assert cc["superseding_activity_id"] is None
    assert cc["next_action"] == "none"
    assert result["operating_state"] == "fresh"


async def test_status_uses_latest_substantive_activity_despite_newer_lifecycle_row(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _make_status_health_ready(tmp_path, monkeypatch)
    await _seed_snapshot(db, "cc", "2026-07-07", "2026-07-07T10:00:00Z")
    await _seed_snapshot(db, "codex", "2026-07-07", "2026-07-07T11:00:00Z")
    substantive_id = await _seed_activity(db, "cc", "2026-07-07T11:15:00Z")
    await _seed_activity(
        db,
        "cc",
        "2026-07-07T11:30:00Z",
        tags=["session-boundary"],
    )
    await db.commit()

    result = await mod.collect_status_summary(db, now=FIXED_NOW)

    cc = result["freshness"]["snapshots"]["cc"]
    assert cc["state"] == "superseded"
    assert cc["superseding_activity_id"] == substantive_id
    assert cc["next_action"] == "cc_refresh_snapshot"
    assert result["operating_state"] == "stale"


async def test_status_freshness_reports_stale_snapshots_without_degrading_health(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _make_status_health_ready(tmp_path, monkeypatch)
    await _seed_snapshot(db, "cc", "2026-07-04", "2026-07-04T11:00:00Z")
    await _seed_snapshot(db, "codex", "2026-07-07", "2026-07-07T11:00:00Z")
    await db.commit()
    _replace_test_anchor(tmp_path / "test.db")

    result = await mod.collect_status_summary(db, now=FIXED_NOW)

    assert result["ok"] is True
    assert result["overall"] == "healthy"
    assert result["storage_health"] == "healthy"
    assert result["operating_state"] == "stale"
    assert result["freshness"]["overall"] == "stale"
    assert result["freshness"]["snapshots"]["cc"]["state"] == "stale"
    assert result["freshness"]["snapshots"]["cc"]["age_hours"] == 73.0
    assert result["freshness"]["next_actions"] == [
        {
            "action": "cc_refresh_snapshot",
            "owner": "cc",
            "reason": "cc snapshot freshness is stale.",
        }
    ]


async def test_status_freshness_reports_missing_snapshots(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _make_status_health_ready(tmp_path, monkeypatch)

    result = await mod.collect_status_summary(db, now=FIXED_NOW)

    assert result["ok"] is True
    assert result["overall"] == "healthy"
    assert result["freshness"]["snapshots"]["cc"]["state"] == "missing"
    assert result["freshness"]["snapshots"]["cc"]["latest_snapshot_date"] == "none"
    assert result["freshness"]["snapshots"]["cc"]["latest_created_at"] == "none"
    assert result["freshness"]["snapshots"]["cc"]["age_hours"] is None
    assert (
        result["freshness"]["snapshots"]["cc"]["next_action"] == "cc_refresh_snapshot"
    )
    assert (
        result["freshness"]["snapshots"]["codex"]["next_action"]
        == "codex_refresh_snapshot"
    )


async def test_status_freshness_reports_quiet_and_missing_activity_sources(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _make_status_health_ready(tmp_path, monkeypatch)
    await _seed_activity(db, "cc", "2026-07-03T07:00:00Z")
    await db.commit()

    result = await mod.collect_status_summary(db, now=FIXED_NOW)
    activity = result["freshness"]["activity_sources"]

    assert activity["cc"]["state"] == "quiet"
    assert activity["cc"]["latest"] == "2026-07-03T07:00:00Z"
    assert activity["cc"]["age_hours"] == 101.0
    assert activity["codex"]["state"] == "missing"
    assert activity["claude_ai"]["state"] == "missing"
    assert activity["notion_os"]["state"] == "missing"
    assert activity["personal_ops"]["state"] == "missing"


async def test_status_freshness_reports_stale_pending_and_active_handoffs(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _make_status_health_ready(tmp_path, monkeypatch)
    await _seed_handoff(db, "pending", "2026-06-29T11:00:00Z")
    await _seed_handoff(
        db,
        "active",
        "2026-07-06T12:00:00Z",
        picked_up_at="2026-07-04T11:00:00Z",
    )
    await db.commit()
    _replace_test_anchor(tmp_path / "test.db")

    result = await mod.collect_status_summary(db, now=FIXED_NOW)

    assert result["freshness"]["handoffs"] == {
        "pending_count": 1,
        "stale_pending_count": 1,
        "active_count": 1,
        "stale_active_count": 1,
        "oldest_pending_age_hours": 193.0,
        "oldest_active_age_hours": 73.0,
        "unknown_pending_count": 0,
        "unknown_active_count": 0,
    }
    assert {
        "action": "review_stale_handoff",
        "owner": "operator",
        "reason": "Pending or active handoffs exceeded freshness thresholds or have unknown age.",
    } in result["freshness"]["next_actions"]


async def test_status_freshness_shipped_event_next_actions(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _make_status_health_ready(tmp_path, monkeypatch)
    await _seed_activity(db, "cc", "2026-07-07T11:00:00Z", tags=["SHIPPED"])
    await _seed_activity(
        db, "codex", "2026-07-07T11:00:00Z", tags=["SHIPPED", "PROCESSED"]
    )
    await db.commit()
    _replace_test_anchor(tmp_path / "test.db")

    result = await mod.collect_status_summary(db, now=FIXED_NOW)

    assert result["freshness"]["shipped_events"] == {
        "unprocessed": 1,
        "actionable_unprocessed": 1,
        "dispositioned_unprocessed": 0,
        "processed_without_receipt": 1,
        "next_action": "inspect_receiptless_processed",
    }
    assert result["freshness"]["next_actions"][:2] == [
        {
            "action": "inspect_receiptless_processed",
            "owner": "operator",
            "reason": "Processed SHIPPED rows lack receipt proof.",
        },
        {
            "action": "record_disposition",
            "owner": "operator",
            "reason": "Actionable SHIPPED rows need receipt-backed sync or disposition.",
        },
    ]


async def test_status_freshness_actionable_unprocessed_next_action_without_receiptless(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _make_status_health_ready(tmp_path, monkeypatch)
    await _seed_activity(db, "cc", "2026-07-07T11:00:00Z", tags=["SHIPPED"])
    await db.commit()

    result = await mod.collect_status_summary(db, now=FIXED_NOW)

    assert result["freshness"]["shipped_events"]["next_action"] == "record_disposition"


async def test_status_freshness_counts_dispositioned_unprocessed_shipped(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _make_status_health_ready(tmp_path, monkeypatch)
    activity_id = await _seed_activity(
        db, "cc", "2026-07-07T11:00:00Z", tags=["SHIPPED"]
    )
    await db.execute(
        "UPDATE activity_log SET sync_disposition = 'declined_mapping', "
        "sync_reason = 'no canonical downstream row', sync_disposition_by = 'codex', "
        "synced_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
        (activity_id,),
    )
    await db.commit()

    result = await mod.collect_status_summary(db, now=FIXED_NOW)

    assert result["signals"]["unprocessed_shipped"] == 1
    assert result["signals"]["actionable_unprocessed_shipped"] == 0
    assert result["signals"]["dispositioned_unprocessed_shipped"] == 1
    assert result["freshness"]["shipped_events"] == {
        "unprocessed": 1,
        "actionable_unprocessed": 0,
        "dispositioned_unprocessed": 1,
        "processed_without_receipt": 0,
        "next_action": "none",
    }
    assert not any(
        action["action"] == "record_disposition"
        for action in result["freshness"]["next_actions"]
    )


async def test_status_freshness_malformed_timestamps_are_unknown_without_crashing(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _make_status_health_ready(tmp_path, monkeypatch)
    await _seed_snapshot(db, "cc", "2026-07-07", "not-a-timestamp")
    await _seed_activity(db, "cc", "also-not-a-timestamp")
    await _seed_handoff(db, "pending", "bad-dispatch-time")
    await db.commit()

    result = await mod.collect_status_summary(db, now=FIXED_NOW)

    assert result["freshness"]["snapshots"]["cc"]["state"] == "unknown"
    assert result["freshness"]["snapshots"]["cc"]["age_hours"] is None
    assert (
        result["freshness"]["snapshots"]["cc"]["next_action"] == "cc_refresh_snapshot"
    )
    assert result["freshness"]["activity_sources"]["cc"]["state"] == "unknown"
    assert result["freshness"]["activity_sources"]["cc"]["age_hours"] is None
    assert result["freshness"]["handoffs"]["unknown_pending_count"] == 1
    assert {
        "action": "review_stale_handoff",
        "owner": "operator",
        "reason": "Pending or active handoffs exceeded freshness thresholds or have unknown age.",
    } in result["freshness"]["next_actions"]


async def test_status_freshness_preserves_existing_keys_and_top_level_health(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _make_status_health_ready(tmp_path, monkeypatch)
    await _seed_snapshot(db, "cc", "2026-07-04", "2026-07-04T11:00:00Z")
    await db.commit()
    _replace_test_anchor(tmp_path / "test.db")

    result = await mod.collect_status_summary(db, now=FIXED_NOW)

    for key in (
        "latest_snapshots",
        "latest_activity",
        "signals",
        "row_counts",
        "pending_handoffs_by_trust",
        "ok",
        "overall",
        "storage_health",
        "operating_state",
        "freshness",
    ):
        assert key in result
    assert result["ok"] is True
    assert result["overall"] == "healthy"
    assert result["storage_health"] == "healthy"
    assert result["operating_state"] == "stale"
    assert result["freshness"]["overall"] == "stale"


async def test_status_breaks_latest_ties_by_id(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "test.db").touch()
    bridge = tmp_path / "bridge.md"
    bridge.write_text("# test", encoding="utf-8")
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge)

    fixed_created_at = "2026-04-17T00:00:00Z"
    first_snapshot = '{"active_projects":"old"}'
    second_snapshot = '{"active_projects":"new"}'
    for snapshot_date, data in (
        ("2026-04-17", first_snapshot),
        ("2026-04-18", second_snapshot),
    ):
        cursor = await db.execute(
            "INSERT INTO system_snapshots (system, snapshot_date, data, created_at) "
            "VALUES ('cc', ?, ?, ?)",
            (snapshot_date, data, fixed_created_at),
        )
        snapshot_id = cursor.lastrowid
        assert snapshot_id is not None
        await upsert_fts_entry(
            db, "snapshot", str(snapshot_id), fts_text_for_snapshot(data)
        )

    for project_name in ("old-activity", "new-activity"):
        cursor = await db.execute(
            "INSERT INTO activity_log (source, timestamp, project_name, summary, created_at) "
            "VALUES ('cc', '2026-04-17', ?, 'checked operator status', ?)",
            (project_name, fixed_created_at),
        )
        activity_id = cursor.lastrowid
        assert activity_id is not None
        await upsert_fts_entry(
            db,
            "activity",
            str(activity_id),
            fts_text_for_activity(project_name, "checked operator status", None),
        )
    await db.commit()
    _replace_test_anchor(tmp_path / "test.db")

    result = await fns["status"](ctx=make_ctx(db))

    assert result["ok"] is True
    assert result["latest_snapshots"]["cc"] == "2026-04-18"
    assert result["latest_activity"]["cc"] == "2026-04-17 (new-activity)"


async def test_health_wal_absent_when_no_wal_file(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    """Missing WAL sibling file → size 0, warning False."""
    (tmp_path / "test.db").touch()
    # Ensure no sibling wal file
    wal = tmp_path / "test.db-wal"
    if wal.exists():
        wal.unlink()
    ctx = make_ctx(db)
    result = await fns["health"](ctx=ctx)
    assert result["wal_size_bytes"] == 0
    assert result["wal_warning"] is False


async def test_health_wal_size_reflects_file_size(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    """`wal_size_bytes` mirrors the real size of the sibling WAL file."""
    (tmp_path / "test.db").touch()
    wal = tmp_path / "test.db-wal"
    wal.write_bytes(b"x" * 1024)
    ctx = make_ctx(db)
    result = await fns["health"](ctx=ctx)
    assert result["wal_size_bytes"] == 1024
    assert result["wal_warning"] is False


async def test_health_wal_warning_at_threshold(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wal_warning flips True strictly above the configured threshold."""
    (tmp_path / "test.db").touch()
    monkeypatch.setattr(config, "WAL_SIZE_WARN_BYTES", 100)
    wal = tmp_path / "test.db-wal"

    wal.write_bytes(b"x" * 100)
    result = await fns["health"](ctx=make_ctx(db))
    # At threshold, not above → no warning
    assert result["wal_warning"] is False

    wal.write_bytes(b"x" * 101)
    result = await fns["health"](ctx=make_ctx(db))
    assert result["wal_warning"] is True


async def test_health_ok_unaffected_by_wal_warning(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wal_warning is a soft signal — `ok` stays True on an otherwise-healthy bridge."""
    (tmp_path / "test.db").touch()
    bridge = tmp_path / "bridge.md"
    bridge.write_text("# test")
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge)
    monkeypatch.setattr(config, "WAL_SIZE_WARN_BYTES", 100)
    (tmp_path / "test.db-wal").write_bytes(b"x" * 1024)

    def verified_anchor(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "state": "verified",
            "ready": True,
            "source_current": True,
            "errors": [],
        }

    monkeypatch.setattr(
        mod,
        "recovery_anchor_inventory",
        verified_anchor,
    )

    result = await fns["health"](ctx=make_ctx(db))
    assert result["wal_warning"] is True
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# F8 — claude_ai section drift monitor (bridge file vs DB projection)
# ---------------------------------------------------------------------------


async def _seed_claude_ai_section(
    db: aiosqlite.Connection, section_name: str, content: str
) -> None:
    await db.execute(
        "INSERT INTO context_sections (section_name, owner, content) VALUES (?, 'claude_ai', ?)",
        (section_name, content),
    )
    await upsert_fts_entry(
        db,
        "section",
        section_name,
        fts_text_for_section(section_name, content),
    )
    await db.commit()


async def test_claude_ai_section_drift_in_sync(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_claude_ai_section(db, "career", "Platform Engineer target.")
    bridge = tmp_path / "bridge.md"
    bridge.write_text("## Career & Professional Target\nPlatform Engineer target.\n")
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge)

    result = await fns["health"](ctx=make_ctx(db))
    drift = result["claude_ai_section_drift"]
    assert drift["checked"] is True
    assert drift["in_sync"] is True
    assert drift["state"] == "current"
    assert drift["drifted_sections"] == []
    assert result["projection_health"] == "current"


async def test_projection_health_is_untracked_without_whole_file_export_state(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
) -> None:
    (tmp_path / "test.db").touch()
    await db.execute("DELETE FROM bridge_file_export_state")
    await db.commit()

    result = await fns["health"](ctx=make_ctx(db))
    assert result["claude_ai_section_drift"]["state"] == "current"
    assert result["bridge_file_export_tracked"] is False
    assert result["projection_health"] == "untracked"
    assert result["ok"] is False

    status = await fns["status"](ctx=make_ctx(db))
    assert status["projection_health"] == "untracked"
    assert status["overall"] == "degraded"


async def test_claude_ai_section_drift_preserves_nested_h2_headings(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "# Career\n\n## Current Role\nSenior IT engineer.\n\n## Proof Points\n- bridge-db"
    await _seed_claude_ai_section(db, "career", body)
    bridge = tmp_path / "bridge.md"
    bridge.write_text(
        "## Career & Professional Target\n"
        f"{body}\n\n"
        "## Pending Handoffs\n<!-- none -->\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge)

    result = await fns["health"](ctx=make_ctx(db))
    drift = result["claude_ai_section_drift"]
    assert drift["checked"] is True
    assert drift["in_sync"] is True
    assert drift["drifted_sections"] == []


async def test_claude_ai_section_drift_detects_mismatch(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # DB holds the synced value; the file has an unsynced inbound edit.
    await _seed_claude_ai_section(db, "career", "Synced value.")
    bridge = tmp_path / "bridge.md"
    bridge.write_text("## Career & Professional Target\nHand-edited but unsynced.\n")
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge)
    _replace_test_anchor(tmp_path / "test.db")

    result = await fns["health"](ctx=make_ctx(db))
    drift = result["claude_ai_section_drift"]
    assert drift["checked"] is True
    assert drift["in_sync"] is False
    assert drift["state"] == "drift"
    assert drift["drifted_sections"] == ["career"]

    status = await fns["status"](ctx=make_ctx(db))
    assert status["signals"]["claude_ai_unsynced_sections"] == 1
    assert result["storage_ok"] is True
    assert result["projection_health"] == "drift"
    assert result["ok"] is False
    assert status["ok"] is False
    assert status["overall"] == "degraded"
    assert status["storage_health"] == "healthy"
    assert status["projection_health"] == "drift"


async def test_claude_ai_section_drift_no_file(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", tmp_path / "missing.md")
    result = await fns["health"](ctx=make_ctx(db))
    drift = result["claude_ai_section_drift"]
    assert drift["checked"] is False
    assert drift["in_sync"] is None
    assert drift["state"] == "missing"
    assert drift["drifted_sections"] == []


async def test_claude_ai_section_drift_unreadable_is_unknown_and_non_green(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = tmp_path / "bridge.md"
    bridge.write_text("## Career & Professional Target\ncontent\n", encoding="utf-8")
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge)
    original_read_text = Path.read_text

    def fail_bridge_read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == bridge:
            raise PermissionError("synthetic unreadable bridge")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_bridge_read)

    result = await fns["health"](ctx=make_ctx(db))
    drift = result["claude_ai_section_drift"]
    assert drift == {
        "checked": False,
        "in_sync": None,
        "state": "unreadable",
        "reason": "read_error",
        "drifted_sections": [],
    }
    assert result["storage_ok"] is True
    assert result["projection_health"] == "unreadable"
    assert result["ok"] is False


async def test_claude_ai_section_drift_malformed_markers_are_non_green(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = tmp_path / "bridge.md"
    bridge.write_text(
        "## Career & Professional Target\n"
        "<!-- bridge-db:owned-section:start:career -->\n"
        "content without an end marker\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge)

    result = await fns["health"](ctx=make_ctx(db))
    assert result["claude_ai_section_drift"]["state"] == "unreadable"
    assert result["projection_health"] == "unreadable"
    assert result["ok"] is False


async def test_health_reports_auth_block(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json as _json

    from bridge_db import config as bridge_config
    from bridge_db.tools.health import collect_health_metrics

    principals_path = tmp_path / "principals.json"
    principals_path.write_text(
        _json.dumps({"version": 1, "principals": {"cc": {"token_sha256": "x" * 64}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(bridge_config, "PRINCIPALS_PATH", principals_path)
    monkeypatch.setattr(bridge_config, "AUTH_MODE", "warn")

    metrics = await collect_health_metrics(db)
    assert metrics["auth"] == {
        "mode": "warn",
        "principals_file_exists": True,
        "principals_enrolled": 0,
    }


async def test_health_reports_ledger_and_orphan_metrics(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / "test.db").touch()
    result = await fns["health"](ctx=make_ctx(db))
    assert result["ledger_protected_count"] == 0
    assert result["receipt_orphan_count"] == 0
    assert result["disposition_orphan_count"] == 0


async def test_health_counts_protected_rows(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / "test.db").touch()
    await insert_activity_row(
        db,
        source="cc",
        timestamp="2026-01-01",
        project_name="p",
        summary="durable",
        tags=["LEDGER"],
    )
    await db.commit()
    result = await fns["health"](ctx=make_ctx(db))
    assert result["ledger_protected_count"] == 1
