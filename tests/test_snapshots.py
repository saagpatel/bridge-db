"""Tests for snapshot and cost tools."""

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from bridge_db import config
from bridge_db.db import open_db
from bridge_db.tools import cost as cost_mod
from bridge_db.tools import snapshots as snap_mod


@pytest.fixture
def snap_fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    snap_mod.register(cap)
    raw_save_snapshot = cap.fns["save_snapshot"]

    async def bound_save_snapshot(**kwargs: Any) -> Any:
        """Default snapshot tests exercise the caller's legitimate channel."""
        caller = kwargs["caller"]
        ctx = kwargs.get("ctx")
        principal = getattr(
            getattr(getattr(ctx, "request_context", None), "lifespan_context", None),
            "principal",
            None,
        )
        if principal is None:
            kwargs["ctx"] = make_ctx(db, principal=caller)
        return await raw_save_snapshot(**kwargs)

    cap.fns["save_snapshot"] = bound_save_snapshot
    return cap.fns


@pytest.fixture
def cost_fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    cost_mod.register(cap)
    raw_record_cost = cap.fns["record_cost"]

    async def bound_record_cost(**kwargs: Any) -> Any:
        """Default cost tests exercise the caller's legitimate channel."""
        caller = kwargs["caller"]
        ctx = kwargs.get("ctx")
        principal = getattr(
            getattr(getattr(ctx, "request_context", None), "lifespan_context", None),
            "principal",
            None,
        )
        if principal is None:
            kwargs["ctx"] = make_ctx(db, principal=caller)
        return await raw_record_cost(**kwargs)

    cap.fns["record_cost"] = bound_record_cost
    return cap.fns


# ── Snapshots ────────────────────────────────────────────────────────────────


async def test_save_snapshot_cc(db: aiosqlite.Connection, snap_fns: dict[str, Any]) -> None:
    ctx = make_ctx(db)
    result = await snap_fns["save_snapshot"](
        caller="cc", data={"active_projects": "ink, bridge-db", "lessons": "- use WAL"}, ctx=ctx
    )
    assert result["ok"] is True
    assert result["system"] == "cc"
    assert result["retention_policy"] == "preserve_existing"


async def test_save_snapshot_schema_defaults_to_preservation() -> None:
    mcp = FastMCP("snapshot-schema")
    snap_mod.register(mcp)

    tools = await mcp.list_tools()
    save_snapshot = next(tool for tool in tools if tool.name == "save_snapshot")
    retention_schema = save_snapshot.model_dump()["inputSchema"]["properties"][
        "retention_policy"
    ]

    assert retention_schema["default"] == "preserve_existing"
    assert retention_schema["enum"] == ["preserve_existing", "prune_oldest"]


