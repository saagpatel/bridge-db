"""Claimant-only clear racing a legitimate pickup on one pending handoff.

Pending rows are no longer clearable through MCP. Across every schedule the
clear is refused and the pickup is the sole possible state transition, leaving
the row active and claimed by codex. This corpus case proves the denial is
stable under concurrency instead of depending on SELECT/UPDATE ordering.
"""

import asyncio
import sqlite3
from pathlib import Path
from random import Random
from typing import Any, cast

import aiosqlite
from conftest import CaptureMCP, make_ctx
from mcp.server.fastmcp.exceptions import ToolError

from bridge_db import clock
from bridge_db.tools import handoffs as handoffs_mod
from dst.sim import (
    SimClock,
    SimConnection,
    SimScheduler,
    TraceEvent,
    open_sim_db,
    trace_hash,
)

# Pinned to the cheapest seed in the deterministic corpus.
CLEAR_REFUSED_SEED = 0
SEED_SWEEP = range(0, 30)


def handoff_fns() -> dict[str, Any]:
    cap = CaptureMCP()
    handoffs_mod.register(cap)
    return cap.fns


async def _clear_writer(
    db_path: Path,
    sim_clock: SimClock,
    rng: Random,
    trace: list[TraceEvent],
    scheduler: SimScheduler,
    project_name: str,
    caller: str,
) -> dict[str, Any]:
    sim = SimConnection(
        db_path, sim_clock, rng, trace, scheduler=scheduler, writer_id="clear"
    )
    fns = handoff_fns()
    ctx = make_ctx(cast(aiosqlite.Connection, sim), principal=caller)
    try:
        result = await fns["clear_handoff"](
            caller=caller, project_name=project_name, ctx=ctx
        )
        if result["cleared"]:
            return {"outcome": "cleared"}
        if result.get("refused_count", 0) > 0:
            return {"outcome": "clear_refused"}
        return {"outcome": "clear_noop"}  # handoff vanished — impossible here
    finally:
        await sim.close()


async def _claim_writer(
    db_path: Path,
    sim_clock: SimClock,
    rng: Random,
    trace: list[TraceEvent],
    scheduler: SimScheduler,
    handoff_id: int,
    caller: str,
) -> dict[str, Any]:
    sim = SimConnection(
        db_path, sim_clock, rng, trace, scheduler=scheduler, writer_id="claim"
    )
    fns = handoff_fns()
    ctx = make_ctx(cast(aiosqlite.Connection, sim), principal=caller)
    try:
        await fns["pick_up_handoff"](caller=caller, handoff_id=handoff_id, ctx=ctx)
        return {"outcome": "claim_won"}
    except ToolError as exc:
        message = str(exc)
        if "picked up by another caller" in message:
            return {"outcome": "claim_lost_raced"}  # SELECT saw pending, CAS lost
        if "not pending" in message:
            return {"outcome": "claim_lost_precheck"}  # SELECT already saw non-pending
        raise
    finally:
        await sim.close()


async def run_clear_race(base: Path, seed: int) -> dict[str, Any]:
    """One seeded run; per-seed oracle asserts claimant-only completion."""
    sim_clock = SimClock()
    rng = Random(seed)
    trace: list[TraceEvent] = []
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / f"clear-{seed}.db"
    project = "ClearProj"

    setup = await open_sim_db(db_path, sim_clock, rng, trace)
    fns = handoff_fns()
    setup_ctx = make_ctx(cast(aiosqlite.Connection, setup), principal="claude_ai")

    clock.install(sim_clock.now)
    try:
        created = await fns["create_handoff"](
            caller="claude_ai",
            project_name=project,
            source_trust="operator",
            ctx=setup_ctx,
        )
        await setup.execute(
            "UPDATE pending_handoffs SET source_trust = 'operator' WHERE id = ?",
            (created["handoff_id"],),
        )
        await setup.commit()
        await setup.close()

        scheduler = SimScheduler(rng, trace)
        handoff_id = created["handoff_id"]
        writers = {
            "clear": _clear_writer(
                db_path, sim_clock, rng, trace, scheduler, project, "cc"
            ),
            "claim": _claim_writer(
                db_path, sim_clock, rng, trace, scheduler, handoff_id, "codex"
            ),
        }
        async with asyncio.timeout(30):
            results = await scheduler.run(writers)
    finally:
        clock.reset()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status, claimed_by FROM pending_handoffs").fetchone()
    conn.close()

    clear_outcome = results["clear"]["outcome"]
    claim_outcome = results["claim"]["outcome"]
    assert row is not None

    assert clear_outcome == "clear_refused", (
        f"seed {seed}: pending clear was not refused: {clear_outcome}"
    )
    assert claim_outcome == "claim_won", (
        f"seed {seed}: legitimate claimant did not win: {claim_outcome}"
    )
    assert row["status"] == "active", f"seed {seed}: status={row['status']}"
    assert row["claimed_by"] == "codex", f"seed {seed}: claimed_by={row['claimed_by']}"

    return {
        "clear": clear_outcome,
        "claim": claim_outcome,
        "status": row["status"],
        "hash": trace_hash(trace),
    }


async def test_pending_clear_refused_rederived(tmp_path: Path) -> None:
    """The pinned seed refuses pending clear and preserves the claimant transition."""
    from bridge_db.invariants import sometimes_counts

    outcome = await run_clear_race(tmp_path, CLEAR_REFUSED_SEED)
    assert outcome["clear"] == "clear_refused"
    assert outcome["claim"] == "claim_won"
    assert sometimes_counts().get("clear_refused_foreign_claim")


async def test_pending_clear_denial_is_schedule_independent(tmp_path: Path) -> None:
    """Every seeded interleaving converges to the same authority outcome."""
    for seed in SEED_SWEEP:
        outcome = await run_clear_race(tmp_path, seed)
        assert outcome["clear"] == "clear_refused"
        assert outcome["claim"] == "claim_won"
        assert outcome["status"] == "active"


async def test_clear_race_replay_is_bit_identical(tmp_path: Path) -> None:
    """Determinism: one seed, two fresh runs, same grants → same trace hash."""
    first = await run_clear_race(tmp_path / "a", seed=7)
    second = await run_clear_race(tmp_path / "b", seed=7)
    assert first["hash"] == second["hash"]
    assert first["clear"] == second["clear"]
    assert first["claim"] == second["claim"]
