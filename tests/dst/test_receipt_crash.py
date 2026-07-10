"""Phase 2 scenario WC-6: crash landed in the loser-receipt window (R4 §B).

The fault point keys on the write_conflicts INSERT fingerprint and fires
pre-commit — the two-op rollback→receipt-commit window in pick_up_handoff
(handoffs.py, loser path). A crash there kills the loser after its
raced_claim receipt is staged but before it commits: the race was genuine,
the ledger stays empty, and the loss is silent exactly when contention
telemetry matters.

GAP LEDGER (R3 gap #1, gate G6): INV-2 is expected-RED here on current
code. The pinned test asserts the VIOLATION reproduces. When Phase 3 lands
receipt-before-raise / idempotent receipts, this seed must flip and the
assertions below get inverted to receipts == 1.
"""

import asyncio
import sqlite3
from pathlib import Path
from random import Random
from typing import Any, cast

import aiosqlite
from conftest import make_ctx
from mcp.server.fastmcp.exceptions import ToolError

from bridge_db import clock
from bridge_db.invariants import sometimes_counts
from dst.sim import (
    FaultPlan,
    FaultPoint,
    SimClock,
    SimConnection,
    SimCrash,
    SimScheduler,
    TraceEvent,
    open_sim_db,
    trace_hash,
)
from dst.test_claim_race import handoff_fns

# Pinned by the 2026-07-10 discovery sweep (4/40 seeds landed the crash
# in-window: 11, 12, 21, 34): race + live fault + in-window fire — the
# crash lands between the receipt INSERT and its commit.
CRASHING_SEED = 11
SEED_SWEEP = range(0, 40)

_WINDOW_LABEL = "fault_fired_in_receipt_window"

_RECEIPT_CRASH = FaultPoint(
    match="INSERT INTO write_conflicts",
    op="pre-commit",
    kind="crash",
    label=_WINDOW_LABEL,
)


async def _crashy_claim_writer(
    writer_id: str,
    db_path: Path,
    sim_clock: SimClock,
    rng: Random,
    trace: list[TraceEvent],
    scheduler: SimScheduler,
    faults: FaultPlan,
    handoff_id: int,
    caller: str,
) -> dict[str, Any]:
    sim = SimConnection(
        db_path,
        sim_clock,
        rng,
        trace,
        scheduler=scheduler,
        writer_id=writer_id,
        faults=faults,
    )
    fns = handoff_fns()
    ctx = make_ctx(cast(aiosqlite.Connection, sim))
    try:
        result = await fns["pick_up_handoff"](
            caller=caller, handoff_id=handoff_id, ctx=ctx
        )
        return {"outcome": "won", "claimed_by": result["claimed_by"]}
    except SimCrash:
        return {"outcome": "crashed"}  # process death mid-receipt-window
    except ToolError as exc:
        message = str(exc)
        if "picked up by another caller" in message:
            return {"outcome": "lost_receipt"}
        if "not pending" in message:
            return {"outcome": "lost_precheck"}
        raise
    finally:
        await sim.close()


async def run_receipt_crash(base: Path, seed: int) -> dict[str, Any]:
    """One seeded WC-6 run; per-seed oracle asserts inside.

    INV-1 must hold on every seed (the crash point is only reachable on the
    loser path, so the winner can never crash). INV-2 is checked at the
    oracle in both directions: when the fault stayed out of the window the
    receipt count is exact; when it landed, the raced loss left zero rows —
    the gap this scenario exists to pin.
    """
    sim_clock = SimClock()
    rng = Random(seed)
    trace: list[TraceEvent] = []
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / f"receipt-crash-{seed}.db"

    setup = await open_sim_db(db_path, sim_clock, rng, trace)
    fns = handoff_fns()
    setup_ctx = make_ctx(cast(aiosqlite.Connection, setup))
    fired_before = sometimes_counts().get(_WINDOW_LABEL, 0)

    clock.install(sim_clock.now)
    try:
        created = await fns["create_handoff"](
            caller="claude_ai",
            project_name="CrashProj",
            source_trust="operator",  # auth off in tests: no clamping
            ctx=setup_ctx,
        )
        await setup.close()

        scheduler = SimScheduler(rng, trace)
        # One plan shared by both writers: liveness is a per-run decision.
        faults = FaultPlan([_RECEIPT_CRASH], rng, trace)
        handoff_id = created["handoff_id"]
        writers = {
            writer: _crashy_claim_writer(
                writer,
                db_path,
                sim_clock,
                rng,
                trace,
                scheduler,
                faults,
                handoff_id,
                writer,
            )
            for writer in ("cc", "codex")
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
    fired = sometimes_counts().get(_WINDOW_LABEL, 0) - fired_before

    # INV-1 hostile control: exactly one committed winner, faults or not.
    assert len(winners) == 1, f"seed {seed}: winners={winners}"
    assert row is not None
    assert row["status"] == "active"
    assert row["claimed_by"] == winners[0]
    if fired:
        # The gap, oracle-detected: a genuine raced loss with zero receipt
        # rows — the crash discarded the staged INSERT (INV-2 RED).
        assert outcomes.count("crashed") == 1, f"seed {seed}"
        assert receipts == 0, f"seed {seed}: receipts={receipts}"
    else:
        # Fault stayed out of the window: INV-2 exact count must hold.
        assert outcomes.count("crashed") == 0, f"seed {seed}"
        assert receipts == outcomes.count("lost_receipt"), f"seed {seed}"

    return {
        "outcomes": outcomes,
        "receipts": receipts,
        "fired": fired,
        "hash": trace_hash(trace),
    }


async def test_receipt_loss_gap_rederived(tmp_path: Path) -> None:
    """WC-6 acceptance: the pinned seed deterministically reproduces a raced
    loss with zero receipt rows, crash landed inside the exact window
    (non-vacuity counter > 0), INV-2 firing at the oracle."""
    outcome = await run_receipt_crash(tmp_path, CRASHING_SEED)
    assert outcome["fired"] >= 1
    assert outcome["outcomes"] == ["crashed", "won"]
    assert outcome["receipts"] == 0


async def test_fault_lands_in_window_across_sweep(tmp_path: Path) -> None:
    """Gate G5 non-vacuity: across the sweep the fault machinery must land
    inside the two-op window at least once — otherwise every seed is
    vacuously green and the scenario proves nothing."""
    crash_seeds: list[int] = []
    for seed in SEED_SWEEP:
        outcome = await run_receipt_crash(tmp_path, seed)
        if outcome["fired"]:
            crash_seeds.append(seed)
    print(f"crash seeds: {crash_seeds}")
    assert crash_seeds, "fault never landed in the receipt window across the sweep"


async def test_receipt_crash_replay_is_bit_identical(tmp_path: Path) -> None:
    """G1 extended to faults: same seed, two fresh runs, same fault
    decisions, same trace hash, same outcomes."""
    first = await run_receipt_crash(tmp_path / "a", CRASHING_SEED)
    second = await run_receipt_crash(tmp_path / "b", CRASHING_SEED)
    assert first["hash"] == second["hash"]
    assert first["outcomes"] == second["outcomes"]
    assert first["receipts"] == second["receipts"]
