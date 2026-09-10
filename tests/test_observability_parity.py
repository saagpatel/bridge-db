"""Observability-parity coverage: snapshot prunes audit like activity prunes,
and recall queries attribute their caller for per-system stats.

Before this change, _prune_snapshots deleted instruction-bearing rows with no
audit trail (activity prunes have audited since the durable-ledger work), and
recall_query_log.jsonl recorded caller=None on every line, so recall_stats
could never answer "weak for whom?".
"""

from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx

from bridge_db import config
from bridge_db.audit import iter_jsonl
from bridge_db.tools import recall as recall_mod
from bridge_db.tools import snapshots as snapshots_mod


@pytest.fixture
def snapshot_fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    snapshots_mod.register(cap)
    return cap.fns


@pytest.fixture
def recall_fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    recall_mod.register(cap)
    return cap.fns


# ── snapshot prune audit parity ──────────────────────────────────────────────


async def test_snapshot_prune_emits_audit_line_and_count(
    db: aiosqlite.Connection, snapshot_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db, principal="cc")
    last: dict[str, Any] = {}
    for i in range(config.SNAPSHOT_RETENTION_PER_SYSTEM + 1):
        last = await snapshot_fns["save_snapshot"](
            caller="cc",
            data={"active_projects": f"round {i}"},
            retention_policy="prune_oldest",
            ctx=ctx,
        )
    assert last["pruned_count"] == 1

    prune_events = [
        e
        for e in iter_jsonl(config.AUDIT_LOG_PATH)
        if e["tool"] == "save_snapshot.prune"
    ]
    assert len(prune_events) == 1
    assert prune_events[0]["caller"] == "cc"
    assert "pruned=1" in prune_events[0]["detail"]
    assert "ids=[1]" in prune_events[0]["detail"]  # oldest row is the one evicted
    assert "system=cc" in prune_events[0]["detail"]


async def test_snapshot_save_under_limit_reports_zero_pruned_and_no_audit(
    db: aiosqlite.Connection, snapshot_fns: dict[str, Any]
) -> None:
    result = await snapshot_fns["save_snapshot"](
        caller="cc",
        data={"active_projects": "only one"},
        ctx=make_ctx(db, principal="cc"),
    )
    assert result["pruned_count"] == 0
    prune_events = [
        e
        for e in iter_jsonl(config.AUDIT_LOG_PATH)
        if e["tool"] == "save_snapshot.prune"
    ]
    assert prune_events == []


async def test_snapshot_prune_respects_codex_families(
    db: aiosqlite.Connection, snapshot_fns: dict[str, Any]
) -> None:
    """Family partitioning is retention semantics the audit line must not distort:
    an over-limit operating family prunes while consulted_node stays untouched."""
    ctx = make_ctx(db, principal="codex")
    operating = {
        "infrastructure": "x",
        "automation_digest": "y",
        "active_projects": "z",
    }
    last: dict[str, Any] = {}
    for _ in range(config.SNAPSHOT_RETENTION_PER_SYSTEM + 1):
        last = await snapshot_fns["save_snapshot"](
            caller="codex",
            data=operating,
            retention_policy="prune_oldest",
            ctx=ctx,
        )
    consulted = await snapshot_fns["save_snapshot"](
        caller="codex", data={"consulted_node": {"k": 1}}, ctx=ctx
    )
    assert last["pruned_count"] == 1
    assert consulted["pruned_count"] == 0
    prune_events = [
        e
        for e in iter_jsonl(config.AUDIT_LOG_PATH)
        if e["tool"] == "save_snapshot.prune"
    ]
    assert len(prune_events) == 1
    assert "families=['operating']" in prune_events[0]["detail"]


# ── recall caller attribution ────────────────────────────────────────────────


async def test_recall_logs_caller_and_stats_break_down_by_caller(
    db: aiosqlite.Connection, recall_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    # attributed hit-less queries from two systems + one legacy unattributed call
    await recall_fns["recall"](query="nonexistent thing", caller="cc", ctx=ctx)
    await recall_fns["recall"](query="another missing thing", caller="cc", ctx=ctx)
    await recall_fns["recall"](query="codex query", caller="codex", ctx=ctx)
    await recall_fns["recall"](query="legacy query", ctx=ctx)

    records = list(iter_jsonl(recall_mod.RECALL_LOG_PATH))
    assert [r["caller"] for r in records] == ["cc", "cc", "codex", None]

    stats = recall_mod.collect_recall_stats(days=7)
    breakdown = stats["caller_breakdown"]
    assert breakdown["cc"]["count"] == 2
    assert breakdown["codex"]["count"] == 1
    assert breakdown["unattributed"]["count"] == 1
    # empty test DB: every query misses, so per-caller miss rates are 1.0
    assert breakdown["cc"]["miss_rate"] == 1.0
    assert stats["total_queries"] == 4
