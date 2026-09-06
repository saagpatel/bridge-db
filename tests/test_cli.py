"""Tests for the bridge-db CLI helpers."""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

import bridge_db.config as cfg
import bridge_db.tools.recall as recall_tool
from bridge_db import auth, config, recovery
from bridge_db.__main__ import (
    status_attention,
    run_apply_owner_delegation_manifest,
    run_cancel_handoff,
    run_create_recovery_anchor,
    run_dogfood,
    run_enroll,
    run_list_principals,
    run_log_session_boundary,
    run_promote_handoff,
    run_promote_section,
    run_rebuild_content_index,
    run_reconcile_canonical_keys,
    run_quarantine_cleared_operator_handoffs,
    run_rotate_recovery_anchor,
    run_restore_handoff_trust,
    run_revoke_principal,
    run_seal_recovery_batch,
    run_status,
    run_upgrade_principals_v2,
    run_verify_recovery_anchor,
)
from bridge_db.db import (
    SCHEMA_VERSION,
    collect_fts_index_metrics,
    fts_text_for_activity,
    fts_text_for_section,
    fts_text_for_snapshot,
    insert_activity_row,
    open_db,
    upsert_fts_entry,
)
from bridge_db.owner_delegation import (
    DELEGATION_MANIFEST_SCHEMA,
    owner_resource_snapshot,
)

FIXED_NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


async def _seed_bridge_export_state(db: aiosqlite.Connection) -> None:
    await db.execute(
        "INSERT INTO bridge_file_export_state (singleton, exported_content_sha256) "
        "VALUES (1, 'tracked-test-projection')"
    )


def _create_cli_recovery_anchor(db_path: Path) -> None:
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )


def _write_cli_principal(
    path: Path,
    *,
    caller: str,
    token: str,
    scopes: list[str] | None = None,
) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "principals": {
                    caller: {
                        "token_sha256": auth.hash_token(token),
                        "issued_at": "2026-07-30T00:00:00Z",
                        "expires_at": "2099-07-30T00:00:00Z",
                        "generation": 1,
                        "scopes": scopes or auth.scopes_for_caller(caller),
                    }
                },
            }
        ),
        encoding="utf-8",
    )


async def _seed_cli_snapshot(
    db: aiosqlite.Connection,
    system: str,
    snapshot_date: str,
    created_at: str,
) -> None:
    data = '{"active_projects":"- bridge-db"}'
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


async def _seed_cli_activity(
    db: aiosqlite.Connection,
    source: str,
    created_at: str,
    tags: list[str] | None = None,
) -> None:
    cursor = await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            source,
            created_at,
            "bridge-db",
            "checked operator status",
            json.dumps(tags or []),
            created_at,
        ),
    )
    activity_id = cursor.lastrowid
    assert activity_id is not None
    await upsert_fts_entry(
        db,
        "activity",
        str(activity_id),
        fts_text_for_activity("bridge-db", "checked operator status", None, tags),
    )


@pytest.mark.asyncio
async def test_run_status_reports_healthy_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "bridge.db"
    bridge_path = tmp_path / "claude_ai_context.md"
    bridge_path.write_text(
        "## Career & Professional Target\nCareer notes\n", encoding="utf-8"
    )

    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "BRIDGE_FILE_PATH", bridge_path)

    db = await open_db(db_path)
    try:
        await _seed_bridge_export_state(db)
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
        cursor = await db.execute(
            "INSERT INTO system_snapshots (system, snapshot_date, data) VALUES (?, ?, ?)",
            ("cc", "2026-04-17", '{"active_projects":"- bridge-db"}'),
        )
        snapshot_id = cursor.lastrowid
        assert snapshot_id is not None
        await upsert_fts_entry(
            db,
            "snapshot",
            str(snapshot_id),
            fts_text_for_snapshot('{"active_projects":"- bridge-db"}'),
        )
        cursor = await db.execute(
            "INSERT INTO activity_log (source, timestamp, project_name, summary, tags) "
            "VALUES (?, ?, ?, ?, ?)",
            ("cc", "2026-04-17", "bridge-db", "checked operator status", '["SHIPPED"]'),
        )
        activity_id = cursor.lastrowid
        assert activity_id is not None
        await upsert_fts_entry(
            db,
            "activity",
            str(activity_id),
            fts_text_for_activity(
                "bridge-db", "checked operator status", None, ["SHIPPED"]
            ),
        )
        await db.commit()
    finally:
        await db.close()
    _create_cli_recovery_anchor(db_path)

    ok = await run_status()
    captured = capsys.readouterr().out

    assert ok is True
    assert "Storage health: healthy" in captured
    assert "Projection health: current" in captured
    assert "Operating state: attention" in captured
    assert "contexts=1" in captured
    assert "pending_handoffs=0" in captured
    assert "unprocessed_shipped=1" in captured
    assert "actionable_unprocessed_shipped=1" in captured
    assert "dispositioned_unprocessed_shipped=0" in captured
    assert "Attention: actionable_unprocessed_shipped=1" in captured
    assert "Pending handoff trust: operator=0, agent=0, ingested=0" in captured
    assert "dogfood will fail until cleared" in captured
    assert "cc=2026-04-17" in captured
    assert '"cc": "2026-04-17 (bridge-db)"' in captured


