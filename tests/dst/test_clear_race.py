"""Phase 2 scenario: clear_handoff (cc) racing pick_up_handoff (codex) on one
pending handoff — the INV-3 / INV-13 clear boundary (R4 WC-2).

This is the first corpus scenario that drives clear_handoff, closing the
`clear_refused_foreign_claim` coverage gap called out in
test_sometimes_coverage (Phase 2 scenario debt).

The clear reads the row as 'pending' (clearable), but a concurrent claim can
flip it to 'active'/claimed_by='codex' before the clear's guarded UPDATE
lands. The UPDATE's status/claimant guard then matches 0 rows and the
post-update recheck refuses it (clear_handoff.py's race branch →
`clear_refused_foreign_claim`). If instead the clear commits first, the
claim sees a non-'pending' row and loses with a durable receipt.

Per-seed oracle (hostile control): the handoff starts 'pending' and only
these two writers mutate it, so EXACTLY one wins — the terminal row is
either 'cleared' (clear won; claim lost with a stale_claim or raced_claim
receipt) or 'active'/claimed_by='codex' (claim won; clear refused with
clear_refused_foreign_claim). Both-lost is impossible unless the guard is
broken (INV-3 + INV-13 + no-silent-loss). INV-3's always() must never trip.
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

# Pinned to the cheapest seed whose interleaving lets the claim win the
# clear's read/write window, driving the refused branch (discovered by the
# sweep below). Replays the clear-race TOCTOU forever via regress_seeds.txt.
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
    """One seeded run; per-seed oracle asserts INV-3 / INV-13 / no-loss inside."""
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
    stale = conn.execute(
        "SELECT COUNT(*) AS n FROM write_conflicts WHERE reason = 'stale_claim'"
    ).fetchone()["n"]
    raced = conn.execute(
        "SELECT COUNT(*) AS n FROM write_conflicts WHERE reason = 'raced_claim'"
    ).fetchone()["n"]
    conn.close()

    clear_outcome = results["clear"]["outcome"]
    claim_outcome = results["claim"]["outcome"]
    assert row is not None

    if clear_outcome == "cleared":
        # Clear won: the claim must have lost, and lost with a durable receipt
        # (no silent loss). The terminal row is 'cleared'.
        assert claim_outcome in ("claim_lost_precheck", "claim_lost_raced"), (
            f"seed {seed}: clear won but claim outcome={claim_outcome}"
        )
        assert row["status"] == "cleared", f"seed {seed}: status={row['status']}"
        assert stale + raced >= 1, f"seed {seed}: claim lost silently (no receipt)"
    elif clear_outcome == "clear_refused":
        # Claim won the window: clear's guarded UPDATE missed, the row is
        # 'active' claimed by codex, and the refusal fired its label.
        assert claim_outcome == "claim_won", (
            f"seed {seed}: clear refused but claim outcome={claim_outcome}"
        )
        assert row["status"] == "active", f"seed {seed}: status={row['status']}"
        assert row["claimed_by"] == "codex", (
            f"seed {seed}: claimed_by={row['claimed_by']}"
        )
    else:
        raise AssertionError(
            f"seed {seed}: handoff exists, so clear must clear or be refused, "
            f"got clear_outcome={clear_outcome}"
        )

    return {
        "clear": clear_outcome,
        "claim": claim_outcome,
        "status": row["status"],
        "hash": trace_hash(trace),
    }


async def test_clear_refused_rederived(tmp_path: Path) -> None:
    """Pinned half: the racing seed re-derives the clear-race TOCTOU — the claim
    commits between clear_handoff's SELECT and its guarded UPDATE, the UPDATE
    matches 0 rows, and the post-update recheck refuses the clear
    (clear_refused_race, a strict subset of clear_refused_foreign_claim). The
    before/after delta proves the pinned seed hits the RACE branch, not a
    static foreign refusal decided before the clear ever read the row."""
    from bridge_db.invariants import sometimes_counts

    before = sometimes_counts().get("clear_refused_race", 0)
    outcome = await run_clear_race(tmp_path, CLEAR_REFUSED_SEED)
    after = sometimes_counts().get("clear_refused_race", 0)
    assert outcome["clear"] == "clear_refused"
    assert outcome["claim"] == "claim_won"
    assert after > before, "pinned seed did not hit the clear-race branch"
    assert sometimes_counts().get("clear_refused_foreign_claim")


async def test_clear_race_both_directions_reachable(tmp_path: Path) -> None:
    """The oracle holds on every sweep seed AND the sweep reaches BOTH winners
    — clear-wins and claim-wins — proving neither branch is vacuous. At least
    one claim-win must hit the true post-UPDATE race branch, not only the
    static foreign-refusal path."""
    from bridge_db.invariants import sometimes_counts

    before = sometimes_counts().get("clear_refused_race", 0)
    clear_won: list[int] = []
    claim_won: list[int] = []
    for seed in SEED_SWEEP:
        outcome = await run_clear_race(tmp_path, seed)
        if outcome["clear"] == "cleared":
            clear_won.append(seed)
        elif outcome["clear"] == "clear_refused":
            claim_won.append(seed)
    assert clear_won, "no seed let the clear win — clear-wins branch is vacuous"
    assert claim_won, "no seed drove the refused clear — the coverage gap is still open"
    assert sometimes_counts().get("clear_refused_race", 0) > before, (
        "no sweep seed hit the clear-race TOCTOU branch — the scenario only "
        "exercises static foreign refusals, not the INV-3 clear race"
    )


async def test_clear_race_replay_is_bit_identical(tmp_path: Path) -> None:
    """Determinism: one seed, two fresh runs, same grants → same trace hash."""
    first = await run_clear_race(tmp_path / "a", seed=7)
    second = await run_clear_race(tmp_path / "b", seed=7)
    assert first["hash"] == second["hash"]
    assert first["clear"] == second["clear"]
    assert first["claim"] == second["claim"]
