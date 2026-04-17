"""Tests for snapshot and cost tools."""

from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx
from mcp.server.fastmcp.exceptions import ToolError

from bridge_db import config
from bridge_db.tools import cost as cost_mod
from bridge_db.tools import snapshots as snap_mod


@pytest.fixture
def snap_fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    snap_mod.register(cap)
    return cap.fns


@pytest.fixture
def cost_fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    cost_mod.register(cap)
    return cap.fns


# ── Snapshots ────────────────────────────────────────────────────────────────


async def test_save_snapshot_cc(db: aiosqlite.Connection, snap_fns: dict[str, Any]) -> None:
    ctx = make_ctx(db)
    result = await snap_fns["save_snapshot"](
        caller="cc", data={"active_projects": "ink, bridge-db", "lessons": "- use WAL"}, ctx=ctx
    )
    assert result["ok"] is True
    assert result["system"] == "cc"


async def test_save_snapshot_claude_ai_raises(
    db: aiosqlite.Connection, snap_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    with pytest.raises(ToolError, match="cannot save snapshots"):
        await snap_fns["save_snapshot"](caller="claude_ai", data={}, ctx=ctx)


async def test_get_latest_snapshot_returns_most_recent(
    db: aiosqlite.Connection, snap_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await snap_fns["save_snapshot"](
        caller="cc", data={"v": "1"}, snapshot_date="2026-01-01", ctx=ctx
    )
    await snap_fns["save_snapshot"](
        caller="cc", data={"v": "2"}, snapshot_date="2026-04-01", ctx=ctx
    )

    snap = await snap_fns["get_latest_snapshot"](system="cc", ctx=ctx)
    assert snap["data"]["v"] == "2"


async def test_get_latest_snapshot_not_found_raises(
    db: aiosqlite.Connection, snap_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    with pytest.raises(ToolError, match="No snapshot found"):
        await snap_fns["get_latest_snapshot"](system="cc", ctx=ctx)


async def test_save_snapshot_prunes_to_retention(
    db: aiosqlite.Connection, snap_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    limit = config.SNAPSHOT_RETENTION_PER_SYSTEM
    for i in range(limit + 3):
        await snap_fns["save_snapshot"](caller="cc", data={"i": i}, ctx=ctx)

    cursor = await db.execute("SELECT COUNT(*) FROM system_snapshots WHERE system='cc'")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == limit


# ── Cost ─────────────────────────────────────────────────────────────────────


async def test_record_cost_upsert(db: aiosqlite.Connection, cost_fns: dict[str, Any]) -> None:
    ctx = make_ctx(db)
    await cost_fns["record_cost"](caller="cc", month="2026-04", amount=55.0, ctx=ctx)
    await cost_fns["record_cost"](caller="cc", month="2026-04", amount=75.0, ctx=ctx)  # update

    cursor = await db.execute(
        "SELECT COUNT(*) FROM cost_records WHERE system='cc' AND month='2026-04'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 1

    cursor2 = await db.execute(
        "SELECT amount FROM cost_records WHERE system='cc' AND month='2026-04'"
    )
    row2 = await cursor2.fetchone()
    assert row2 is not None
    assert row2["amount"] == 75.0


async def test_record_cost_bad_month_raises(
    db: aiosqlite.Connection, cost_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    with pytest.raises(ToolError, match="Invalid month"):
        await cost_fns["record_cost"](caller="cc", month="April 2026", amount=10.0, ctx=ctx)


async def test_record_cost_claude_ai_raises(
    db: aiosqlite.Connection, cost_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    with pytest.raises(ToolError, match="cannot record costs"):
        await cost_fns["record_cost"](caller="claude_ai", month="2026-04", amount=10.0, ctx=ctx)


async def test_get_cost_history_filter_by_system(
    db: aiosqlite.Connection, cost_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await cost_fns["record_cost"](caller="cc", month="2026-04", amount=55.0, ctx=ctx)
    await cost_fns["record_cost"](caller="codex", month="2026-04", amount=10.0, ctx=ctx)

    cc_only = await cost_fns["get_cost_history"](system="cc", ctx=ctx)
    assert len(cc_only) == 1
    assert cc_only[0]["system"] == "cc"

    all_costs = await cost_fns["get_cost_history"](ctx=ctx)
    assert len(all_costs) == 2


async def test_get_cost_history_invalid_system_raises(
    db: aiosqlite.Connection, cost_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    with pytest.raises(ToolError, match="Invalid system"):
        await cost_fns["get_cost_history"](system="claude_ai", ctx=ctx)