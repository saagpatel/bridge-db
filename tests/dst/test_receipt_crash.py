"""Phase 2 scenario WC-6: crash landed in the loser-receipt window (R4 §B).

The fault point keys on the write_conflicts INSERT fingerprint and fires
pre-commit in pick_up_handoff's loser path. A crash there kills the loser
after its raced_claim receipt is staged but before it commits.

GAP FLIPPED (R3 gap #1 → Phase 3 fix): the loser path now stages the
receipt inside the claim attempt's own transaction (no separate
rollback→receipt-commit window), and a refused re-attempt writes an
idempotent stale_claim receipt. A crash before the single commit still
discards the in-flight raced_claim row — the irreducible at-most-once
residue of process death — but the caller's natural retry converges the
ledger to exactly one durable, attributable receipt. The pinned seed
asserts the RECOVERY, and the pre-fix silent-loss shape (zero rows after
retry) would fail these assertions red.

Subsumes WC-7 (self-race phantom, R4 corpus-acceptance): WC-7's hazard is a
retry after an ambiguous durable-BUSY-on-commit emitting a phantom
`raced_claim` receipt that names the caller as loser to a claim it holds.
The Phase-3 INV-2 fix routes every non-'pending' retry through the pre-check
-> `stale_claim` path (handoffs.py:204), so no retry can emit `raced_claim`;
the recovery arm below already asserts `raced == 0` after retries observe an
active row. WC-7's one distinct angle (the retrying caller IS the winner)
takes that identical pre-check path — no new invariant behaviour — and its
ambiguity arm ("commit durable but BUSY returned") is inexpressible without
a post-commit fault mode the engine deliberately omits. Tracked as covered
here rather than built as a redundant scenario that would need shared-harness
surgery for zero marginal invariant coverage.
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

# Re-pinned twice on 2026-07-10, each a declared window change from a fix
# shifting the RNG draw alignment (not lost determinism): 11 → 5 when the
# INV-2 fix removed the loser path's rollback op; 5 → 20 when the review
# round made the stale_claim dedupe statement-atomic and the recovery arm
# concurrent. Current sweep: 3/40 seeds land it (20, 21, 34).
CRASHING_SEED = 20
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


async def _recovery_retry(
    db_path: Path,
    sim_clock: SimClock,
    rng: Random,
    trace: list[TraceEvent],
    scheduler: SimScheduler,
    handoff_id: int,
    caller: str,
    writer_id: str,
) -> str:
    """One retry by the crashed loser's caller on a fresh connection
    (fresh process, no faults — the declared crash was a one-time event).
    The pre-check refusal writes the idempotent stale_claim receipt that
    makes the crashed loss durable. The scenario runs TWO of these
    concurrently under the scheduler: the insert-if-absent dedupe must
    converge to one row even when both retries interleave inside the
    receipt window — the hostile case a sequential double-retry never
    exercises. writer_id must equal the task key handed to scheduler.run
    (the grant loop matches parked entries by that id)."""
    sim = SimConnection(
        db_path,
        sim_clock,
        rng,
        trace,
        scheduler=scheduler,
        writer_id=writer_id,
    )
    fns = handoff_fns()
    ctx = make_ctx(cast(aiosqlite.Connection, sim))
    try:
        await fns["pick_up_handoff"](caller=caller, handoff_id=handoff_id, ctx=ctx)
        return "won"
    except ToolError as exc:
        return "refused" if "not pending" in str(exc) else "error"
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

        crashed = [w for w, r in results.items() if r["outcome"] == "crashed"]
        recovery: list[str] | None = None
        if crashed:
            recovered = await scheduler.run(
                {
                    writer_id: _recovery_retry(
                        db_path,
                        sim_clock,
                        rng,
                        trace,
                        scheduler,
                        handoff_id,
                        crashed[0],
                        writer_id,
                    )
                    for writer_id in ("recovery-a", "recovery-b")
                }
            )
            recovery = sorted(recovered.values())
    finally:
        clock.reset()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status, claimed_by FROM pending_handoffs").fetchone()
    raced = conn.execute(
        "SELECT COUNT(*) AS n FROM write_conflicts WHERE reason = 'raced_claim'"
    ).fetchone()["n"]
    stale = conn.execute(
        "SELECT COUNT(*) AS n FROM write_conflicts WHERE reason = 'stale_claim'"
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
        # Post-fix INV-2: the in-flight raced_claim receipt died with the
        # crashed process (at-most-once residue), but the caller's retry
        # made the loss durable — exactly one stale_claim receipt, even
        # with TWO retries racing concurrently through the receipt window
        # (the insert-if-absent dedupe is statement-atomic).
        assert outcomes.count("crashed") == 1, f"seed {seed}"
        assert raced == 0, f"seed {seed}: raced={raced}"
        assert recovery == ["refused", "refused"], f"seed {seed}: {recovery}"
        assert stale == 1, f"seed {seed}: stale={stale}"
    else:
        # Fault stayed out of the window: INV-2 exact counts must hold on
        # both ledgers — one raced_claim per raced loser, one idempotent
        # stale_claim per pre-check loser.
        assert outcomes.count("crashed") == 0, f"seed {seed}"
        assert raced == outcomes.count("lost_receipt"), f"seed {seed}"
        assert stale == outcomes.count("lost_precheck"), f"seed {seed}"

    return {
        "outcomes": outcomes,
        "raced_receipts": raced,
        "stale_receipts": stale,
        "fired": fired,
        "hash": trace_hash(trace),
    }


async def test_receipt_loss_recovered_after_crash(tmp_path: Path) -> None:
    """WC-6 flipped green (Phase 3 INV-2 fix): the pinned crash seed lands
    the fault in the exact window (non-vacuity counter > 0), the in-flight
    raced_claim receipt dies with the process, and the caller's retry
    converges the ledger to exactly one durable stale_claim receipt. On
    pre-fix code the retry leaves zero rows — these assertions go red."""
    outcome = await run_receipt_crash(tmp_path, CRASHING_SEED)
    assert outcome["fired"] >= 1
    assert outcome["outcomes"] == ["crashed", "won"]
    assert outcome["raced_receipts"] == 0
    assert outcome["stale_receipts"] == 1


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
    assert first["raced_receipts"] == second["raced_receipts"]
    assert first["stale_receipts"] == second["stale_receipts"]