@pytest.mark.asyncio
async def test_recovery_anchor_cli_creates_once_and_verifies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "bridge.db"
    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    db = await open_db(db_path)
    await db.close()

    assert run_create_recovery_anchor() is True
    created = capsys.readouterr().out
    assert "Result: created" in created
    assert "State: verified" in created
    assert "Digest verified: True" in created
    assert "Semantic readback: True" in created
    events = [
        json.loads(line)
        for line in cfg.AUDIT_LOG_PATH.read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["tool"] == "recovery_anchor.create"
    assert events[-1]["caller"] == "operator-cli"
    assert events[-1]["project"] == "bridge-db"
    assert events[-1]["ok"] is True
    assert "disposition=created" in events[-1]["detail"]
    assert "state=verified" in events[-1]["detail"]
    assert "sha256=" in events[-1]["detail"]

    assert run_create_recovery_anchor() is True
    preserved = capsys.readouterr().out
    assert "Result: preserved_existing" in preserved
    events = [
        json.loads(line)
        for line in cfg.AUDIT_LOG_PATH.read_text(encoding="utf-8").splitlines()
    ]
    assert "disposition=preserved_existing" in events[-1]["detail"]

    assert run_verify_recovery_anchor() is True
    verified = capsys.readouterr().out
    assert "RecoveryAnchorV1 verification" in verified
    assert "State: verified" in verified


@pytest.mark.asyncio
async def test_recovery_anchor_cli_verifies_when_live_database_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "bridge.db"
    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    db = await open_db(db_path)
    await db.close()
    assert run_create_recovery_anchor() is True
    capsys.readouterr()
    db_path.unlink()

    assert run_verify_recovery_anchor() is True
    output = capsys.readouterr().out
    assert "State: verified" in output
    assert "Digest verified: True" in output


@pytest.mark.asyncio
async def test_recovery_anchor_cli_rotates_stale_bundle_and_preserves_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "bridge.db"
    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    db = await open_db(db_path)
    await db.close()
    assert run_create_recovery_anchor() is True
    capsys.readouterr()
    original = recovery.verify_recovery_anchor(
        recovery.recovery_anchor_path(db_path),
        expected_schema_version=SCHEMA_VERSION,
    )
    with sqlite3.connect(db_path) as changed:
        changed.execute(
            "INSERT INTO activity_log "
            "(source, timestamp, project_name, summary) VALUES (?, ?, ?, ?)",
            ("codex", "2026-07-18T09:00:00Z", "bridge-db", "after anchor"),
        )

    assert run_rotate_recovery_anchor() is True
    rotated = capsys.readouterr().out
    assert "RecoveryAnchorV1 rotation" in rotated
    assert "Result: rotated" in rotated
    assert "State: verified" in rotated
    assert "Superseded path:" in rotated
    assert str(original["sha256"]) in rotated
    events = [
        json.loads(line)
        for line in cfg.AUDIT_LOG_PATH.read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["tool"] == "recovery_anchor.rotate"
    assert events[-1]["caller"] == "operator-cli"
    assert events[-1]["ok"] is True
    assert "disposition=rotated" in events[-1]["detail"]
    assert "superseded_path=" in events[-1]["detail"]

    assert run_rotate_recovery_anchor() is True
    preserved = capsys.readouterr().out
    assert "Result: preserved_current" in preserved


@pytest.mark.asyncio
async def test_recovery_batch_seal_cli_requires_bound_scope_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "bridge.db"
    principals_path = tmp_path / "principals.json"
    token = "codex-recovery-seal-token"
    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "PRINCIPALS_PATH", principals_path)
    monkeypatch.setenv("BRIDGE_DB_PRINCIPAL_TOKEN", token)
    _write_cli_principal(
        principals_path,
        caller="codex",
        token=token,
    )
    db = await open_db(db_path)
    await db.close()
    _create_cli_recovery_anchor(db_path)
    with sqlite3.connect(db_path) as changed:
        changed.execute(
            "INSERT INTO activity_log "
            "(source, timestamp, project_name, summary) VALUES (?, ?, ?, ?)",
            ("codex", "2026-07-30", "bridge-db", "completed CLI batch"),
        )

    assert run_seal_recovery_batch("cli-batch-001") is True
    first = capsys.readouterr().out
    assert "RecoverySealReceiptV1" in first
    assert "Batch: cli-batch-001" in first
    assert "Seal owner: codex" in first
    assert "Outcome: recovery_sealed" in first
    assert "Replayed: False" in first
    assert "Digest verified: True" in first
    assert "Semantic readback: True" in first
    assert "Source current: True" in first

    assert run_seal_recovery_batch("cli-batch-001") is True
    second = capsys.readouterr().out
    assert "Outcome: recovery_sealed" in second
    assert "Replayed: True" in second


@pytest.mark.asyncio
async def test_recovery_batch_seal_cli_refuses_unauthorized_principal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "bridge.db"
    principals_path = tmp_path / "principals.json"
    token = "claude-ai-token"
    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "PRINCIPALS_PATH", principals_path)
    monkeypatch.setenv("BRIDGE_DB_PRINCIPAL_TOKEN", token)
    _write_cli_principal(
        principals_path,
        caller="claude_ai",
        token=token,
    )
    db = await open_db(db_path)
    await db.close()
    _create_cli_recovery_anchor(db_path)

    assert run_seal_recovery_batch("unauthorized-cli-001") is False
    output = capsys.readouterr().out
    assert "recovery batch seal refused" in output
    assert "not scoped" in output
    assert not db_path.with_name("bridge.db.recovery-seals-v1").exists()


@pytest.mark.asyncio
async def test_recovery_batch_seal_cli_refuses_legacy_grant_without_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "bridge.db"
    principals_path = tmp_path / "principals.json"
    token = "old-codex-token"
    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "PRINCIPALS_PATH", principals_path)
    monkeypatch.setenv("BRIDGE_DB_PRINCIPAL_TOKEN", token)
    _write_cli_principal(
        principals_path,
        caller="codex",
        token=token,
        scopes=["log_activity"],
    )
    db = await open_db(db_path)
    await db.close()
    _create_cli_recovery_anchor(db_path)

    assert run_seal_recovery_batch("legacy-grant-001") is False
    output = capsys.readouterr().out
    assert "not scoped" in output
    assert not db_path.with_name("bridge.db.recovery-seals-v1").exists()


@pytest.mark.asyncio
async def test_recovery_anchor_cli_refuses_success_when_audit_degrades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "bridge.db"
    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    db = await open_db(db_path)
    await db.close()

    def degraded_audit(
        _tool: str,
        _caller: str | None,
        _project: str | None,
        ok: bool,
        detail: str | None = None,
    ) -> dict[str, object]:
        del ok, detail
        return {"audit_degraded": True}

    monkeypatch.setattr(
        "bridge_db.audit.log_audit",
        degraded_audit,
    )

    assert run_create_recovery_anchor() is False
    output = capsys.readouterr().out
    assert "audit evidence degraded" in output
    assert "RecoveryAnchorV1" not in output


def test_recovery_anchor_cli_fails_closed_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "missing.db")

    assert run_verify_recovery_anchor() is False
    output = capsys.readouterr().out
    assert "State: missing" in output
    assert "anchor_missing" in output


def test_recovery_anchor_cli_fails_closed_for_corrupt_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "bridge.db"
    db_path.write_bytes(b"not a sqlite database")
    monkeypatch.setattr(cfg, "DB_PATH", db_path)

    assert run_create_recovery_anchor() is False
    output = capsys.readouterr().out
    assert "recovery anchor creation refused" in output
    assert "Traceback" not in output


