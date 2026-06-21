"""Tests for snapshot and cost tools."""

import json
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


async def test_save_snapshot_persists_and_echoes_source_trust(
    db: aiosqlite.Connection, snap_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    asserted = await snap_fns["save_snapshot"](
        caller="cc", data={"v": "1"}, source_trust="operator", ctx=ctx
    )
    defaulted = await snap_fns["save_snapshot"](caller="codex", data={"v": "2"}, ctx=ctx)

    assert asserted["source_trust"] == "operator"
    assert defaulted["source_trust"] == "agent"

    cursor = await db.execute("SELECT system, source_trust FROM system_snapshots ORDER BY id")
    rows: list[aiosqlite.Row] = await cursor.fetchall()  # type: ignore[assignment]
    trust = {r["system"]: r["source_trust"] for r in rows}
    assert trust["cc"] == "operator"
    assert trust["codex"] == "agent"


async def test_save_snapshot_default_date_uses_utc_calendar_day(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(snap_mod, "_utc_snapshot_date", lambda: "2026-06-21")
    cap = CaptureMCP()
    snap_mod.register(cap)

    result = await cap.fns["save_snapshot"](
        caller="codex", data={"v": "utc-default"}, ctx=make_ctx(db)
    )

    assert result["snapshot_date"] == "2026-06-21"
    cursor = await db.execute(
        "SELECT snapshot_date FROM system_snapshots WHERE system = 'codex'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["snapshot_date"] == "2026-06-21"


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


async def test_get_latest_snapshot_breaks_created_at_ties_by_id(
    db: aiosqlite.Connection, snap_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await snap_fns["save_snapshot"](
        caller="cc", data={"v": "old"}, snapshot_date="2026-01-01", ctx=ctx
    )
    await snap_fns["save_snapshot"](
        caller="cc", data={"v": "new"}, snapshot_date="2026-01-02", ctx=ctx
    )
    await db.execute(
        "UPDATE system_snapshots SET created_at = '2026-01-01T00:00:00Z' WHERE system = 'cc'"
    )
    await db.commit()

    snap = await snap_fns["get_latest_snapshot"](system="cc", ctx=ctx)

    assert snap["data"]["v"] == "new"


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


async def test_save_snapshot_prunes_codex_families_independently(
    db: aiosqlite.Connection, snap_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    limit = config.SNAPSHOT_RETENTION_PER_SYSTEM
    for i in range(limit + 2):
        await snap_fns["save_snapshot"](
            caller="codex",
            data={
                "infrastructure": f"- infra {i}",
                "automation_digest": f"- automation {i}",
                "active_projects": f"- project {i}",
            },
            ctx=ctx,
        )
        await snap_fns["save_snapshot"](
            caller="codex",
            data={"consulted_node": {"latest_consultation": f"CN-{i:03d}"}},
            ctx=ctx,
        )

    cursor = await db.execute("SELECT data FROM system_snapshots WHERE system='codex'")
    rows = await cursor.fetchall()
    operating = 0
    consulted_node = 0
    for row in rows:
        data = json.loads(row["data"])
        if "consulted_node" in data:
            consulted_node += 1
        elif {"infrastructure", "automation_digest", "active_projects"}.issubset(data):
            operating += 1

    assert operating == limit
    assert consulted_node == limit


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


async def test_save_snapshot_clamps_operator_label_in_db(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_db import config as bridge_config
    from bridge_db.tools import snapshots as snapshots_module

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "warn")
    cap = CaptureMCP()
    snapshots_module.register(cap)
    result = await cap.fns["save_snapshot"](
        caller="cc",
        data={"active_projects": ["x"]},
        source_trust="operator",
        ctx=make_ctx(db, principal="cc"),
    )
    assert result["source_trust_clamped"] is True
    cursor = await db.execute(
        "SELECT source_trust FROM system_snapshots WHERE system = 'cc' ORDER BY id DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["source_trust"] == "agent"
