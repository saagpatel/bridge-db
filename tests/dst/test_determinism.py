"""Determinism self-test (R3 §2) — the gate every other DST result rests on.

One seed, run twice from scratch through the REAL tool functions: the
event-trace hashes and the final DB file bytes must be bit-identical. If
this fails, some nondeterminism leaked (a new wall-clock call site, a
thread, an unseeded draw) and no other DST result from this build is
trustworthy.
"""

from pathlib import Path
from random import Random
from typing import Any, cast

import aiosqlite
from conftest import CaptureMCP, make_ctx

from bridge_db import clock
from bridge_db.tools import activity as activity_mod
from bridge_db.tools import context as context_mod
from bridge_db.tools import handoffs as handoffs_mod
from dst.sim import SimClock, open_sim_db, trace_hash


def _tool_fns() -> dict[str, Any]:
    cap = CaptureMCP()
    handoffs_mod.register(cap)
    context_mod.register(cap)
    activity_mod.register(cap)
    return cap.fns


async def _run_scenario(db_path: Path, seed: int) -> tuple[str, bytes]:
    """A fixed single-writer script over handoffs + sections + activity.

    Deliberately crosses every table with a strftime('now') default and
    exercises a rejection path (stale CAS -> conflict receipt) so receipts
    land in the byte comparison too.
    """
    sim_clock = SimClock()
    rng = Random(seed)
    trace: list[dict[str, Any]] = []
    sim = await open_sim_db(db_path, sim_clock, rng, trace)
    db = cast(aiosqlite.Connection, sim)
    fns = _tool_fns()
    ctx = make_ctx(db, principal="claude_ai")

    clock.install(sim_clock.now)
    try:
        created = await fns["create_handoff"](
            caller="claude_ai",
            project_name="SimProj",
            project_path="/tmp/simproj",
            phase="Phase 1",
            ctx=ctx,
        )

        await fns["update_section"](
            caller="claude_ai",
            section_name="career",
            content="sim baseline",
            ctx=ctx,
        )
        section = await fns["get_section"](section_name="career", ctx=ctx)
        await fns["update_section"](
            caller="claude_ai",
            section_name="career",
            content="sim CAS write",
            if_match_version=section["version"],
            ctx=ctx,
        )
        # Stale CAS on purpose: drives rollback + write_conflicts receipt.
        stale = await fns["update_section"](
            caller="claude_ai",
            section_name="career",
            content="sim stale write",
            if_match_version=section["version"],
            ctx=ctx,
        )
        assert stale["ok"] is False and stale["conflict"] is True

        await fns["log_activity"](
            caller="cc",
            project_name="SimProj",
            summary="sim scenario step",
            timestamp="2030-01-01",
            tags=[],
            ctx=make_ctx(db, principal="cc"),
        )

        await sim.execute(
            "UPDATE pending_handoffs SET source_trust = 'operator' WHERE id = ?",
            (created["handoff_id"],),
        )
        await sim.commit()
        picked = await fns["pick_up_handoff"](
            caller="cc", handoff_id=created["handoff_id"], ctx=ctx
        )
        assert picked["ok"] is True
        cleared = await fns["clear_handoff"](
            caller="cc", project_name="SimProj", ctx=ctx
        )
        assert cleared["cleared"] is True
    finally:
        clock.reset()

    await sim.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await sim.close()
    return trace_hash(trace), db_path.read_bytes()


async def test_same_seed_is_bit_identical(tmp_path: Path) -> None:
    hash_a, bytes_a = await _run_scenario(tmp_path / "run-a.db", seed=42)
    hash_b, bytes_b = await _run_scenario(tmp_path / "run-b.db", seed=42)
    assert hash_a == hash_b
    assert bytes_a == bytes_b


async def test_different_seeds_diverge(tmp_path: Path) -> None:
    """Non-vacuity guard for the bit-identical test: different seeds draw
    different clock ticks, so both the trace AND the DB bytes must differ —
    proving the comparators can actually fail."""
    hash_a, bytes_a = await _run_scenario(tmp_path / "seed-1.db", seed=1)
    hash_b, bytes_b = await _run_scenario(tmp_path / "seed-2.db", seed=2)
    assert hash_a != hash_b
    assert bytes_a != bytes_b


async def test_sim_time_reaches_sql_defaults(tmp_path: Path) -> None:
    """Leak check: rows written through the tools carry SimClock time (the
    2030 epoch), proving no real wall clock slipped past either seam."""
    sim_clock = SimClock()
    rng = Random(7)
    trace: list[dict[str, Any]] = []
    sim = await open_sim_db(tmp_path / "leak.db", sim_clock, rng, trace)
    db = cast(aiosqlite.Connection, sim)
    fns = _tool_fns()
    ctx = make_ctx(db, principal="claude_ai")

    clock.install(sim_clock.now)
    try:
        await fns["create_handoff"](
            caller="claude_ai", project_name="LeakProbe", ctx=ctx
        )
    finally:
        clock.reset()

    cursor = await sim.execute("SELECT dispatched_at FROM pending_handoffs")
    row = await cursor.fetchone()
    assert row is not None
    assert row["dispatched_at"].startswith("2030-")
    await sim.close()
