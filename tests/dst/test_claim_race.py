"""Phase 2 scenario (a): N writers contending pick_up_handoff on one handoff.

R3 Phase 2 exit criterion: the engine re-derives the raced-claim TOCTOU
from a seed — the loser's SELECT sees 'pending' before the winner's claim
commits, its status-guarded UPDATE then matches 0 rows, and the loser path
writes exactly one raced_claim receipt. Per-seed oracle (hostile control):
exactly one winner ever, DB state names that winner, and the receipt count
equals the number of losers who took the raced branch (INV-1 + INV-2).
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

# Pinned by the 2026-07-10 discovery sweep (23/30 seeds raced; 7 lost at the
# pre-check instead). Replays the TOCTOU forever via regress_seeds.txt.
RACING_SEED = 0
SEED_SWEEP = range(0, 30)


def handoff_fns() -> dict[str, Any]:
    cap = CaptureMCP()
    handoffs_mod.register(cap)
    return cap.fns


async def _claim_writer(
    writer_id: str,
    db_path: Path,
    sim_clock: SimClock,
    rng: Random,
    trace: list[TraceEvent],
    scheduler: SimScheduler,
    handoff_id: int,
    caller: str,
) -> dict[str, Any]:
    sim = SimConnection(
        db_path, sim_clock, rng, trace, scheduler=scheduler, writer_id=writer_id
    )
    fns = handoff_fns()
    ctx = make_ctx(cast(aiosqlite.Connection, sim))
    try:
        result = await fns["pick_up_handoff"](
            caller=caller, handoff_id=handoff_id, ctx=ctx
        )
        return {"outcome": "won", "claimed_by": result["claimed_by"]}
    except ToolError as exc:
        message = str(exc)
        if "picked up by another caller" in message:
            return {"outcome": "lost_receipt"}  # the raced CAS branch
        if "not pending" in message:
            return {"outcome": "lost_precheck"}  # loser's SELECT already saw it
        raise
    finally:
        await sim.close()


async def run_claim_race(base: Path, seed: int) -> dict[str, Any]:
    """One seeded run; per-seed oracle asserts INV-1/INV-2 inside."""
    sim_clock = SimClock()
    rng = Random(seed)
    trace: list[TraceEvent] = []
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / f"race-{seed}.db"

    setup = await open_sim_db(db_path, sim_clock, rng, trace)
    fns = handoff_fns()
    setup_ctx = make_ctx(cast(aiosqlite.Connection, setup))

    clock.install(sim_clock.now)
    try:
        created = await fns["create_handoff"](
            caller="claude_ai",
            project_name="RaceProj",
            source_trust="operator",  # auth off in tests: no clamping
            ctx=setup_ctx,
        )
        await setup.close()

        scheduler = SimScheduler(rng, trace)
        handoff_id = created["handoff_id"]
        writers = {
            "cc": _claim_writer(
                "cc", db_path, sim_clock, rng, trace, scheduler, handoff_id, "cc"
            ),
            "codex": _claim_writer(
                "codex", db_path, sim_clock, rng, trace, scheduler, handoff_id, "codex"
            ),
        }
        async with asyncio.timeout(30):
            results = await scheduler.run(writers)
    finally:
        clock.reset()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status, claimed_by FROM pending_handoffs").fetchone()
    receipts = conn.execute(
        "SELECT COUNT(*) AS n FROM write_conflicts WHERE reason = 'raced_claim'"
    ).fetchone()["n"]
    conn.close()

    outcomes = sorted(r["outcome"] for r in results.values())
    winners = [w for w, r in results.items() if r["outcome"] == "won"]
    # INV-1 hostile control: exactly one committed winner, every seed.
    assert len(winners) == 1, f"seed {seed}: winners={winners}"
    assert row is not None
    assert row["status"] == "active"
    assert row["claimed_by"] == winners[0]
    # INV-2 exact count: one receipt per loser that took the raced branch.
    assert receipts == outcomes.count("lost_receipt"), f"seed {seed}"

    return {"outcomes": outcomes, "receipts": receipts, "hash": trace_hash(trace)}


async def test_raced_claim_toctou_rederived(tmp_path: Path) -> None:
    """R3 Phase 2 exit criterion, pinned half: the known racing seed
    reproduces the TOCTOU — loser SELECT saw 'pending', CAS matched 0,
    exactly one raced_claim receipt — and the Phase-0 reachability counter
    fires on its intended trigger (closing the deferred coverage gap from
    the 5000102 review)."""
    from bridge_db.invariants import sometimes_counts

    outcome = await run_claim_race(tmp_path, RACING_SEED)
    assert outcome["receipts"] >= 1
    assert outcome["outcomes"] == ["lost_receipt", "won"]
    assert sometimes_counts().get("raced_claim_receipt_written") == outcome["receipts"]


async def test_single_winner_across_seed_sweep(tmp_path: Path) -> None:
    """INV-1 holds on every sweep seed AND the sweep re-derives the raced
    TOCTOU on at least one (Phase 2 exit criterion, discovery half)."""
    raced_seeds: list[int] = []
    for seed in SEED_SWEEP:
        outcome = await run_claim_race(tmp_path, seed)
        if outcome["receipts"] > 0:
            raced_seeds.append(seed)
    assert raced_seeds, "no seed in the sweep produced the raced-claim TOCTOU"


async def test_scheduler_replay_is_bit_identical(tmp_path: Path) -> None:
    """G1 extended to concurrency: one seed, two fresh runs, same grants,
    same trace hash, same outcomes."""
    first = await run_claim_race(tmp_path / "a", seed=11)
    second = await run_claim_race(tmp_path / "b", seed=11)
    assert first["hash"] == second["hash"]
    assert first["outcomes"] == second["outcomes"]
    assert first["receipts"] == second["receipts"]