async def test_save_snapshot_persists_and_echoes_source_trust(
    db: aiosqlite.Connection, snap_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    asserted = await snap_fns["save_snapshot"](
        caller="cc", data={"v": "1"}, source_trust="operator", ctx=ctx
    )
    defaulted = await snap_fns["save_snapshot"](caller="codex", data={"v": "2"}, ctx=ctx)

    assert asserted["source_trust"] == "agent"
    assert asserted["source_trust_clamped"] is True
    assert defaulted["source_trust"] == "agent"

    cursor = await db.execute("SELECT system, source_trust FROM system_snapshots ORDER BY id")
    rows: list[aiosqlite.Row] = await cursor.fetchall()  # type: ignore[assignment]
    trust = {r["system"]: r["source_trust"] for r in rows}
    assert trust["cc"] == "agent"
    assert trust["codex"] == "agent"


async def test_save_snapshot_default_date_uses_utc_calendar_day(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(snap_mod, "_utc_snapshot_date", lambda: "2026-06-21")
    cap = CaptureMCP()
    snap_mod.register(cap)

    result = await cap.fns["save_snapshot"](
        caller="codex", data={"v": "utc-default"}, ctx=make_ctx(db, principal="codex")
    )
    assert result["snapshot_date"] == "2026-06-21"
    cursor = await db.execute(
        "SELECT snapshot_date FROM system_snapshots WHERE system = 'codex'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["snapshot_date"] == "2026-06-21"


async def test_save_snapshot_auth_off_rejects_unbound_operator_forgery(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BDB-DS-003-R1: rollout off cannot bypass snapshot identity or trust."""
    monkeypatch.setattr(config, "AUTH_MODE", "off")
    cap = CaptureMCP()
    snap_mod.register(cap)

    with pytest.raises(ToolError, match="Unauthenticated connection"):
        await cap.fns["save_snapshot"](
            caller="codex",
            data={"forged": True},
            source_trust="operator",
            ctx=make_ctx(db),
        )

    cursor = await db.execute("SELECT COUNT(*) FROM system_snapshots")
    count_row = await cursor.fetchone()
    assert count_row is not None
    assert count_row[0] == 0

async def test_save_snapshot_claude_ai_raises(
    db: aiosqlite.Connection, snap_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    with pytest.raises(ToolError, match="cannot save snapshots"):
        await snap_fns["save_snapshot"](caller="claude_ai", data={}, ctx=ctx)


async def test_save_snapshot_rejects_unknown_retention_policy_before_mutation(
    db: aiosqlite.Connection, snap_fns: dict[str, Any]
) -> None:
    with pytest.raises(ToolError, match="snapshot.invalid_retention_policy"):
        await snap_fns["save_snapshot"](
            caller="cc",
            data={"v": "not-written"},
            retention_policy="delete_everything",
            ctx=make_ctx(db),
        )

    row = await (await db.execute("SELECT COUNT(*) FROM system_snapshots")).fetchone()
    assert row is not None and row[0] == 0


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
        await snap_fns["save_snapshot"](
            caller="cc",
            data={"i": i},
            retention_policy="prune_oldest",
            ctx=ctx,
        )

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
            retention_policy="prune_oldest",
            ctx=ctx,
        )
        await snap_fns["save_snapshot"](
            caller="codex",
            data={"consulted_node": {"latest_consultation": f"CN-{i:03d}"}},
            retention_policy="prune_oldest",
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


async def test_save_snapshot_preserve_existing_refuses_full_family_without_mutation(
    db: aiosqlite.Connection,
    snap_fns: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "SNAPSHOT_RETENTION_PER_SYSTEM", 2)
    ctx = make_ctx(db)
    for i in range(2):
        await snap_fns["save_snapshot"](caller="cc", data={"i": i}, ctx=ctx)

    before = await (
        await db.execute(
            "SELECT id, data FROM system_snapshots WHERE system = 'cc' ORDER BY id"
        )
    ).fetchall()
    result = await snap_fns["save_snapshot"](
        caller="cc",
        data={"i": "refused"},
        retention_policy="preserve_existing",
        ctx=ctx,
    )
    after = await (
        await db.execute(
            "SELECT id, data FROM system_snapshots WHERE system = 'cc' ORDER BY id"
        )
    ).fetchall()
    indexed = await (
        await db.execute(
            "SELECT COUNT(*) FROM content_index WHERE source_type = 'snapshot'"
        )
    ).fetchone()

    assert result == {
        "ok": False,
        "reason_code": "snapshot.retention_would_prune",
        "mutation_performed": False,
        "snapshot_id": None,
        "system": "cc",
        "snapshot_family": "default",
        "snapshot_date": result["snapshot_date"],
        "source_trust": "agent",
        "source_trust_clamped": False,
        "retention_policy": "preserve_existing",
        "retention_limit": 2,
        "retained_count": 2,
        "would_prune_count": 1,
        "pruned_count": 0,
    }
    assert [(row["id"], row["data"]) for row in after] == [
        (row["id"], row["data"]) for row in before
    ]
    assert indexed is not None and indexed[0] == 2
    assert db.in_transaction is False


async def test_save_snapshot_preserve_existing_accepts_under_limit_without_pruning(
    db: aiosqlite.Connection,
    snap_fns: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "SNAPSHOT_RETENTION_PER_SYSTEM", 2)
    ctx = make_ctx(db)
    first = await snap_fns["save_snapshot"](caller="cc", data={"i": 1}, ctx=ctx)
    second = await snap_fns["save_snapshot"](
        caller="cc",
        data={"i": 2},
        retention_policy="preserve_existing",
        ctx=ctx,
    )

    rows = await (
        await db.execute(
            "SELECT id, data FROM system_snapshots WHERE system = 'cc' ORDER BY id"
        )
    ).fetchall()
    indexed = await (
        await db.execute(
            "SELECT source_id FROM content_index "
            "WHERE source_type = 'snapshot' ORDER BY CAST(source_id AS INTEGER)"
        )
    ).fetchall()

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["mutation_performed"] is True
    assert second["retention_policy"] == "preserve_existing"
    assert second["pruned_count"] == 0
    assert [json.loads(row["data"])["i"] for row in rows] == [1, 2]
    assert [int(row["source_id"]) for row in indexed] == [
        first["snapshot_id"],
        second["snapshot_id"],
    ]


async def test_save_snapshot_preserve_existing_does_not_prune_other_codex_family(
    db: aiosqlite.Connection,
    snap_fns: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "SNAPSHOT_RETENTION_PER_SYSTEM", 1)
    operating = {
        "infrastructure": "infra",
        "automation_digest": "automation",
        "active_projects": "projects",
    }
    await db.executemany(
        """
        INSERT INTO system_snapshots (system, snapshot_date, data)
        VALUES ('codex', '2026-01-01', ?)
        """,
        [(json.dumps(operating),), (json.dumps(operating),)],
    )
    await db.commit()

    result = await snap_fns["save_snapshot"](
        caller="codex",
        data={"consulted_node": {"latest_consultation": "CN-001"}},
        retention_policy="preserve_existing",
        ctx=make_ctx(db),
    )
    rows = cast(
        list[aiosqlite.Row],
        await (
            await db.execute(
                "SELECT data FROM system_snapshots WHERE system = 'codex' ORDER BY id"
            )
        ).fetchall(),
    )

    assert result["ok"] is True
    assert result["snapshot_family"] == "consulted_node"
    assert result["pruned_count"] == 0
    assert len(rows) == 3
    assert (
        sum(
            1
            for row in rows
            if {"infrastructure", "automation_digest", "active_projects"}.issubset(
                json.loads(row["data"])
            )
        )
        == 2
    )


async def test_save_snapshot_preserve_existing_serializes_capacity_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "SNAPSHOT_RETENTION_PER_SYSTEM", 1)
    db_path = tmp_path / "concurrent.db"
    first_db = await open_db(db_path)
    second_db = await open_db(db_path)
    cap = CaptureMCP()
    snap_mod.register(cap)
    try:
        results = cast(
            tuple[dict[str, Any], dict[str, Any]],
            await asyncio.gather(
                cap.fns["save_snapshot"](
                    caller="cc",
                    data={"writer": "first"},
                    retention_policy="preserve_existing",
                    ctx=make_ctx(first_db, principal="cc"),
                ),
                cap.fns["save_snapshot"](
                    caller="cc",
                    data={"writer": "second"},
                    retention_policy="preserve_existing",
                    ctx=make_ctx(second_db, principal="cc"),
                ),
            ),
        )
        row = await (
            await first_db.execute(
                "SELECT COUNT(*) FROM system_snapshots WHERE system = 'cc'"
            )
        ).fetchone()
    finally:
        await first_db.close()
        await second_db.close()

    assert sorted(result["ok"] for result in results) == [False, True]
    refusal = next(result for result in results if not result["ok"])
    assert refusal["reason_code"] == "snapshot.retention_would_prune"
    assert refusal["mutation_performed"] is False
    assert refusal["pruned_count"] == 0
    assert row is not None and row[0] == 1


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


async def test_record_cost_auth_off_rejects_cross_system_overwrite(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BDB-DS-004-R1: rollout off cannot authorize a forged cost owner."""
    monkeypatch.setattr(config, "AUTH_MODE", "off")
    cap = CaptureMCP()
    cost_mod.register(cap)
    await cap.fns["record_cost"](
        caller="cc",
        month="2026-04",
        amount=55.0,
        ctx=make_ctx(db, principal="cc"),
    )

    with pytest.raises(ToolError, match="bound to 'codex'"):
        await cap.fns["record_cost"](
            caller="cc",
            month="2026-04",
            amount=999.0,
            ctx=make_ctx(db, principal="codex"),
        )

    cursor = await db.execute(
        "SELECT amount FROM cost_records WHERE system = 'cc' AND month = '2026-04'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["amount"] == 55.0


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
