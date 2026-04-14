"""Tests for activity log tools."""

import json
from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx
from mcp.server.fastmcp.exceptions import ToolError

from bridge_db import config
from bridge_db.tools import activity as mod


@pytest.fixture
def fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    mod.register(cap)
    return cap.fns


async def test_log_activity_inserts_row(db: aiosqlite.Connection, fns: dict[str, Any]) -> None:
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

    recent = await fns["get_recent_activity"](since="2026-03-01", ctx=ctx)
    assert len(recent) == 1
    assert recent[0]["project_name"] == "New"


async def test_get_shipped_events(db: aiosqlite.Connection, fns: dict[str, Any]) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc", project_name="A", summary="shipped", tags=["SHIPPED"], ctx=ctx
    )
    await fns["log_activity"](caller="cc", project_name="B", summary="not shipped", ctx=ctx)

    shipped = await fns["get_shipped_events"](ctx=ctx)
    assert len(shipped) == 1
    assert shipped[0]["project_name"] == "A"
    assert "SHIPPED" in shipped[0]["tags"]


async def test_get_shipped_events_unprocessed_only(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](caller="cc", project_name="A", summary="s", tags=["SHIPPED"], ctx=ctx)
    await fns["log_activity"](
        caller="cc", project_name="B", summary="s", tags=["SHIPPED", "PROCESSED"], ctx=ctx
    )

    unprocessed = await fns["get_shipped_events"](unprocessed_only=True, ctx=ctx)
    assert len(unprocessed) == 1
    assert unprocessed[0]["project_name"] == "A"


async def test_mark_shipped_processed_idempotent(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](caller="cc", project_name="A", summary="s", tags=["SHIPPED"], ctx=ctx)
    cursor = await db.execute("SELECT id FROM activity_log")
    row = await cursor.fetchone()
    assert row is not None
    activity_id = row["id"]

    result1 = await fns["mark_shipped_processed"](activity_ids=[activity_id], ctx=ctx)
    assert result1["updated"] == 1

    result2 = await fns["mark_shipped_processed"](activity_ids=[activity_id], ctx=ctx)
    assert result2["updated"] == 0

    cursor2 = await db.execute("SELECT tags FROM activity_log WHERE id = ?", (activity_id,))
    row2 = await cursor2.fetchone()
    assert row2 is not None
    assert json.loads(row2["tags"]).count("PROCESSED") == 1


async def test_mark_shipped_processed_empty_raises(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    with pytest.raises(ToolError):
        await fns["mark_shipped_processed"](activity_ids=[], ctx=ctx)


async def test_log_activity_prunes_to_retention_limit(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    limit = config.ACTIVITY_RETENTION_PER_SOURCE
    for i in range(limit + 5):
        await fns["log_activity"](caller="cc", project_name=f"P{i}", summary="s", ctx=ctx)

    cursor = await db.execute("SELECT COUNT(*) FROM activity_log WHERE source = 'cc'")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == limit