@pytest.mark.asyncio
async def test_run_status_clarifies_dispositioned_unprocessed_shipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "bridge.db"
    bridge_path = tmp_path / "claude_ai_context.md"
    bridge_path.write_text("# bridge\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "BRIDGE_FILE_PATH", bridge_path)

    db = await open_db(db_path)
    try:
        await _seed_bridge_export_state(db)
        cursor = await db.execute(
            "INSERT INTO activity_log (source, timestamp, project_name, summary, tags) "
            "VALUES (?, ?, ?, ?, ?)",
            ("cc", "2026-04-17", "bridge-db", "non-actionable ship", '["SHIPPED"]'),
        )
        activity_id = cursor.lastrowid
        assert activity_id is not None
        await upsert_fts_entry(
            db,
            "activity",
            str(activity_id),
            fts_text_for_activity(
                "bridge-db", "non-actionable ship", None, ["SHIPPED"]
            ),
        )
        await db.execute(
            "UPDATE activity_log SET sync_disposition = 'declined_mapping', "
            "sync_reason = 'no canonical downstream row', "
            "sync_disposition_by = 'codex', "
            "synced_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (activity_id,),
        )
        await db.commit()
    finally:
        await db.close()
    _create_cli_recovery_anchor(db_path)

    ok = await run_status()
    captured = capsys.readouterr().out

    assert ok is True
    assert "unprocessed_shipped=1" in captured
    assert "actionable_unprocessed_shipped=0" in captured
    assert "dispositioned_unprocessed_shipped=1" in captured
    assert "Attention: execution_generation_state=mutable_direct_path" in captured
    assert "Execution generation: state=mutable_direct_path, id=none" in captured
    assert "record_disposition" not in captured


@pytest.mark.asyncio
async def test_run_status_reports_degraded_when_bridge_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "bridge.db"
    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "BRIDGE_FILE_PATH", tmp_path / "missing.md")

    db = await open_db(db_path)
    await db.close()

    ok = await run_status()
    captured = capsys.readouterr().out

    assert ok is False
    assert "Storage health: degraded" in captured
    assert "Operating state: attention" in captured
    assert "exists=False, age=missing" in captured
    assert "Attention: bridge health is degraded" in captured


@pytest.mark.asyncio
async def test_run_status_reports_freshness_attention_without_degrading_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "bridge.db"
    bridge_path = tmp_path / "claude_ai_context.md"
    bridge_path.write_text("# bridge\n", encoding="utf-8")

    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "BRIDGE_FILE_PATH", bridge_path)

    db = await open_db(db_path)
    try:
        await _seed_bridge_export_state(db)
        await _seed_cli_snapshot(db, "cc", "2026-07-04", "2026-07-04T11:00:00Z")
        await _seed_cli_snapshot(db, "codex", "2026-07-07", "2026-07-07T11:00:00Z")
        await db.commit()
    finally:
        await db.close()
    _create_cli_recovery_anchor(db_path)

    ok = await run_status(now=FIXED_NOW)
    captured = capsys.readouterr().out

    assert ok is True
    assert "Storage health: healthy" in captured
    assert "Operating state: stale" in captured
    assert "Freshness: stale" in captured
    assert "Next actions: cc_refresh_snapshot (cc)" in captured


@pytest.mark.asyncio
async def test_run_status_degraded_exit_code_stays_tied_to_bridge_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "bridge.db"

    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "BRIDGE_FILE_PATH", tmp_path / "missing.md")

    db = await open_db(db_path)
    try:
        await _seed_bridge_export_state(db)
        await _seed_cli_snapshot(db, "cc", "2026-07-07", "2026-07-07T11:00:00Z")
        await _seed_cli_snapshot(db, "codex", "2026-07-07", "2026-07-07T11:00:00Z")
        await db.commit()
    finally:
        await db.close()
    _create_cli_recovery_anchor(db_path)

    ok = await run_status(now=FIXED_NOW)
    captured = capsys.readouterr().out

    assert ok is False
    assert "Storage health: degraded" in captured
    assert "Operating state: attention" in captured
    assert "Freshness: attention" in captured


@pytest.mark.asyncio
async def test_run_status_freshness_actions_use_safe_operator_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "bridge.db"
    bridge_path = tmp_path / "claude_ai_context.md"
    bridge_path.write_text("# bridge\n", encoding="utf-8")

    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "BRIDGE_FILE_PATH", bridge_path)

    db = await open_db(db_path)
    try:
        await _seed_bridge_export_state(db)
        await _seed_cli_snapshot(db, "cc", "2026-07-07", "2026-07-07T11:00:00Z")
        await _seed_cli_snapshot(db, "codex", "2026-07-07", "2026-07-07T11:00:00Z")
        await _seed_cli_activity(db, "cc", "2026-07-07T11:00:00Z", tags=["SHIPPED"])
        await db.commit()
    finally:
        await db.close()
    _create_cli_recovery_anchor(db_path)

    ok = await run_status(now=FIXED_NOW)
    captured = capsys.readouterr().out

    assert ok is True
    assert "Freshness: attention" in captured
    assert "record_disposition (operator)" in captured
    assert "mark_shipped_processed" not in captured


