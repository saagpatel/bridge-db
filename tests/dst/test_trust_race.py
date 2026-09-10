"""Phase 2 scenario WC-8: trust downgraded inside the claim window (R4 §B).

SYNTHETIC-FAULT, declared: no current tool mutates a handoff's
source_trust mid-flight, so the mutation is a raw harness write — armor
for the day an ingest/sync/admin path gains one (R3's own spoofing
comment treats that as a live design concern). A codex claimant passes
the provenance gate against SELECT-time trust='operator'; the synthetic
writer commits a downgrade to 'agent' inside the gate→CAS window.

GAP FLIPPED (R3 gap #3 → Phase 3 fix): the claim CAS now carries
``AND source_trust = ?`` bound to the gate-time value, so a mid-window
downgrade makes the CAS miss and the claim is refused (lost path,
receipt) instead of landing under stale trust. On pre-fix code the same
in-window seed commits a codex-held active claim on non-operator trust —
the pinned test's INV-8 scan goes red.
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
from dst.sim import (
    SimClock,
    SimConnection,
    SimScheduler,
    TraceEvent,
    open_sim_db,
    trace_hash,
)
from dst.test_claim_race import handoff_fns

# Pinned by the 2026-07-10 discovery sweep (15/30 seeds in-window): the
# synthetic downgrade lands inside the gate-SELECT → CAS-commit window
# (post-fix observable: the claimant takes the CAS-miss lost path).
IN_WINDOW_SEED = 0
SEED_SWEEP = range(0, 30)


async def _claimant(
    db_path: Path,
    sim_clock: SimClock,
    rng: Random,
    trace: list[TraceEvent],
    scheduler: SimScheduler,
    handoff_id: int,
) -> dict[str, Any]:
    sim = SimConnection(
        db_path, sim_clock, rng, trace, scheduler=scheduler, writer_id="codex"
    )
    fns = handoff_fns()
    ctx = make_ctx(cast(aiosqlite.Connection, sim), principal="codex")
    try:
        result = await fns["pick_up_handoff"](
            caller="codex", handoff_id=handoff_id, ctx=ctx
        )
        return {"outcome": "won", "claimed_by": result["claimed_by"]}
    except ToolError as exc:
        message = str(exc)
        if "picked up by another caller" in message:
            return {"outcome": "lost_receipt"}  # the CAS-miss (in-window) path
        if "non-operator source trust" in message:
            return {"outcome": "refused_gate"}  # downgrade landed pre-SELECT
        raise
    finally:
        await sim.close()


async def _trust_downgrader(
    db_path: Path,
    sim_clock: SimClock,
    rng: Random,
    trace: list[TraceEvent],
    scheduler: SimScheduler,
    handoff_id: int,
) -> dict[str, Any]:
    """The declared synthetic write: a raw trust downgrade, standing in for
    a future ingest/sync/admin path."""
    sim = SimConnection(
        db_path, sim_clock, rng, trace, scheduler=scheduler, writer_id="syn"
    )
    try:
        await sim.execute(
            "UPDATE pending_handoffs SET source_trust = 'agent' WHERE id = ?",
            (handoff_id,),
        )
        await sim.commit()
        return {"outcome": "downgraded"}
    finally:
        await sim.close()


def _first_commit_index(trace: list[TraceEvent], writer: str) -> int | None:
    for index, event in enumerate(trace):
        if event.get("writer") == writer and event.get("op") == "commit":
            return index
    return None


async def run_trust_race(base: Path, seed: int) -> dict[str, Any]:
    """One seeded WC-8 run; per-seed oracle asserts INV-8 inside.

    INV-8, temporalized: a claim that COMMITS after the downgrade commit
    must never leave codex holding an active claim on non-operator trust.
    (A downgrade landing after a legitimate claim commit is trust
    revocation, a different property — the gate's answer was honestly
    true at commit time.)
    """
    sim_clock = SimClock()
    rng = Random(seed)
    trace: list[TraceEvent] = []
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / f"trust-race-{seed}.db"

    setup = await open_sim_db(db_path, sim_clock, rng, trace)
    fns = handoff_fns()
    setup_ctx = make_ctx(cast(aiosqlite.Connection, setup), principal="claude_ai")

    clock.install(sim_clock.now)
    try:
        created = await fns["create_handoff"](
            caller="claude_ai",
            project_name="TrustProj",
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
            "codex": _claimant(db_path, sim_clock, rng, trace, scheduler, handoff_id),
            "syn": _trust_downgrader(
                db_path, sim_clock, rng, trace, scheduler, handoff_id
            ),
        }
        async with asyncio.timeout(30):
            results = await scheduler.run(writers)
    finally:
        clock.reset()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, claimed_by, source_trust FROM pending_handoffs"
    ).fetchone()
    conn.close()

    outcome = results["codex"]["outcome"]
    syn_commit = _first_commit_index(trace, "syn")
    claim_commit = _first_commit_index(trace, "codex")
    syn_before_claim = (
        syn_commit is not None
        and claim_commit is not None
        and syn_commit < claim_commit
    )

    assert row is not None
    assert results["syn"]["outcome"] == "downgraded"
    # INV-8: if the downgrade committed before the claim commit, the claim
    # must not have landed — no codex-held active claim under non-operator
    # trust, ever.
    if outcome == "won" and syn_before_claim:
        raise AssertionError(
            f"seed {seed}: INV-8 violated — codex claim committed after the "
            f"trust downgrade (final trust={row['source_trust']})"
        )
    if outcome == "won":
        assert row["status"] == "active" and row["claimed_by"] == "codex"
    else:
        # Refused (gate or CAS-miss): the handoff was never claimed.
        assert row["status"] == "pending" and row["claimed_by"] is None

    return {
        "outcome": outcome,
        "syn_before_claim": syn_before_claim,
        "final_trust": row["source_trust"],
        "hash": trace_hash(trace),
    }


async def test_in_window_downgrade_refuses_claim(tmp_path: Path) -> None:
    """WC-8 flipped green (Phase 3 INV-8 fix): on the pinned seed the
    downgrade lands inside the gate→CAS window and the trust-bound CAS
    misses — the claim is refused, the handoff stays pending. On pre-fix
    code the same seed commits the claim under stale trust and the
    per-seed INV-8 scan raises red."""
    outcome = await run_trust_race(tmp_path, IN_WINDOW_SEED)
    assert outcome["outcome"] == "lost_receipt"
    assert outcome["final_trust"] == "agent"


async def test_window_reachable_across_sweep(tmp_path: Path) -> None:
    """Non-vacuity: the sweep must land the downgrade inside the window on
    at least one seed (observable post-fix as the CAS-miss lost path) —
    otherwise the pin proves nothing about the TOCTOU coordinate."""
    in_window: list[int] = []
    for seed in SEED_SWEEP:
        outcome = await run_trust_race(tmp_path, seed)
        if outcome["outcome"] == "lost_receipt":
            in_window.append(seed)
    print(f"in-window seeds: {in_window}")
    assert in_window, "no seed landed the downgrade inside the claim window"


async def test_trust_race_replay_is_bit_identical(tmp_path: Path) -> None:
    """G1: same seed, fresh runs, identical traces and outcomes."""
    first = await run_trust_race(tmp_path / "a", IN_WINDOW_SEED)
    second = await run_trust_race(tmp_path / "b", IN_WINDOW_SEED)
    assert first["hash"] == second["hash"]
    assert first["outcome"] == second["outcome"]
