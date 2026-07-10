"""Tests for the bridge-db CLI helpers."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

import bridge_db.config as cfg
import bridge_db.tools.recall as recall_tool
from bridge_db import auth, config
from bridge_db.__main__ import (
    mark_audit_posture,
    run_dogfood,
    run_enroll,
    run_list_principals,
    run_log_session_boundary,
    run_promote_section,
    run_rebuild_content_index,
    run_reconcile_canonical_keys,
    run_revoke_principal,
    run_status,
)
from bridge_db.db import (
    collect_fts_index_metrics,
    fts_text_for_activity,
    fts_text_for_section,
    fts_text_for_snapshot,
    insert_activity_row,
    open_db,
    upsert_fts_entry,
)

FIXED_NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


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
        fts_text_for_activity("bridge-db", "checked operator status", None),
    )


def test_mark_audit_posture_classifies_legacy_and_blocked_rows() -> None:
    assert (
        mark_audit_posture(
            [
                {
                    "detail": (
                        "activity_ids=[1] updated_ids=[1] missing_ids=[] "
                        "updated=1/1 shipped_bypass_ids=[1]"
                    )
                }
            ]
        )
        == "historical shipped bypass evidence only; no receiptless shipped rows"
    )
    assert (
        mark_audit_posture(
            [
                {
                    "detail": (
                        "activity_ids=[1] updated_ids=[1] missing_ids=[] "
                        "updated=1/1 shipped_bypass_ids=[1]"
                    )
                }
            ],
            processed_shipped_without_receipt=1,
        )
        == "legacy shipped bypass observed"
    )
    assert (
        mark_audit_posture([{"detail": "activity_ids=[1] blocked_shipped_ids=[1]"}])
        == "blocked shipped misuse observed"
    )


@pytest.mark.asyncio
async def test_run_status_reports_healthy_summary(
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
            fts_text_for_activity("bridge-db", "checked operator status", None),
        )
        await db.commit()
    finally:
        await db.close()

    ok = await run_status()
    captured = capsys.readouterr().out

    assert ok is True
    assert "Overall: healthy" in captured
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
            """
            INSERT INTO shipped_event_dispositions (
                activity_id, disposition_type, reason, decided_by
            )
            VALUES (?, 'declined_mapping', 'no canonical downstream row', 'codex')
            """,
            (activity_id,),
        )
        await db.commit()
    finally:
        await db.close()

    ok = await run_status()
    captured = capsys.readouterr().out

    assert ok is True
    assert "unprocessed_shipped=1" in captured
    assert "actionable_unprocessed_shipped=0" in captured
    assert "dispositioned_unprocessed_shipped=1" in captured
    assert "Attention:" not in captured
    assert "confirm_shipped_sync_or_record_disposition" not in captured


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
    assert "Overall: degraded" in captured
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
        await _seed_cli_snapshot(db, "cc", "2026-07-04", "2026-07-04T11:00:00Z")
        await _seed_cli_snapshot(db, "codex", "2026-07-07", "2026-07-07T11:00:00Z")
        await db.commit()
    finally:
        await db.close()

    ok = await run_status(now=FIXED_NOW)
    captured = capsys.readouterr().out

    assert ok is True
    assert "Overall: healthy" in captured
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
        await _seed_cli_snapshot(db, "cc", "2026-07-07", "2026-07-07T11:00:00Z")
        await _seed_cli_snapshot(db, "codex", "2026-07-07", "2026-07-07T11:00:00Z")
        await db.commit()
    finally:
        await db.close()

    ok = await run_status(now=FIXED_NOW)
    captured = capsys.readouterr().out

    assert ok is False
    assert "Overall: degraded" in captured
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
        await _seed_cli_snapshot(db, "cc", "2026-07-07", "2026-07-07T11:00:00Z")
        await _seed_cli_snapshot(db, "codex", "2026-07-07", "2026-07-07T11:00:00Z")
        await _seed_cli_activity(db, "cc", "2026-07-07T11:00:00Z", tags=["SHIPPED"])
        await db.commit()
    finally:
        await db.close()

    ok = await run_status(now=FIXED_NOW)
    captured = capsys.readouterr().out

    assert ok is True
    assert "Freshness: attention" in captured
    assert "confirm_shipped_sync_or_record_disposition (operator)" in captured
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
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-04-17T00:00:00Z",
                        "tool": "confirm_shipped_sync",
                        "caller": "codex",
                        "project": "bridge-db",
                        "ok": True,
                        "detail": "activity_id=1 downstream=notion:abc",
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-04-17T00:01:00Z",
                        "tool": "mark_shipped_processed",
                        "caller": None,
                        "project": None,
                        "ok": True,
                        "detail": (
                            "activity_ids=[1] updated_ids=[1] missing_ids=[] updated=1/1"
                        ),
                    }
                ),
            ]
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
    await db.close()

    ok = await run_dogfood()
    captured = capsys.readouterr().out

    assert ok is True
    assert "bridge-db dogfood" in captured
    assert "dispositioned_unprocessed_shipped=0" in captured
    assert "processed_shipped_without_receipt=0" in captured
    assert "FTS: expected=0, indexed=0, missing=0, orphaned=0" in captured
    assert (
        "Latest confirm_shipped_sync: activity_id=1 downstream=notion:abc" in captured
    )
    assert (
        "Compatibility audit posture: non-shipped compatibility detail current"
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
            "SELECT id, source, timestamp, project_name, summary, tags "
            "FROM activity_log ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["source"] == "cc"
        assert row["timestamp"] == "2026-05-30T12:00:00Z"
        assert row["project_name"] == "bridge-db"
        assert row["summary"] == "CC session ended (7min)"
        assert json.loads(row["tags"]) == ["session-boundary"]

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


@pytest.mark.parametrize(
    ("flag", "expected_text"),
    [
        ("--status", "bridge-db status"),
        ("--doctor", "DB opens (WAL + schema)"),
        ("--dogfood", "bridge-db dogfood"),
    ],
)
def test_cli_entrypoints_smoke(flag: str, expected_text: str, tmp_path: Path) -> None:
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
from bridge_db.db import open_db


async def main() -> None:
    db = await open_db(Path(os.environ["BRIDGE_DB_PATH"]))
    await db.close()


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

    assert result.returncode == 0, result.stderr
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
        assert "Attention:" not in result.stdout


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
    assert data["principals"]["cc"]["token_sha256"] == auth.hash_token(token)
    assert (tmp_path / "principals.json").stat().st_mode & 0o777 == 0o600


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

    assert await run_promote_section("career") is True
    cursor = await db.execute(
        "SELECT source_trust FROM context_sections WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["source_trust"] == "operator"


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