@pytest.mark.asyncio
async def test_run_dogfood_reports_read_only_observability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "bridge.db"
    bridge_path = tmp_path / "claude_ai_context.md"
    audit_log_path = tmp_path / "audit.jsonl"
    recall_log_path = tmp_path / "recall_query_log.jsonl"
    bridge_path.write_text("# bridge\n", encoding="utf-8")
    audit_log_path.write_text(
        json.dumps(
            {
                "ts": "2026-04-17T00:00:00Z",
                "tool": "record_disposition",
                "caller": "codex",
                "project": "bridge-db",
                "ok": True,
                "detail": "activity_id=1 disposition=synced downstream=notion:abc",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    recall_log_path.write_text(
        json.dumps(
            {
                "ts": "2026-04-17T00:02:00Z",
                "query": "bridge-db",
                "scope": "activity",
                "limit": 10,
                "n_results": 1,
                "caller": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "BRIDGE_FILE_PATH", bridge_path)
    monkeypatch.setattr(cfg, "AUDIT_LOG_PATH", audit_log_path)
    monkeypatch.setattr(recall_tool, "RECALL_LOG_PATH", recall_log_path)

    db = await open_db(db_path)
    try:
        await _seed_bridge_export_state(db)
        await db.commit()
    finally:
        await db.close()
    _create_cli_recovery_anchor(db_path)

    ok = await run_dogfood()
    captured = capsys.readouterr().out

    assert ok is True
    assert "bridge-db dogfood" in captured
    assert "dispositioned_unprocessed_shipped=0" in captured
    assert "processed_shipped_without_receipt=0" in captured
    assert "FTS: expected=0, indexed=0, missing=0, orphaned=0" in captured
    assert (
        "Latest record_disposition: activity_id=1 disposition=synced downstream=notion:abc"
        in captured
    )


@pytest.mark.asyncio
async def test_rebuild_content_index_repairs_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "bridge.db"
    bridge_path = tmp_path / "claude_ai_context.md"
    bridge_path.write_text("# bridge\n", encoding="utf-8")

    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "BRIDGE_FILE_PATH", bridge_path)

    db = await open_db(db_path)
    try:
        await db.execute(
            "INSERT INTO activity_log (source, timestamp, project_name, summary) "
            "VALUES ('cc', '2026-04-17', 'bridge-db', 'unindexed activity')"
        )
        await db.commit()
    finally:
        await db.close()

    assert await run_status() is False
    degraded = capsys.readouterr().out
    assert "fts_missing=1" in degraded

    assert await run_rebuild_content_index() is True
    rebuilt = capsys.readouterr().out
    assert "activity=1" in rebuilt
    assert "missing=0" in rebuilt
    assert "Overall: healthy" in rebuilt

    assert await run_rebuild_content_index() is True
    idempotent = capsys.readouterr().out
    assert "expected=1, indexed=1, missing=0, orphaned=0" in idempotent


@pytest.mark.asyncio
async def test_reconcile_canonical_keys_cli_reports_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "bridge.db"
    registry_path = tmp_path / "project-registry.json"
    audit_log_path = tmp_path / "audit.jsonl"
    registry_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "canonical_key": "operant-public",
                        "display_name": "operant-public",
                        "repo_full_name": "saagpatel/operant",
                        "bridge_project_names": ["OPERANT"],
                        "aliases": [],
                    }
                ],
                "resolution_overrides": {},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "PROJECT_REGISTRY_PATH", registry_path)
    monkeypatch.setattr(cfg, "AUDIT_LOG_PATH", audit_log_path)

    db = await open_db(db_path)
    try:
        await db.execute(
            "INSERT INTO activity_log (source, timestamp, project_name, summary, canonical_key) "
            "VALUES ('cc', '2026-07-03', 'OPERANT', 'old slug', 'operant-public')"
        )
        await db.commit()
    finally:
        await db.close()

    ok = await run_reconcile_canonical_keys()
    captured = capsys.readouterr().out

    assert ok is True
    assert "bridge-db canonical_key reconcile" in captured
    assert "updated=1" in captured
    assert "disagreements_resolved=1" in captured

    db = await open_db(db_path)
    try:
        row = await (
            await db.execute("SELECT canonical_key FROM activity_log")
        ).fetchone()
        assert row is not None
        assert row["canonical_key"] == "saagpatel/operant"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_log_session_boundary_uses_fts_safe_activity_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "bridge.db"
    bridge_path = tmp_path / "claude_ai_context.md"
    audit_log_path = tmp_path / "audit.jsonl"
    bridge_path.write_text("# bridge\n", encoding="utf-8")

    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "BRIDGE_FILE_PATH", bridge_path)
    monkeypatch.setattr(cfg, "AUDIT_LOG_PATH", audit_log_path)
    token = "cc-session-boundary-token"
    principals_path = tmp_path / "principals.json"
    _write_cli_principal(
        principals_path,
        caller="cc",
        token=token,
        scopes=["log_activity"],
    )
    monkeypatch.setattr(cfg, "PRINCIPALS_PATH", principals_path)
    monkeypatch.setenv("BRIDGE_DB_PRINCIPAL_TOKEN", token)
    registry_path = tmp_path / "project-registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "canonical_key": "bridge-db",
                        "display_name": "bridge-db",
                        "repo_full_name": "example/bridge-db",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg, "PROJECT_REGISTRY_PATH", registry_path)

    db = await open_db(db_path)
    try:
        await insert_activity_row(
            db,
            source="cc",
            timestamp="2026-05-30T11:00:00Z",
            project_name="older-a",
            summary="older activity",
            tags=["SHIPPED"],
            retention_limit=None,
        )
        await insert_activity_row(
            db,
            source="cc",
            timestamp="2026-05-30T11:30:00Z",
            project_name="older-b",
            summary="older activity",
            tags=["SHIPPED"],
            retention_limit=None,
        )
        await db.commit()
    finally:
        await db.close()

    ok = await run_log_session_boundary(
        "bridge-db", duration_minutes="7", timestamp="2026-05-30T12:00:00Z"
    )
    captured = capsys.readouterr().out

    assert ok is True
    assert "bridge-db session boundary" in captured
    assert "missing=0" in captured

    db = await open_db(db_path)
    try:
        cursor = await db.execute(
            "SELECT id, source, timestamp, project_name, summary, tags, canonical_key "
            "FROM activity_log ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["source"] == "cc"
        assert row["timestamp"] == "2026-05-30T12:00:00Z"
        assert row["project_name"] == "bridge-db"
        assert row["summary"] == "CC session ended (7min)"
        assert json.loads(row["tags"]) == ["session-boundary"]
        assert row["canonical_key"] == "example/bridge-db"

        metrics = await collect_fts_index_metrics(db)
        assert metrics["ok"] is True
        assert metrics["expected"] == 3
        assert metrics["indexed"] == 3

        cursor = await db.execute(
            "SELECT COUNT(*) FROM activity_log WHERE source = 'cc'"
        )
        count_row = await cursor.fetchone()
        assert count_row is not None
        assert count_row[0] == 3

        cursor = await db.execute(
            "SELECT COUNT(*) FROM content_index "
            "WHERE source_type = 'activity' AND source_id = ? "
            "AND content_index MATCH 'bridge'",
            (str(row["id"]),),
        )
        match_row = await cursor.fetchone()
        assert match_row is not None
        assert match_row[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_log_session_boundary_rejects_non_cc_principal_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "bridge.db"
    token = "codex-session-boundary-token"
    principals_path = tmp_path / "principals.json"
    _write_cli_principal(
        principals_path,
        caller="codex",
        token=token,
        scopes=["log_activity"],
    )
    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "PRINCIPALS_PATH", principals_path)
    monkeypatch.setenv("BRIDGE_DB_PRINCIPAL_TOKEN", token)

    ok = await run_log_session_boundary("bridge-db")

    assert ok is False
    assert "bound principal is 'codex', required 'cc'" in capsys.readouterr().out
    assert not db_path.exists()


@pytest.mark.parametrize(
    ("flag", "expected_text", "expected_returncode"),
    [
        ("--status", "bridge-db status", 1),
        ("--doctor", "DB opens (WAL + schema)", 0),
        ("--dogfood", "bridge-db dogfood", 1),
    ],
)
def test_cli_entrypoints_smoke(
    flag: str,
    expected_text: str,
    expected_returncode: int,
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "bridge.db"
    bridge_path = tmp_path / "claude_ai_context.md"
    audit_log_path = tmp_path / "audit.log"
    bridge_path.write_text("# bridge\n", encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")
    env["BRIDGE_DB_PATH"] = str(db_path)
    env["BRIDGE_FILE_PATH"] = str(bridge_path)
    env["BRIDGE_DB_AUDIT_LOG_PATH"] = str(audit_log_path)

    bootstrap = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import asyncio
import os
from pathlib import Path
from bridge_db import recovery
from bridge_db.db import SCHEMA_VERSION, open_db


async def main() -> None:
    db = await open_db(Path(os.environ["BRIDGE_DB_PATH"]))
    await db.execute(
        "INSERT INTO bridge_file_export_state (singleton, exported_content_sha256) "
        "VALUES (1, 'tracked-test-projection')"
    )
    await db.commit()
    await db.close()
    recovery.create_recovery_anchor(
        Path(os.environ["BRIDGE_DB_PATH"]),
        expected_schema_version=SCHEMA_VERSION,
    )


asyncio.run(main())
""",
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bootstrap.returncode == 0, bootstrap.stderr

    legacy_env = env.copy()
    legacy_env.pop("BRIDGE_DB_PATH")
    legacy_env.pop("BRIDGE_FILE_PATH")
    legacy_env.pop("BRIDGE_DB_AUDIT_LOG_PATH")
    legacy_env["HOME"] = str(tmp_path / "legacy-home")
    legacy_env["DB_PATH"] = str(db_path)
    legacy_env["AUDIT_LOG_PATH"] = str(audit_log_path)

    legacy_result = subprocess.run(
        [sys.executable, "-m", "bridge_db", flag],
        cwd=repo_root,
        env=legacy_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert legacy_result.returncode != 0

    result = subprocess.run(
        [sys.executable, "-m", "bridge_db", flag],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == expected_returncode, result.stderr
    assert expected_text in result.stdout
    if flag == "--doctor":
        assert str(db_path) in result.stdout
        assert str(audit_log_path) in result.stdout
        assert "Verify the current tool count from source" in (
            repo_root / "README.md"
        ).read_text(encoding="utf-8")
        assert "do not hardcode the current test count" in (
            repo_root / "CLAUDE.md"
        ).read_text(encoding="utf-8")
    if flag == "--status":
        assert "contexts=0" in result.stdout
        assert "readiness=missing" in result.stdout
        assert "Execution generation: state=mutable_direct_path" in result.stdout


def test_enroll_writes_hashed_token_with_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "PRINCIPALS_PATH", tmp_path / "principals.json")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    assert run_enroll("cc") is True
    out = capsys.readouterr().out
    token_line = [line for line in out.splitlines() if line.startswith("  token: ")]
    assert len(token_line) == 1
    token = token_line[0].removeprefix("  token: ").strip()

    data = json.loads((tmp_path / "principals.json").read_text(encoding="utf-8"))
    assert data["version"] == 2
    assert data["principals"]["cc"]["token_sha256"] == auth.hash_token(token)
    assert data["principals"]["cc"]["generation"] == 1
    assert data["principals"]["cc"]["scopes"] == sorted(auth.scopes_for_caller("cc"))
    issued = datetime.fromisoformat(
        data["principals"]["cc"]["issued_at"].replace("Z", "+00:00")
    )
    expires = datetime.fromisoformat(
        data["principals"]["cc"]["expires_at"].replace("Z", "+00:00")
    )
    assert (expires - issued).days == 90
    assert (tmp_path / "principals.json").stat().st_mode & 0o777 == 0o600


def test_enroll_accepts_read_only_hermes_principal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "PRINCIPALS_PATH", tmp_path / "principals.json")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    assert run_enroll("hermes") is True
    output = capsys.readouterr().out
    token = next(
        line.removeprefix("  token: ").strip()
        for line in output.splitlines()
        if line.startswith("  token: ")
    )
    data = json.loads((tmp_path / "principals.json").read_text(encoding="utf-8"))
    entry = data["principals"]["hermes"]
    assert entry["token_sha256"] == auth.hash_token(token)
    assert entry["generation"] == 1
    assert entry["scopes"] == []


def test_upgrade_principals_v2_preserves_hashes_and_adds_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "principals.json"
    old_hash = auth.hash_token("token-cc")
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "principals": {
                    "cc": {
                        "token_sha256": old_hash,
                        "enrolled_at": "2026-06-12T00:00:00Z",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PRINCIPALS_PATH", path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def confirm_upgrade(_prompt: str) -> str:
        return "upgrade"

    monkeypatch.setattr("builtins.input", confirm_upgrade)

    assert run_upgrade_principals_v2() is True

    data = json.loads(path.read_text(encoding="utf-8"))
    entry = data["principals"]["cc"]
    assert data["version"] == 2
    assert entry["token_sha256"] == old_hash
    assert entry["generation"] == 1
    assert entry["scopes"] == sorted(auth.scopes_for_caller("cc"))
    assert path.with_name("principals.json.pre-v2.bak").exists()


@pytest.mark.asyncio
async def test_cancel_handoff_requires_exact_operator_ceremony(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursor = await db.execute(
        "INSERT INTO pending_handoffs (project_name, source_trust) VALUES (?, ?)",
        ("CancelMe", "agent"),
    )
    handoff_id = cursor.lastrowid
    assert handoff_id is not None
    await db.commit()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def confirm_cancel(_prompt: str) -> str:
        return "cancel"

    monkeypatch.setattr("builtins.input", confirm_cancel)

    assert await run_cancel_handoff(handoff_id, "superseded by operator") is True

    row = await (
        await db.execute(
            "SELECT status FROM pending_handoffs WHERE id = ?", (handoff_id,)
        )
    ).fetchone()
    assert row is not None and row["status"] == "cleared"
    receipt = await (
        await db.execute(
            "SELECT reason, previous_status FROM handoff_cancellation_receipts "
            "WHERE handoff_id = ?",
            (handoff_id,),
        )
    ).fetchone()
    assert receipt is not None
    assert receipt["reason"] == "superseded by operator"
    assert receipt["previous_status"] == "pending"


@pytest.mark.asyncio
async def test_cancel_handoff_refuses_claimed_active_work(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursor = await db.execute(
        "INSERT INTO pending_handoffs "
        "(project_name, status, claimed_by, source_trust) VALUES (?, 'active', 'codex', 'operator')",
        ("ActiveWork",),
    )
    handoff_id = cursor.lastrowid
    assert handoff_id is not None
    await db.commit()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    assert await run_cancel_handoff(handoff_id, "should not override") is False
    row = await (
        await db.execute(
            "SELECT status FROM pending_handoffs WHERE id = ?", (handoff_id,)
        )
    ).fetchone()
    assert row is not None and row["status"] == "active"


@pytest.mark.asyncio
async def test_recover_orphaned_handoff_requires_expiry_and_records_receipt(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_db.__main__ import run_recover_orphaned_handoff

    cursor = await db.execute(
        "INSERT INTO pending_handoffs "
        "(project_name, status, claimed_by, source_trust) "
        "VALUES (?, 'active', 'codex', 'operator')",
        ("OrphanedWork",),
    )
    handoff_id = cursor.lastrowid
    assert handoff_id is not None
    await db.execute(
        """
        INSERT INTO handoff_session_capabilities (
            handoff_id, session_id, token_sha256, claimed_caller,
            allowed_transition, issued_at, expires_at
        ) VALUES (?, 'session-orphan', ?, 'codex', 'clear',
                  '2026-08-01T00:00:00Z', '2026-08-02T00:00:00Z')
        """,
        (handoff_id, "a" * 64),
    )
    await db.commit()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "bridge_db.clock._provider", lambda: datetime(2026, 8, 5, tzinfo=UTC)
    )

    def confirm_recover(_prompt: str) -> str:
        return "recover"

    monkeypatch.setattr("builtins.input", confirm_recover)

    assert (
        await run_recover_orphaned_handoff(handoff_id, "claiming session vanished")
        is True
    )
    row = await (
        await db.execute(
            "SELECT status FROM pending_handoffs WHERE id = ?", (handoff_id,)
        )
    ).fetchone()
    assert row is not None and row["status"] == "cleared"
    capability = await (
        await db.execute(
            "SELECT recovered_at, consumed_at FROM handoff_session_capabilities "
            "WHERE handoff_id = ?",
            (handoff_id,),
        )
    ).fetchone()
    assert capability is not None
    assert capability["recovered_at"] is not None
    assert capability["consumed_at"] is None
    receipt = await (
        await db.execute(
            "SELECT reason, recovery_basis, previous_claimant, claim_session_id "
            "FROM handoff_orphan_recovery_receipts WHERE handoff_id = ?",
            (handoff_id,),
        )
    ).fetchone()
    assert receipt is not None
    assert receipt["reason"] == "claiming session vanished"
    assert receipt["recovery_basis"] == "expired_capability"
    assert receipt["previous_claimant"] == "codex"
    assert receipt["claim_session_id"] == "session-orphan"


@pytest.mark.asyncio
async def test_recover_orphaned_handoff_refuses_live_capability(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_db.__main__ import run_recover_orphaned_handoff

    cursor = await db.execute(
        "INSERT INTO pending_handoffs "
        "(project_name, status, claimed_by, source_trust) "
        "VALUES (?, 'active', 'cc', 'operator')",
        ("LiveWork",),
    )
    handoff_id = cursor.lastrowid
    assert handoff_id is not None
    await db.execute(
        """
        INSERT INTO handoff_session_capabilities (
            handoff_id, session_id, token_sha256, claimed_caller,
            allowed_transition, issued_at, expires_at
        ) VALUES (?, 'session-live', ?, 'cc', 'clear',
                  '2026-08-01T00:00:00Z', '2026-08-10T00:00:00Z')
        """,
        (handoff_id, "b" * 64),
    )
    await db.commit()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "bridge_db.clock._provider", lambda: datetime(2026, 8, 5, tzinfo=UTC)
    )

    assert await run_recover_orphaned_handoff(handoff_id, "too soon") is False
    row = await (
        await db.execute(
            "SELECT status FROM pending_handoffs WHERE id = ?", (handoff_id,)
        )
    ).fetchone()
    assert row is not None and row["status"] == "active"


@pytest.mark.asyncio
async def test_recover_orphaned_handoff_rechecks_expiry_under_lock(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_db.__main__ import run_recover_orphaned_handoff

    cursor = await db.execute(
        "INSERT INTO pending_handoffs "
        "(project_name, status, claimed_by, source_trust) "
        "VALUES (?, 'active', 'codex', 'operator')",
        ("ClockCorrectedWork",),
    )
    handoff_id = cursor.lastrowid
    assert handoff_id is not None
    await db.execute(
        """
        INSERT INTO handoff_session_capabilities (
            handoff_id, session_id, token_sha256, claimed_caller,
            allowed_transition, issued_at, expires_at
        ) VALUES (?, 'session-clock-corrected', ?, 'codex', 'clear',
                  '2026-08-01T00:00:00Z', '2026-08-02T00:00:00Z')
        """,
        (handoff_id, "c" * 64),
    )
    await db.commit()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    observed_times = iter(
        [
            datetime(2026, 8, 5, tzinfo=UTC),
            datetime(2026, 8, 1, tzinfo=UTC),
        ]
    )
    monkeypatch.setattr("bridge_db.clock._provider", lambda: next(observed_times))

    def confirm_recover(_prompt: str) -> str:
        return "recover"

    monkeypatch.setattr("builtins.input", confirm_recover)

    assert await run_recover_orphaned_handoff(handoff_id, "clock corrected") is False
    row = await (
        await db.execute(
            "SELECT status FROM pending_handoffs WHERE id = ?", (handoff_id,)
        )
    ).fetchone()
    assert row is not None and row["status"] == "active"
    capability = await (
        await db.execute(
            "SELECT recovered_at FROM handoff_session_capabilities "
            "WHERE handoff_id = ?",
            (handoff_id,),
        )
    ).fetchone()
    assert capability is not None and capability["recovered_at"] is None
    receipt = await (
        await db.execute(
            "SELECT 1 FROM handoff_orphan_recovery_receipts WHERE handoff_id = ?",
            (handoff_id,),
        )
    ).fetchone()
    assert receipt is None


@pytest.mark.asyncio
async def test_recover_orphaned_handoff_binds_full_capability_row(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_db.__main__ import run_recover_orphaned_handoff

    cursor = await db.execute(
        "INSERT INTO pending_handoffs "
        "(project_name, status, claimed_by, source_trust) "
        "VALUES (?, 'active', 'codex', 'operator')",
        ("CapabilityChangedWork",),
    )
    handoff_id = cursor.lastrowid
    assert handoff_id is not None
    await db.execute(
        """
        INSERT INTO handoff_session_capabilities (
            handoff_id, session_id, token_sha256, claimed_caller,
            allowed_transition, issued_at, expires_at
        ) VALUES (?, 'session-capability-changed', ?, 'codex', 'clear',
                  '2026-08-01T00:00:00Z', '2026-08-02T00:00:00Z')
        """,
        (handoff_id, "d" * 64),
    )
    await db.commit()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "bridge_db.clock._provider", lambda: datetime(2026, 8, 5, tzinfo=UTC)
    )

    def mutate_then_confirm(_prompt: str) -> str:
        with sqlite3.connect(tmp_path / "test.db") as concurrent:
            concurrent.execute(
                "UPDATE handoff_session_capabilities SET token_sha256 = ? "
                "WHERE handoff_id = ?",
                ("e" * 64, handoff_id),
            )
        return "recover"

    monkeypatch.setattr("builtins.input", mutate_then_confirm)

    assert await run_recover_orphaned_handoff(handoff_id, "capability changed") is False
    row = await (
        await db.execute(
            "SELECT status FROM pending_handoffs WHERE id = ?", (handoff_id,)
        )
    ).fetchone()
    assert row is not None and row["status"] == "active"
    receipt = await (
        await db.execute(
            "SELECT 1 FROM handoff_orphan_recovery_receipts WHERE handoff_id = ?",
            (handoff_id,),
        )
    ).fetchone()
    assert receipt is None


@pytest.mark.asyncio
async def test_quarantine_and_exact_restore_preserve_recovery_evidence(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursor = await db.execute(
        "INSERT INTO pending_handoffs "
        "(project_name, status, source_trust, cleared_at) "
        "VALUES (?, 'cleared', 'operator', '2026-07-01T00:00:00Z')",
        ("LegacyReviewed",),
    )
    handoff_id = cursor.lastrowid
    assert handoff_id is not None
    await db.commit()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    confirmations = iter(["quarantine", "restore"])

    def confirm_recovery(_prompt: str) -> str:
        return next(confirmations)

    monkeypatch.setattr("builtins.input", confirm_recovery)

    assert await run_quarantine_cleared_operator_handoffs() is True
    row = await (
        await db.execute(
            "SELECT source_trust FROM pending_handoffs WHERE id = ?", (handoff_id,)
        )
    ).fetchone()
    assert row is not None and row["source_trust"] == "ingested"
    recovery = await (
        await db.execute(
            "SELECT row_json, row_sha256, restored_at FROM handoff_trust_quarantine "
            "WHERE handoff_id = ?",
            (handoff_id,),
        )
    ).fetchone()
    assert recovery is not None
    assert recovery["restored_at"] is None

    assert await run_restore_handoff_trust(handoff_id) is True
    restored = await (
        await db.execute(
            "SELECT source_trust FROM pending_handoffs WHERE id = ?", (handoff_id,)
        )
    ).fetchone()
    assert restored is not None and restored["source_trust"] == "operator"


@pytest.mark.asyncio
async def test_apply_owner_delegation_manifest_requires_exact_tty_ceremony(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursor = await db.execute(
        "INSERT INTO activity_log "
        "(source, timestamp, project_name, summary, tags) "
        "VALUES ('cc', '2026-08-23', 'DelegatedCLI', 'shipped', '[\"SHIPPED\"]')"
    )
    assert cursor.lastrowid is not None
    activity_id = int(cursor.lastrowid)
    await db.commit()
    snapshot = await owner_resource_snapshot(
        db, resource_type="activity_disposition", resource_id=activity_id
    )
    manifest_path = tmp_path / "delegation.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": DELEGATION_MANIFEST_SCHEMA,
                "delegated_to": "codex",
                "authorization_reason": "Operator approved exact lifecycle takeover",
                "authorization_ref": "codex-task:test-cli-delegation",
                "resources": [
                    {
                        "resource_type": "activity_disposition",
                        "resource_id": activity_id,
                        "original_owner": "cc",
                        "resource_sha256": snapshot["resource_sha256"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def confirm_delegation(_prompt: str) -> str:
        return "delegate 1 to codex"

    monkeypatch.setattr("builtins.input", confirm_delegation)

    assert await run_apply_owner_delegation_manifest(str(manifest_path)) is True
    stored = await (
        await db.execute(
            "SELECT original_owner, delegated_to, resource_id "
            "FROM owner_delegations"
        )
    ).fetchone()
    assert stored is not None
    assert dict(stored) == {
        "original_owner": "cc",
        "delegated_to": "codex",
        "resource_id": activity_id,
    }
    source = await (
        await db.execute("SELECT source FROM activity_log WHERE id = ?", (activity_id,))
    ).fetchone()
    assert source is not None and source["source"] == "cc"


def test_enroll_refuses_without_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "PRINCIPALS_PATH", tmp_path / "principals.json")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert run_enroll("cc") is False
    assert not (tmp_path / "principals.json").exists()


def test_enroll_rejects_unknown_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "PRINCIPALS_PATH", tmp_path / "principals.json")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert run_enroll("mallory") is False


def test_revoke_removes_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "PRINCIPALS_PATH", tmp_path / "principals.json")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    run_enroll("cc")
    assert run_revoke_principal("cc") is True
    data = json.loads((tmp_path / "principals.json").read_text(encoding="utf-8"))
    assert "cc" not in data["principals"]


def test_list_principals_shows_enrolled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "PRINCIPALS_PATH", tmp_path / "principals.json")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    run_enroll("cc")
    capsys.readouterr()  # discard enroll output
    assert run_list_principals() is True
    out = capsys.readouterr().out
    assert "cc" in out


@pytest.mark.asyncio
async def test_promote_section_sets_operator_label(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_db.tools.context import (
        _upsert_section,  # pyright: ignore[reportPrivateUsage]
    )

    await _upsert_section(
        db=db,
        section_name="career",
        owner="claude_ai",
        content="reviewed content",
        source_trust="ingested",
        attempted_by="claude_ai",
        operation="update_section",
    )
    await db.commit()
    # run_promote_section opens its own connection to the same file the `db`
    # fixture created (tmp_path / "test.db"); WAL mode permits both.
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def confirm(_prompt: str) -> str:
        return "yes"

    monkeypatch.setattr("builtins.input", confirm)

    assert await run_promote_section("career") is True
    cursor = await db.execute(
        "SELECT source_trust FROM context_sections WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["source_trust"] == "operator"


@pytest.mark.asyncio
@pytest.mark.parametrize("increment_version", [True, False])
async def test_promote_section_rejects_content_changed_after_review(
    db: aiosqlite.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    increment_version: bool,
) -> None:
    from bridge_db.tools.context import (
        _upsert_section,  # pyright: ignore[reportPrivateUsage]
    )

    await _upsert_section(
        db=db,
        section_name="career",
        owner="claude_ai",
        content="reviewed content",
        source_trust="ingested",
        attempted_by="sync_from_file",
        operation="sync_from_file",
    )
    await db.commit()
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def mutate_then_confirm(_prompt: str) -> str:
        with sqlite3.connect(db_path) as concurrent:
            if increment_version:
                concurrent.execute(
                    "UPDATE context_sections SET content = ?, source_trust = ?, "
                    "version = version + 1 WHERE section_name = ?",
                    ("replacement content", "ingested", "career"),
                )
            else:
                concurrent.execute(
                    "UPDATE context_sections SET content = ?, source_trust = ? "
                    "WHERE section_name = ?",
                    ("replacement content", "ingested", "career"),
                )
        return "yes"

    monkeypatch.setattr("builtins.input", mutate_then_confirm)

    assert await run_promote_section("career") is False
    cursor = await db.execute(
        "SELECT content, source_trust, version FROM context_sections "
        "WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["content"] == "replacement content"
    assert row["source_trust"] == "ingested"
    assert row["version"] == (2 if increment_version else 1)


@pytest.mark.asyncio
async def test_promote_handoff_confirms_exact_pending_row(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursor = await db.execute(
        "INSERT INTO pending_handoffs "
        "(project_name, project_path, phase, source_trust) VALUES (?, ?, ?, ?)",
        ("ReviewedProject", "/tmp/reviewed", "Phase 2", "agent"),
    )
    handoff_id = cursor.lastrowid
    assert handoff_id is not None
    await db.commit()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def confirm(_prompt: str) -> str:
        return "yes"

    monkeypatch.setattr("builtins.input", confirm)

    assert await run_promote_handoff(handoff_id) is True

    cursor = await db.execute(
        "SELECT status, source_trust FROM pending_handoffs WHERE id = ?",
        (handoff_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == "pending"
    assert row["source_trust"] == "operator"
    events = [
        json.loads(line)
        for line in config.AUDIT_LOG_PATH.read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["tool"] == "auth.promote_handoff"
    assert events[-1]["caller"] == "operator-cli"
    assert f"handoff_id={handoff_id}" in events[-1]["detail"]
    assert "sha256=" in events[-1]["detail"]


@pytest.mark.asyncio
async def test_promote_handoff_rejects_state_changed_after_review(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursor = await db.execute(
        "INSERT INTO pending_handoffs (project_name, phase, source_trust) "
        "VALUES (?, ?, ?)",
        ("RacedProject", "Phase 1", "agent"),
    )
    handoff_id = cursor.lastrowid
    assert handoff_id is not None
    await db.commit()
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def mutate_then_confirm(_prompt: str) -> str:
        with sqlite3.connect(db_path) as concurrent:
            concurrent.execute(
                "UPDATE pending_handoffs SET phase = ? WHERE id = ?",
                ("Phase changed", handoff_id),
            )
        return "yes"

    monkeypatch.setattr("builtins.input", mutate_then_confirm)
    assert await run_promote_handoff(handoff_id) is False

    cursor = await db.execute(
        "SELECT phase, source_trust FROM pending_handoffs WHERE id = ?",
        (handoff_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["phase"] == "Phase changed"
    assert row["source_trust"] == "agent"


@pytest.mark.asyncio
async def test_promote_handoff_refuses_without_tty(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursor = await db.execute(
        "INSERT INTO pending_handoffs (project_name, source_trust) VALUES (?, ?)",
        ("NoTTY", "agent"),
    )
    handoff_id = cursor.lastrowid
    assert handoff_id is not None
    await db.commit()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert await run_promote_handoff(handoff_id) is False
    cursor = await db.execute(
        "SELECT source_trust FROM pending_handoffs WHERE id = ?",
        (handoff_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["source_trust"] == "agent"


def test_enroll_rotation_replaces_old_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "PRINCIPALS_PATH", tmp_path / "principals.json")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    run_enroll("cc")
    first = json.loads((tmp_path / "principals.json").read_text(encoding="utf-8"))
    run_enroll("cc")
    second = json.loads((tmp_path / "principals.json").read_text(encoding="utf-8"))

    assert (
        first["principals"]["cc"]["token_sha256"]
        != second["principals"]["cc"]["token_sha256"]
    )
    assert len(second["principals"]) == 1
    out = capsys.readouterr().out
    assert "rotated" in out


def test_revoke_unknown_caller_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "PRINCIPALS_PATH", tmp_path / "principals.json")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert run_revoke_principal("codex") is False
    assert "no enrollment found" in capsys.readouterr().out


def test_enroll_recovers_from_malformed_principals_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    principals_path = tmp_path / "principals.json"
    principals_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(config, "PRINCIPALS_PATH", principals_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    assert run_enroll("cc") is True
    out = capsys.readouterr().out
    assert "malformed principals file" in out
    data = json.loads(principals_path.read_text(encoding="utf-8"))
    assert "cc" in data["principals"]


def _attention_summary(**signal_overrides: object) -> dict[str, object]:
    """A green status summary; overrides switch on individual signals."""
    signals: dict[str, object] = {
        "pending_handoffs": 0,
        "actionable_unprocessed_shipped": 0,
        "processed_shipped_without_receipt": 0,
        "unacknowledged_snapshot_refusals": 0,
        "execution_generation_state": "verified",
        "tenancy_state": "verified",
        "fts_missing": 0,
        "fts_orphaned": 0,
        "fts_content_mismatched": 0,
        "audit_degraded": False,
        "evidence_disposition_degraded": False,
    }
    signals.update(signal_overrides)
    return {"ok": True, "signals": signals}


def teststatus_attention_is_none_when_every_signal_is_clear() -> None:
    assert status_attention(_attention_summary()) is None


def teststatus_attention_claims_dogfood_failure_only_for_a_real_gate() -> None:
    attention = status_attention(
        _attention_summary(actionable_unprocessed_shipped=2)
    )

    assert attention is not None
    assert "actionable_unprocessed_shipped=2" in attention
    assert attention.endswith("dogfood will fail until cleared")


def teststatus_attention_does_not_claim_dogfood_failure_for_advisory_signals() -> None:
    """Regression: --dogfood exits 0 with these set, so the claim was false.

    execution_generation_state is not one of run_dogfood's gates. Running the
    CLI from a git checkout rather than an immutable release reports
    mutable_direct_path forever, which previously told every reader that
    dogfood would fail when it exits 0.
    """
    attention = status_attention(
        _attention_summary(
            execution_generation_state="mutable_direct_path",
            tenancy_state="unverified",
            unacknowledged_snapshot_refusals=1,
        )
    )

    assert attention is not None
    assert "execution_generation_state=mutable_direct_path" in attention
    assert "tenancy_state=unverified" in attention
    assert "unacknowledged_snapshot_refusals=1" in attention
    assert "dogfood will fail" not in attention
    assert attention.endswith("advisory; dogfood does not gate on these")


def teststatus_attention_prefers_the_gate_claim_when_signals_are_mixed() -> None:
    attention = status_attention(
        _attention_summary(
            execution_generation_state="mutable_direct_path",
            fts_missing=3,
        )
    )

    assert attention is not None
    assert "execution_generation_state=mutable_direct_path" in attention
    assert "fts_missing=3" in attention
    assert attention.endswith("dogfood will fail until cleared")
