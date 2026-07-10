"""Tests for activity log tools."""

import json
import os
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx
from mcp.server.fastmcp.exceptions import ToolError

from bridge_db import config
from bridge_db.db import collect_fts_index_metrics, insert_activity_row
from bridge_db.tools import activity as mod


@pytest.fixture
def fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    mod.register(cap)
    return cap.fns


async def test_log_activity_inserts_row(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    result = await fns["log_activity"](
        caller="cc",
        project_name="TestProject",
        summary="did stuff",
        branch="feat/test",
        tags=["SHIPPED"],
        timestamp="2026-04-14",
        ctx=ctx,
    )
    assert result["ok"] is True

    cursor = await db.execute("SELECT * FROM activity_log")
    rows: list[aiosqlite.Row] = await cursor.fetchall()  # type: ignore[assignment]
    assert len(rows) == 1
    assert rows[0]["source"] == "cc"
    assert rows[0]["project_name"] == "TestProject"
    assert json.loads(rows[0]["tags"]) == ["SHIPPED"]


async def test_log_activity_persists_and_echoes_source_trust(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    asserted = await fns["log_activity"](
        caller="cc", project_name="A", summary="s", source_trust="operator", ctx=ctx
    )
    defaulted = await fns["log_activity"](
        caller="cc", project_name="B", summary="s", ctx=ctx
    )

    assert asserted["source_trust"] == "operator"
    assert defaulted["source_trust"] == "agent"

    cursor = await db.execute(
        "SELECT project_name, source_trust FROM activity_log ORDER BY id"
    )
    rows: list[aiosqlite.Row] = await cursor.fetchall()  # type: ignore[assignment]
    trust = {r["project_name"]: r["source_trust"] for r in rows}
    assert trust["A"] == "operator"
    assert trust["B"] == "agent"


async def test_insert_activity_row_defaults_source_trust_agent(
    db: aiosqlite.Connection,
) -> None:
    """The shared helper (used by the --log-session-boundary hook) defaults to 'agent'."""
    await insert_activity_row(
        db,
        source="cc",
        timestamp="2026-06-10",
        project_name="boundary",
        summary="session end",
    )
    await db.commit()
    cursor = await db.execute(
        "SELECT source_trust FROM activity_log WHERE project_name = 'boundary'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["source_trust"] == "agent"


async def test_log_activity_defaults_timestamp_to_today(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    from datetime import date

    ctx = make_ctx(db)
    await fns["log_activity"](caller="cc", project_name="P", summary="s", ctx=ctx)
    cursor = await db.execute("SELECT timestamp FROM activity_log")
    row = await cursor.fetchone()
    assert row is not None
    assert row["timestamp"] == str(date.today())


async def test_get_recent_activity_filters_by_source(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](caller="cc", project_name="A", summary="s", ctx=ctx)
    await fns["log_activity"](caller="codex", project_name="B", summary="s", ctx=ctx)

    cc_only = await fns["get_recent_activity"](source="cc", ctx=ctx)
    assert len(cc_only) == 1
    assert cc_only[0]["source"] == "cc"

    all_items = await fns["get_recent_activity"](ctx=ctx)
    assert len(all_items) == 2


async def test_get_recent_activity_filters_by_since(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc", project_name="Old", summary="s", timestamp="2026-01-01", ctx=ctx
    )
    await fns["log_activity"](
        caller="cc", project_name="New", summary="s", timestamp="2026-04-01", ctx=ctx
    )
    await db.execute(
        """
        UPDATE activity_log
        SET created_at = timestamp || 'T00:00:00Z'
        WHERE project_name IN ('Old', 'New')
        """
    )
    await db.commit()

    recent = await fns["get_recent_activity"](since="2026-03-01", ctx=ctx)
    assert len(recent) == 1
    assert recent[0]["project_name"] == "New"


async def test_get_recent_activity_since_includes_recently_created_prior_date(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="codex",
        project_name="PreviousDay",
        summary="old insert",
        timestamp="2026-06-20",
        ctx=ctx,
    )
    await fns["log_activity"](
        caller="codex",
        project_name="MidnightCloseout",
        summary="closeout inserted after UTC midnight",
        timestamp="2026-06-20",
        ctx=ctx,
    )
    await db.execute(
        "UPDATE activity_log SET created_at = ? WHERE project_name = ?",
        ("2026-06-20T23:30:00Z", "PreviousDay"),
    )
    await db.execute(
        "UPDATE activity_log SET created_at = ? WHERE project_name = ?",
        ("2026-06-21T06:15:33Z", "MidnightCloseout"),
    )
    await db.commit()

    recent = await fns["get_recent_activity"](
        source="codex", since="2026-06-21", ctx=ctx
    )

    assert [entry["project_name"] for entry in recent] == ["MidnightCloseout"]
    assert recent[0]["timestamp"] == "2026-06-20"
    assert recent[0]["created_at"] == "2026-06-21T06:15:33Z"


async def test_get_recent_activity_breaks_created_at_ties_by_id(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc",
        project_name="OldTie",
        summary="first",
        timestamp="2026-04-01",
        ctx=ctx,
    )
    await fns["log_activity"](
        caller="cc",
        project_name="NewTie",
        summary="second",
        timestamp="2026-04-01",
        ctx=ctx,
    )
    await db.execute(
        "UPDATE activity_log SET created_at = '2026-04-01T00:00:00Z' WHERE source = 'cc'"
    )
    await db.commit()

    recent = await fns["get_recent_activity"](source="cc", limit=2, ctx=ctx)

    assert [entry["project_name"] for entry in recent] == ["NewTie", "OldTie"]


async def test_get_recent_activity_invalid_source_raises(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    with pytest.raises(ToolError, match="Invalid source"):
        await fns["get_recent_activity"](source="bogus", ctx=ctx)


async def test_get_activity_signal_compresses_session_boundaries(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    for timestamp in (
        "2026-06-19T05:04:16Z",
        "2026-06-19T05:05:16Z",
        "2026-06-19T05:06:16Z",
    ):
        await insert_activity_row(
            db,
            source="cc",
            timestamp=timestamp,
            project_name="operant",
            summary="CC session ended",
            tags=["session-boundary"],
        )
    await insert_activity_row(
        db,
        source="cc",
        timestamp="2026-06-18",
        project_name="evals",
        summary="Built LLM judge",
        tags=["eval"],
    )
    await db.commit()

    raw = await fns["get_recent_activity"](limit=10, ctx=ctx)
    assert len(raw) == 4
    assert sum(1 for row in raw if "session-boundary" in row["tags"]) == 3

    signal = await fns["get_activity_signal"](limit=10, ctx=ctx)
    assert len(signal) == 2
    aggregate = signal[0]
    assert aggregate["kind"] == "lifecycle_aggregate"
    assert aggregate["source"] == "cc"
    assert aggregate["project_name"] == "operant"
    assert aggregate["summary_family"] == "CC session ended"
    assert aggregate["time_bucket"] == "2026-06-19T05"
    assert aggregate["count"] == 3
    assert aggregate["first_ts"] == "2026-06-19T05:04:16Z"
    assert aggregate["last_ts"] == "2026-06-19T05:06:16Z"
    assert aggregate["tags"] == ["session-boundary"]

    substantive = signal[1]
    assert substantive["kind"] == "activity"
    assert substantive["project_name"] == "evals"
    assert substantive["summary"] == "Built LLM judge"


async def test_get_activity_signal_keeps_substantive_rows_visible_under_noise(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    for minute in range(30):
        await insert_activity_row(
            db,
            source="cc",
            timestamp=f"2026-06-19T05:{minute:02d}:00Z",
            project_name="operant",
            summary="CC session ended",
            tags=["session-boundary"],
        )
    await insert_activity_row(
        db,
        source="cc",
        timestamp="2026-06-18",
        project_name="evals",
        summary="Substantive eval result",
        tags=["eval"],
    )
    await db.commit()

    signal = await fns["get_activity_signal"](limit=2, ctx=ctx)

    assert [entry["kind"] for entry in signal] == ["lifecycle_aggregate", "activity"]
    assert signal[0]["count"] == 30
    assert signal[1]["project_name"] == "evals"


async def test_get_activity_signal_reserves_substantive_row_when_lifecycle_buckets_exceed_limit(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    for hour in range(24):
        await insert_activity_row(
            db,
            source="cc",
            timestamp=f"2026-06-19T{hour:02d}:00:00Z",
            project_name=f"project-{hour:02d}",
            summary="CC session ended",
            tags=["session-boundary"],
        )
    await insert_activity_row(
        db,
        source="cc",
        timestamp="2026-06-18T23:00:00Z",
        project_name="evals",
        summary="Substantive eval result",
        tags=["eval"],
    )
    await db.commit()

    signal = await fns["get_activity_signal"](limit=20, ctx=ctx)

    assert len(signal) == 20
    assert sum(1 for entry in signal if entry["kind"] == "lifecycle_aggregate") == 19
    assert any(
        entry["kind"] == "activity" and entry["project_name"] == "evals"
        for entry in signal
    )


async def test_get_activity_signal_filters_source_and_since(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await insert_activity_row(
        db,
        source="cc",
        timestamp="2026-06-19T05:00:00Z",
        project_name="operant",
        summary="CC session ended",
        tags=["session-boundary"],
    )
    await insert_activity_row(
        db,
        source="cc",
        timestamp="2026-06-19T05:30:00Z",
        project_name="operant",
        summary="CC session ended",
        tags=["session-boundary"],
    )
    await insert_activity_row(
        db,
        source="codex",
        timestamp="2026-06-19T05:30:00Z",
        project_name="bridge-db",
        summary="Read-only audit",
    )
    await db.execute(
        """
        UPDATE activity_log
        SET created_at = timestamp
        WHERE project_name IN ('operant', 'bridge-db')
        """
    )
    await db.commit()

    cc_signal = await fns["get_activity_signal"](
        source="cc", since="2026-06-19T05:10:00Z", ctx=ctx
    )
    assert len(cc_signal) == 1
    assert cc_signal[0]["kind"] == "lifecycle_aggregate"
    assert cc_signal[0]["count"] == 1
    assert cc_signal[0]["first_ts"] == "2026-06-19T05:30:00Z"

    codex_signal = await fns["get_activity_signal"](source="codex", ctx=ctx)
    assert len(codex_signal) == 1
    assert codex_signal[0]["kind"] == "activity"
    assert codex_signal[0]["source"] == "codex"

    with pytest.raises(ToolError, match="Invalid source"):
        await fns["get_activity_signal"](source="bogus", ctx=ctx)


async def test_get_activity_signal_since_includes_recently_created_prior_date(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await insert_activity_row(
        db,
        source="codex",
        timestamp="2026-06-20",
        project_name="MidnightCloseout",
        summary="closeout inserted after UTC midnight",
    )
    await db.execute(
        "UPDATE activity_log SET created_at = ? WHERE project_name = ?",
        ("2026-06-21T06:15:33Z", "MidnightCloseout"),
    )
    await db.commit()

    signal = await fns["get_activity_signal"](
        source="codex", since="2026-06-21", ctx=ctx
    )

    assert len(signal) == 1
    assert signal[0]["kind"] == "activity"
    assert signal[0]["project_name"] == "MidnightCloseout"
    assert signal[0]["timestamp"] == "2026-06-20"
    assert signal[0]["created_at"] == "2026-06-21T06:15:33Z"


async def test_get_activity_signal_does_not_mutate_fts_or_audit(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await insert_activity_row(
        db,
        source="cc",
        timestamp="2026-06-19T05:00:00Z",
        project_name="operant",
        summary="CC session ended",
        tags=["session-boundary"],
    )
    await db.commit()
    before = await collect_fts_index_metrics(db)

    signal = await fns["get_activity_signal"](ctx=ctx)

    after = await collect_fts_index_metrics(db)
    assert signal[0]["kind"] == "lifecycle_aggregate"
    assert after == before
    assert not config.AUDIT_LOG_PATH.exists()


async def test_signal_pins_protected_rows_beyond_window(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc",
        project_name="p",
        summary="durable milestone",
        tags=["SHIPPED"],
        timestamp="2026-01-01",
        ctx=ctx,
    )
    for i in range(config.ACTIVITY_RETENTION_PER_SOURCE + 10):
        await fns["log_activity"](
            caller="cc",
            project_name="p",
            summary=f"noise {i}",
            timestamp="2026-01-02",
            ctx=ctx,
        )

    signal = await fns["get_activity_signal"](limit=5, ctx=ctx)
    ledger = [e for e in signal if e["kind"] == "ledger"]
    assert len(ledger) == 1
    assert ledger[0]["summary"] == "durable milestone"
    assert len([e for e in signal if e["kind"] != "ledger"]) <= 5


async def test_signal_ledger_dedupes_recent_protected(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc", project_name="p", summary="fresh ship", tags=["SHIPPED"], ctx=ctx
    )
    signal = await fns["get_activity_signal"](limit=10, ctx=ctx)
    matches = [e for e in signal if e["summary"] == "fresh ship"]
    assert len(matches) == 1
    assert matches[0]["kind"] == "ledger"


async def test_signal_stays_flat_list(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    signal: list[dict[str, Any]] = await fns["get_activity_signal"](ctx=make_ctx(db))
    assert isinstance(signal, list)
    assert all(isinstance(e, dict) and "kind" in e for e in signal)


async def test_get_shipped_events(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc", project_name="A", summary="shipped", tags=["SHIPPED"], ctx=ctx
    )
    await fns["log_activity"](
        caller="cc", project_name="B", summary="not shipped", ctx=ctx
    )

    shipped = await fns["get_shipped_events"](ctx=ctx)
    assert len(shipped) == 1
    assert shipped[0]["project_name"] == "A"
    assert "SHIPPED" in shipped[0]["tags"]
    assert shipped[0]["sync_receipt"] is None


async def test_get_shipped_events_since_includes_recently_created_prior_date(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="codex",
        project_name="MidnightShip",
        summary="shipped closeout inserted after UTC midnight",
        tags=["SHIPPED"],
        timestamp="2026-06-20",
        ctx=ctx,
    )
    await db.execute(
        "UPDATE activity_log SET created_at = ? WHERE project_name = ?",
        ("2026-06-21T06:15:33Z", "MidnightShip"),
    )
    await db.commit()

    shipped = await fns["get_shipped_events"](since="2026-06-21", ctx=ctx)

    assert len(shipped) == 1
    assert shipped[0]["project_name"] == "MidnightShip"
    assert shipped[0]["timestamp"] == "2026-06-20"
    assert shipped[0]["created_at"] == "2026-06-21T06:15:33Z"


async def test_get_shipped_events_unprocessed_only(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc", project_name="A", summary="s", tags=["SHIPPED"], ctx=ctx
    )
    await fns["log_activity"](
        caller="cc",
        project_name="B",
        summary="s",
        tags=["SHIPPED", "PROCESSED"],
        ctx=ctx,
    )

    unprocessed = await fns["get_shipped_events"](unprocessed_only=True, ctx=ctx)
    assert len(unprocessed) == 1
    assert unprocessed[0]["project_name"] == "A"


async def test_get_shipped_events_includes_notion_sync_contract(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = tmp_path / "project-registry.json"
    registry.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "canonical_key": "ReadyProject",
                        "display_name": "ReadyProject",
                        "repo_full_name": "saagpatel/ready-project",
                        "bridge_project_names": ["ready-project"],
                        "aliases": [],
                        "notion_local_title": "Ready Project",
                        "notion_local_page_id": "page-ready",
                    },
                    {
                        "canonical_key": "MappedWithoutPage",
                        "display_name": "MappedWithoutPage",
                        "repo_full_name": None,
                        "bridge_project_names": ["mapped-without-page"],
                        "aliases": [],
                        "notion_local_title": "Mapped Without Page",
                        "notion_local_page_id": None,
                    },
                ],
                "resolution_overrides": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PROJECT_REGISTRY_PATH", registry)

    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc",
        project_name="ready-project",
        summary="s",
        tags=["SHIPPED"],
        ctx=ctx,
    )
    await fns["log_activity"](
        caller="cc",
        project_name="mapped-without-page",
        summary="s",
        tags=["SHIPPED"],
        ctx=ctx,
    )
    await fns["log_activity"](
        caller="cc",
        project_name="missing-project",
        summary="s",
        tags=["SHIPPED"],
        ctx=ctx,
    )

    shipped = await fns["get_shipped_events"](ctx=ctx)
    by_name = {entry["project_name"]: entry for entry in shipped}

    assert by_name["ready-project"]["canonical_key"] == "saagpatel/ready-project"
    assert by_name["ready-project"]["notion_sync"] == {
        "state": "ready",
        "reason": "canonical project has explicit notion_local_page_id",
        "canonical_key": "saagpatel/ready-project",
        "notion_page_id": "page-ready",
        "notion_title": "Ready Project",
    }
    assert by_name["mapped-without-page"]["notion_sync"]["state"] == "no_notion_target"
    assert by_name["mapped-without-page"]["notion_sync"]["canonical_key"] is None
    assert by_name["missing-project"]["notion_sync"]["state"] == "unmatched"


async def test_get_shipped_events_marks_policy_backed_meta_events(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = tmp_path / "project-registry.json"
    registry.write_text(
        json.dumps({"entries": [], "resolution_overrides": {}}), encoding="utf-8"
    )
    policy = tmp_path / "meta-shipped-events.json"
    policy.write_text(
        json.dumps(
            {
                "projects": {
                    "operator-os-coherence": {
                        "reason": "machine-level receipt",
                        "record_outcome_in": "bridge-db shipped_sync_receipts",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PROJECT_REGISTRY_PATH", registry)
    monkeypatch.setattr(config, "META_SHIPPED_EVENTS_PATH", policy)

    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc",
        project_name="operator-os-coherence",
        summary="s",
        tags=["SHIPPED"],
        ctx=ctx,
    )
    await fns["log_activity"](
        caller="cc",
        project_name="missing-project",
        summary="s",
        tags=["SHIPPED"],
        ctx=ctx,
    )

    shipped = await fns["get_shipped_events"](ctx=ctx)
    by_name = {entry["project_name"]: entry for entry in shipped}

    meta_sync = by_name["operator-os-coherence"]["notion_sync"]
    assert meta_sync["state"] == "meta_no_target"
    assert meta_sync["reason"] == "machine-level receipt"
    assert meta_sync["notion_page_id"] is None
    assert meta_sync["record_outcome_in"] == "bridge-db shipped_sync_receipts"
    assert meta_sync["policy_ref"] == str(policy)
    assert by_name["missing-project"]["notion_sync"]["state"] == "unmatched"


async def test_record_shipped_event_disposition_is_non_receipt(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc",
        project_name="fable-outputs",
        summary="local artifact",
        tags=["SHIPPED"],
        ctx=ctx,
    )
    cursor = await db.execute("SELECT id FROM activity_log")
    row = await cursor.fetchone()
    assert row is not None
    activity_id = row["id"]

    result = await fns["record_shipped_event_disposition"](
        caller="codex",
        activity_id=activity_id,
        disposition_type="unsynced_by_policy",
        reason="experimental local artifact with no durable downstream target",
        policy_ref="/home/user/Documents/Codex/operating-system-audits/example.md",
        notes="leave pending without receipt",
        ctx=ctx,
    )

    assert result["ok"] is True
    assert result["activity_id"] == activity_id
    assert result["disposition_type"] == "unsynced_by_policy"

    cursor = await db.execute(
        """
        SELECT a.tags, d.disposition_type, d.policy_ref, d.reason, d.decided_by, d.notes
        FROM activity_log AS a
        JOIN shipped_event_dispositions AS d ON d.activity_id = a.id
        WHERE a.id = ?
        """,
        (activity_id,),
    )
    disposition = await cursor.fetchone()
    assert disposition is not None
    assert json.loads(disposition["tags"]) == ["SHIPPED"]
    assert disposition["disposition_type"] == "unsynced_by_policy"
    assert (
        disposition["policy_ref"]
        == "/home/user/Documents/Codex/operating-system-audits/example.md"
    )
    assert (
        disposition["reason"]
        == "experimental local artifact with no durable downstream target"
    )
    assert disposition["decided_by"] == "codex"
    assert disposition["notes"] == "leave pending without receipt"

    cursor = await db.execute("SELECT COUNT(*) FROM shipped_sync_receipts")
    count_row = await cursor.fetchone()
    assert count_row is not None
    assert count_row[0] == 0

    shipped = await fns["get_shipped_events"](ctx=ctx)
    assert shipped[0]["sync_receipt"] is None
    assert shipped[0]["policy_disposition"]["disposition_type"] == "unsynced_by_policy"
    assert shipped[0]["policy_disposition"]["reason"] == (
        "experimental local artifact with no durable downstream target"
    )


async def test_record_shipped_event_disposition_rejects_receipted_event(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc",
        project_name="personal-ops",
        summary="merged",
        tags=["SHIPPED"],
        ctx=ctx,
    )
    cursor = await db.execute("SELECT id FROM activity_log")
    row = await cursor.fetchone()
    assert row is not None
    activity_id = row["id"]

    await fns["confirm_shipped_sync"](
        caller="codex",
        activity_id=activity_id,
        downstream_system="github",
        downstream_ref="https://github.com/example/repo/pull/1",
        ctx=ctx,
    )

    with pytest.raises(ToolError, match="already has a shipped_sync_receipts row"):
        await fns["record_shipped_event_disposition"](
            caller="codex",
            activity_id=activity_id,
            disposition_type="unsynced_by_policy",
            reason="too late",
            ctx=ctx,
        )


async def test_mark_shipped_processed_idempotent(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", tmp_path / "bridge.md")
    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc", project_name="A", summary="s", tags=["TASK_DONE"], ctx=ctx
    )
    cursor = await db.execute("SELECT id FROM activity_log")
    row = await cursor.fetchone()
    assert row is not None
    activity_id = row["id"]

    result1 = await fns["mark_shipped_processed"](activity_ids=[activity_id], ctx=ctx)
    assert result1["updated"] == 1
    assert result1["activity_ids"] == [activity_id]
    assert result1["updated_ids"] == [activity_id]
    assert result1["missing_ids"] == []

    result2 = await fns["mark_shipped_processed"](activity_ids=[activity_id], ctx=ctx)
    assert result2["updated"] == 0
    assert result2["activity_ids"] == [activity_id]
    assert result2["updated_ids"] == []
    assert result2["missing_ids"] == []

    cursor2 = await db.execute(
        "SELECT tags FROM activity_log WHERE id = ?", (activity_id,)
    )
    row2 = await cursor2.fetchone()
    assert row2 is not None
    assert json.loads(row2["tags"]).count("PROCESSED") == 1


async def test_mark_shipped_processed_audit_names_activity_ids(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compatibility processing audit rows should be enough to review what was touched."""
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", audit_path)
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", tmp_path / "bridge.md")

    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc", project_name="A", summary="s", tags=["TASK_DONE"], ctx=ctx
    )
    cursor = await db.execute("SELECT id FROM activity_log")
    row = await cursor.fetchone()
    assert row is not None
    activity_id = row["id"]
    missing_id = activity_id + 999

    result = await fns["mark_shipped_processed"](
        activity_ids=[activity_id, missing_id], ctx=ctx
    )

    assert result["updated"] == 1
    assert result["activity_ids"] == [activity_id, missing_id]
    assert result["updated_ids"] == [activity_id]
    assert result["missing_ids"] == [missing_id]
    audit_lines = audit_path.read_text(encoding="utf-8").splitlines()
    audit_record = json.loads(audit_lines[-1])
    assert audit_record["tool"] == "mark_shipped_processed"
    assert f"activity_ids=[{activity_id}, {missing_id}]" in audit_record["detail"]
    assert f"updated_ids=[{activity_id}]" in audit_record["detail"]
    assert f"missing_ids=[{missing_id}]" in audit_record["detail"]


async def test_mark_shipped_processed_empty_raises(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    with pytest.raises(ToolError):
        await fns["mark_shipped_processed"](activity_ids=[], ctx=ctx)


async def test_mark_shipped_processed_rejects_shipped_rows(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F7: SHIPPED rows require proof or disposition, while non-shipped operational
    events still pass through the compatibility path."""
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", audit_path)
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", tmp_path / "bridge.md")
    ctx = make_ctx(db)

    # operational (non-shipped) event — legitimate use, no receipt lifecycle
    await fns["log_activity"](
        caller="personal_ops",
        project_name="Ops",
        summary="s",
        tags=["TASK_DONE"],
        ctx=ctx,
    )
    # genuine shipped artifact — should go through confirm_shipped_sync instead
    await fns["log_activity"](
        caller="cc", project_name="Ship", summary="s", tags=["SHIPPED"], ctx=ctx
    )
    cursor = await db.execute("SELECT id, tags FROM activity_log ORDER BY id")
    rows: list[aiosqlite.Row] = await cursor.fetchall()  # type: ignore[assignment]
    ops_id, shipped_id = rows[0]["id"], rows[1]["id"]

    ops_result = await fns["mark_shipped_processed"](activity_ids=[ops_id], ctx=ctx)
    assert ops_result["shipped_bypass_ids"] == []

    with pytest.raises(ToolError, match="confirm_shipped_sync"):
        await fns["mark_shipped_processed"](activity_ids=[shipped_id], ctx=ctx)

    last = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert last["tool"] == "mark_shipped_processed"
    assert last["ok"] is False
    assert f"blocked_shipped_ids=[{shipped_id}]" in last["detail"]

    cursor2 = await db.execute(
        "SELECT tags FROM activity_log WHERE id = ?", (shipped_id,)
    )
    row2 = await cursor2.fetchone()
    assert row2 is not None
    assert "PROCESSED" not in json.loads(row2["tags"])


async def test_mark_shipped_processed_refuses_mixed_batch_with_shipped_id(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="personal_ops",
        project_name="Ops",
        summary="done",
        tags=["TASK_DONE"],
        ctx=ctx,
    )
    await fns["log_activity"](
        caller="cc", project_name="Ship", summary="shipped", tags=["SHIPPED"], ctx=ctx
    )
    cursor = await db.execute("SELECT id, tags FROM activity_log ORDER BY id")
    rows: list[aiosqlite.Row] = await cursor.fetchall()  # type: ignore[assignment]
    ops_id, shipped_id = rows[0]["id"], rows[1]["id"]
    before_tags = {row["id"]: json.loads(row["tags"]) for row in rows}

    with pytest.raises(ToolError, match="refuses SHIPPED activity ids"):
        await fns["mark_shipped_processed"](activity_ids=[ops_id, shipped_id], ctx=ctx)

    cursor = await db.execute("SELECT id, tags FROM activity_log ORDER BY id")
    after_rows = await cursor.fetchall()
    after_tags = {row["id"]: json.loads(row["tags"]) for row in after_rows}
    assert after_tags == before_tags


async def test_confirm_shipped_sync_requires_downstream_proof(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc", project_name="A", summary="s", tags=["SHIPPED"], ctx=ctx
    )
    cursor = await db.execute("SELECT id FROM activity_log")
    row = await cursor.fetchone()
    assert row is not None

    with pytest.raises(ToolError, match="downstream_ref"):
        await fns["confirm_shipped_sync"](
            caller="codex",
            activity_id=row["id"],
            downstream_system="notion",
            downstream_ref=" ",
            ctx=ctx,
        )


async def test_confirm_shipped_sync_rejects_non_shipped_event(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](caller="cc", project_name="A", summary="s", ctx=ctx)
    cursor = await db.execute("SELECT id FROM activity_log")
    row = await cursor.fetchone()
    assert row is not None

    with pytest.raises(ToolError, match="not tagged SHIPPED"):
        await fns["confirm_shipped_sync"](
            caller="codex",
            activity_id=row["id"],
            downstream_system="notion",
            downstream_ref="page-123",
            ctx=ctx,
        )


async def test_confirm_shipped_sync_records_receipt_and_marks_processed(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge_path = tmp_path / "bridge.md"
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge_path)

    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="codex",
        project_name="personal-ops",
        summary="SHIPPED wrapper cleanup",
        tags=["SHIPPED"],
        ctx=ctx,
    )
    cursor = await db.execute("SELECT id FROM activity_log")
    row = await cursor.fetchone()
    assert row is not None
    activity_id = row["id"]

    result = await fns["confirm_shipped_sync"](
        caller="codex",
        activity_id=activity_id,
        downstream_system="notion",
        downstream_ref="35bc21f1-caf0-81cb-9426-dd264ef668b2",
        notes="Updated personal-ops portfolio row",
        ctx=ctx,
    )

    assert result["ok"] is True
    assert result["processed_added"] is True
    assert result["downstream_system"] == "notion"

    cursor = await db.execute(
        """
        SELECT a.tags, r.downstream_system, r.downstream_ref, r.synced_by, r.notes
        FROM activity_log AS a
        JOIN shipped_sync_receipts AS r ON r.activity_id = a.id
        WHERE a.id = ?
        """,
        (activity_id,),
    )
    receipt = await cursor.fetchone()
    assert receipt is not None
    assert json.loads(receipt["tags"]) == ["SHIPPED", "PROCESSED"]
    assert receipt["downstream_system"] == "notion"
    assert receipt["downstream_ref"] == "35bc21f1-caf0-81cb-9426-dd264ef668b2"
    assert receipt["synced_by"] == "codex"
    assert receipt["notes"] == "Updated personal-ops portfolio row"

    shipped = await fns["get_shipped_events"](ctx=ctx)
    assert shipped[0]["sync_receipt"]["downstream_ref"] == (
        "35bc21f1-caf0-81cb-9426-dd264ef668b2"
    )
    assert bridge_path.exists()


async def test_confirm_shipped_sync_auto_export_records_context_export_state(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", tmp_path / "bridge.md")
    await db.execute(
        "INSERT INTO context_sections (section_name, owner, content, source_trust, version) "
        "VALUES ('career', 'claude_ai', 'career baseline', 'operator', 3)"
    )
    await db.commit()

    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="codex",
        project_name="bridge-db",
        summary="shipped",
        tags=["SHIPPED"],
        ctx=ctx,
    )
    cursor = await db.execute("SELECT id FROM activity_log")
    row = await cursor.fetchone()
    assert row is not None

    await fns["confirm_shipped_sync"](
        caller="codex",
        activity_id=row["id"],
        downstream_system="policy",
        downstream_ref="/tmp/policy.md",
        ctx=ctx,
    )

    cursor = await db.execute(
        """
        SELECT exported_version, exported_content_sha256
        FROM context_section_export_state
        WHERE section_name = 'career'
        """
    )
    export_state = await cursor.fetchone()
    assert export_state is not None
    assert export_state["exported_version"] == 3
    assert export_state["exported_content_sha256"]


async def test_confirm_shipped_sync_is_idempotent_and_can_refresh_receipt(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc", project_name="A", summary="s", tags=["SHIPPED"], ctx=ctx
    )
    cursor = await db.execute("SELECT id FROM activity_log")
    row = await cursor.fetchone()
    assert row is not None
    activity_id = row["id"]

    first = await fns["confirm_shipped_sync"](
        caller="codex",
        activity_id=activity_id,
        downstream_system="notion",
        downstream_ref="page-1",
        ctx=ctx,
    )
    second = await fns["confirm_shipped_sync"](
        caller="codex",
        activity_id=activity_id,
        downstream_system="notion",
        downstream_ref="page-2",
        ctx=ctx,
    )

    assert first["processed_added"] is True
    assert second["processed_added"] is False

    cursor = await db.execute("SELECT COUNT(*) FROM shipped_sync_receipts")
    count_row = await cursor.fetchone()
    assert count_row is not None
    assert count_row[0] == 1

    cursor = await db.execute("SELECT downstream_ref FROM shipped_sync_receipts")
    receipt = await cursor.fetchone()
    assert receipt is not None
    assert receipt["downstream_ref"] == "page-2"


async def test_log_activity_prunes_to_retention_limit(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    limit = config.ACTIVITY_RETENTION_PER_SOURCE
    for i in range(limit + 5):
        await fns["log_activity"](
            caller="cc", project_name=f"P{i}", summary="s", ctx=ctx
        )

    cursor = await db.execute("SELECT COUNT(*) FROM activity_log WHERE source = 'cc'")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == limit


async def test_log_activity_retention_keeps_highest_ids_when_created_at_ties(
    db: aiosqlite.Connection,
) -> None:
    limit = config.ACTIVITY_RETENTION_PER_SOURCE
    for i in range(limit + 5):
        await insert_activity_row(
            db,
            source="cc",
            timestamp="2026-06-06",
            project_name=f"P{i:02}",
            summary="same-second burst",
            retention_limit=None,
        )
    await db.execute(
        "UPDATE activity_log SET created_at = '9999-01-01T00:00:00Z' WHERE source = 'cc'"
    )
    await db.commit()

    # This insert triggers retention pruning. Its lower created_at means the
    # survivor set is decided entirely by id DESC within the fixed timestamp tie.
    await insert_activity_row(
        db,
        source="cc",
        timestamp="2026-06-06",
        project_name="outside-tie",
        summary="triggers pruning",
        retention_limit=limit,
    )
    await db.commit()

    cursor = await db.execute(
        "SELECT id FROM activity_log WHERE source = 'cc' ORDER BY id"
    )
    rows = await cursor.fetchall()
    assert [row["id"] for row in rows] == list(range(6, 56))

    metrics = await collect_fts_index_metrics(db)
    assert metrics["ok"] is True


async def test_protected_rows_survive_retention_prune(db: aiosqlite.Connection) -> None:
    limit = config.ACTIVITY_RETENTION_PER_SOURCE
    await insert_activity_row(
        db,
        source="cc",
        timestamp="2026-01-01",
        project_name="p",
        summary="shipped thing",
        tags=["SHIPPED"],
        retention_limit=limit,
    )
    for i in range(limit + 10):
        await insert_activity_row(
            db,
            source="cc",
            timestamp="2026-01-02",
            project_name="p",
            summary=f"noise {i}",
            retention_limit=limit,
        )
    await db.commit()

    cursor = await db.execute(
        "SELECT COUNT(*) FROM activity_log WHERE source='cc' "
        "AND EXISTS (SELECT 1 FROM json_each(tags) WHERE upper(value)='SHIPPED')"
    )
    row = await cursor.fetchone()
    assert row is not None and row[0] == 1  # survived past the cap

    cursor = await db.execute("SELECT COUNT(*) FROM activity_log WHERE source='cc'")
    row = await cursor.fetchone()
    assert row is not None and row[0] == limit + 1  # newest-50 ∪ protected


async def test_protected_tag_match_is_case_insensitive(
    db: aiosqlite.Connection,
) -> None:
    limit = config.ACTIVITY_RETENTION_PER_SOURCE
    await insert_activity_row(
        db,
        source="cc",
        timestamp="2026-01-01",
        project_name="p",
        summary="lowercase ledger",
        tags=["ledger"],
        retention_limit=limit,
    )
    for i in range(limit + 5):
        await insert_activity_row(
            db,
            source="cc",
            timestamp="2026-01-02",
            project_name="p",
            summary=f"noise {i}",
            retention_limit=limit,
        )
    await db.commit()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM activity_log WHERE source='cc' "
        "AND EXISTS (SELECT 1 FROM json_each(tags) WHERE upper(value)='LEDGER')"
    )
    row = await cursor.fetchone()
    assert row is not None and row[0] == 1


async def test_protected_rows_keep_fts_mirror(db: aiosqlite.Connection) -> None:
    limit = config.ACTIVITY_RETENTION_PER_SOURCE
    result = await insert_activity_row(
        db,
        source="cc",
        timestamp="2026-01-01",
        project_name="p",
        summary="durable entry",
        tags=["LEDGER"],
        retention_limit=limit,
    )
    for i in range(limit + 5):
        await insert_activity_row(
            db,
            source="cc",
            timestamp="2026-01-02",
            project_name="p",
            summary=f"noise {i}",
            retention_limit=limit,
        )
    await db.commit()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM content_index WHERE source_type='activity' AND source_id=?",
        (str(result.activity_id),),
    )
    row = await cursor.fetchone()
    assert row is not None and row[0] == 1
    cursor = await db.execute(
        "SELECT COUNT(*) FROM content_index WHERE source_type='activity'"
    )
    fts_row = await cursor.fetchone()
    cursor = await db.execute("SELECT COUNT(*) FROM activity_log")
    base_row = await cursor.fetchone()
    assert fts_row is not None and base_row is not None and fts_row[0] == base_row[0]


async def test_prune_returns_pruned_rows(db: aiosqlite.Connection) -> None:
    limit = config.ACTIVITY_RETENTION_PER_SOURCE
    for i in range(limit):
        await insert_activity_row(
            db,
            source="cc",
            timestamp="2026-01-01",
            project_name="p",
            summary=f"row {i}",
            retention_limit=limit,
        )
    result = await insert_activity_row(
        db,
        source="cc",
        timestamp="2026-01-02",
        project_name="p",
        summary="the 51st",
        retention_limit=limit,
    )
    await db.commit()
    assert len(result.pruned_rows) == 1
    pruned_id, pruned_tags = result.pruned_rows[0]
    assert isinstance(pruned_id, int) and pruned_tags == "[]"


async def test_prune_emits_audit_line(
    db: aiosqlite.Connection, fns: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str | None]] = []

    def fake_log_audit(
        tool: str, caller: str, project: str, ok: bool = True, detail: str | None = None
    ) -> None:
        calls.append((tool, detail))

    monkeypatch.setattr(mod, "log_audit", fake_log_audit)
    ctx = make_ctx(db)
    for i in range(config.ACTIVITY_RETENTION_PER_SOURCE + 1):
        await fns["log_activity"](
            caller="cc", project_name="p", summary=f"row {i}", ctx=ctx
        )

    prune_calls = [c for c in calls if c[0] == "log_activity.prune"]
    assert len(prune_calls) == 1  # only the 51st insert pruned anything
    detail = prune_calls[0][1]
    assert detail is not None
    assert (
        "pruned=1" in detail
        and "ids_head=" in detail
        and "tags=" in detail
        and "source=cc" in detail
    )


async def test_log_activity_accepts_notion_os(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    result = await fns["log_activity"](
        caller="notion_os", project_name="BuildLog", summary="synced 3 entries", ctx=ctx
    )
    assert result["ok"] is True
    assert result["source"] == "notion_os"


async def test_log_activity_accepts_personal_ops(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    result = await fns["log_activity"](
        caller="personal_ops", project_name="Inbox", summary="processed mail", ctx=ctx
    )
    assert result["ok"] is True
    assert result["source"] == "personal_ops"


async def test_log_activity_enforce_rejects_caller_mismatch(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_db import config as bridge_config
    from bridge_db.tools import activity as activity_module

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "enforce")
    cap = CaptureMCP()
    activity_module.register(cap)
    with pytest.raises(ToolError, match="bound to 'codex'"):
        await cap.fns["log_activity"](
            caller="cc",
            project_name="TestProject",
            summary="forged write",
            ctx=make_ctx(db, principal="codex"),
        )


async def test_log_activity_enforce_allows_matching_principal(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_db import config as bridge_config
    from bridge_db.tools import activity as activity_module

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "enforce")
    cap = CaptureMCP()
    activity_module.register(cap)
    result = await cap.fns["log_activity"](
        caller="cc",
        project_name="TestProject",
        summary="legit write",
        ctx=make_ctx(db, principal="cc"),
    )
    assert result["ok"] is True


async def test_log_activity_warn_allows_mismatch(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_db import config as bridge_config
    from bridge_db.tools import activity as activity_module

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "warn")
    cap = CaptureMCP()
    activity_module.register(cap)
    result = await cap.fns["log_activity"](
        caller="cc",
        project_name="TestProject",
        summary="mismatched but warned",
        ctx=make_ctx(db, principal="codex"),
    )
    assert result["ok"] is True


async def test_mark_shipped_processed_triggers_auto_export(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After mark_shipped_processed, the bridge markdown file should be written."""
    bridge_path = tmp_path / "bridge.md"
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge_path)

    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc",
        project_name="TestProject",
        summary="operational v1",
        tags=["TASK_DONE"],
        ctx=ctx,
    )
    cursor = await db.execute("SELECT id FROM activity_log")
    row = await cursor.fetchone()
    assert row is not None
    activity_id = row["id"]

    await fns["mark_shipped_processed"](activity_ids=[activity_id], ctx=ctx)

    assert bridge_path.exists(), (
        "bridge markdown file should be written after mark_shipped_processed"
    )
    content = bridge_path.read_text()
    assert "TestProject" in content


async def test_log_activity_clamps_operator_label_in_db(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_db import config as bridge_config
    from bridge_db.tools import activity as activity_module

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "warn")
    cap = CaptureMCP()
    activity_module.register(cap)
    result = await cap.fns["log_activity"](
        caller="cc",
        project_name="TestProject",
        summary="tried to mint operator",
        source_trust="operator",
        ctx=make_ctx(db, principal="cc"),
    )
    assert result["source_trust_clamped"] is True
    cursor = await db.execute(
        "SELECT source_trust FROM activity_log WHERE project_name = ?", ("TestProject",)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["source_trust"] == "agent"


async def test_unprocessed_only_excludes_dispositioned_rows(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc", project_name="A", summary="shipped", tags=["SHIPPED"], ctx=ctx
    )
    cursor = await db.execute("SELECT id FROM activity_log")
    row = await cursor.fetchone()
    assert row is not None
    await db.execute(
        """
        INSERT INTO shipped_event_dispositions (
            activity_id, disposition_type, reason, decided_by
        )
        VALUES (?, 'unsynced_by_policy', 'experimental artifact', 'codex')
        """,
        (row["id"],),
    )
    await db.commit()

    unprocessed = await fns["get_shipped_events"](unprocessed_only=True, ctx=ctx)
    assert unprocessed == []

    everything = await fns["get_shipped_events"](ctx=ctx)
    assert len(everything) == 1
    assert (
        everything[0]["policy_disposition"]["disposition_type"] == "unsynced_by_policy"
    )


async def test_get_shipped_events_honors_limit(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    for i in range(5):
        await fns["log_activity"](
            caller="cc",
            project_name=f"p{i}",
            summary=f"ship {i}",
            tags=["SHIPPED"],
            timestamp=f"2026-07-0{i + 1}",
            ctx=ctx,
        )
    limited = await fns["get_shipped_events"](limit=2, ctx=ctx)
    assert len(limited) == 2
    assert limited[0]["project_name"] == "p4"  # newest first


def test_meta_policy_cache_is_mtime_keyed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_path = tmp_path / "meta-shipped-events.json"
    policy_path.write_text(
        json.dumps({"projects": {"proj": {"reason": "first"}}}), encoding="utf-8"
    )
    monkeypatch.setattr(config, "META_SHIPPED_EVENTS_PATH", policy_path)
    monkeypatch.setattr(mod, "_META_POLICY_CACHE", None)  # reset the module global

    first = mod._load_meta_shipped_event_policy("proj")  # pyright: ignore[reportPrivateUsage]
    assert first is not None and first["reason"] == "first"

    # Overwrite content but pin mtime — the cache must serve the old value.
    stat = policy_path.stat()
    policy_path.write_text(
        json.dumps({"projects": {"proj": {"reason": "second"}}}), encoding="utf-8"
    )
    os.utime(policy_path, (stat.st_atime, stat.st_mtime))
    cached = mod._load_meta_shipped_event_policy("proj")  # pyright: ignore[reportPrivateUsage]
    assert cached is not None and cached["reason"] == "first"

    # Bump mtime — the cache must refresh.
    os.utime(policy_path, (stat.st_atime, stat.st_mtime + 10))
    refreshed = mod._load_meta_shipped_event_policy(  # pyright: ignore[reportPrivateUsage]
        "proj"
    )
    assert refreshed is not None and refreshed["reason"] == "second"
