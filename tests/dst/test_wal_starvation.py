"""Phase 2 scenario WC-9: leaked transaction + reader wall vs the checkpointer.

Arm red: a declared always-on fault fails the FTS INSERT inside
log_activity; the tool has no rollback path, the (buggy) caller swallows
the error, and the connection idles in-transaction — the leak (INV-10,
detected at the shim's tool-return probe). Behind it, every other writer
spins on BUSY to the grant budget (starved) and every checkpoint_wal
TRUNCATE attempt returns busy — one leaked transaction from one tool call
poisons the entire batch's liveness signal: sometimes("wal_truncated")
stays 0 across the poisoned batch (INV-11 red at the batch report).

Arm control: identical actors, no fault. Writers complete, the scheduler's
draws leave reader-free gaps in some seeds, TRUNCATE completes there, and
post-checkpoint WAL size is 0 — INV-11 holds with nonzero counters.

INV-11 is a liveness property: no single trace can trip it. The assertion
lives at the BATCH dimension (R3 CI tier 1: "a label at 0/N fails the
build"), which is why the tests below sweep a seed batch per arm.
"""

import asyncio
from pathlib import Path
from random import Random
from typing import Any, cast

import aiosqlite
from conftest import CaptureMCP, make_ctx

from bridge_db import clock
from bridge_db.db import checkpoint_wal
from bridge_db.invariants import sometimes, sometimes_counts
from bridge_db.tools import activity as activity_mod
from dst.sim import (
    FaultPlan,
    FaultPoint,
    SimClock,
    SimConnection,
    SimInjectedError,
    SimScheduler,
    TraceEvent,
    open_sim_db,
    trace_hash,
)

# The batch IS the assertion unit for INV-11 (liveness); per-seed oracles
# inside the runner cover INV-10. Seed 0 of each arm is pinned in
# regress_seeds.txt as the corpus anchor.
PINNED_SEED = 0
SEED_BATCH = range(0, 8)
GRANT_BUDGET = 2000
CHECKPOINT_ATTEMPTS = 6
_TRUNCATED_LABEL = "wal_truncated"

# Declared always-on fault (liveness_p=fire_p=1.0): the FTS mirror INSERT
# fails inside log_activity's transaction — RC-8's leak material.
_FTS_ERROR = FaultPoint(
    match="INSERT INTO content_index",
    op="execute",
    kind="error",
)


def activity_fns() -> dict[str, Any]:
    cap = CaptureMCP()
    activity_mod.register(cap)
    return cap.fns


async def _leaker(sim: SimConnection, project: str) -> dict[str, Any]:
    """One log_activity call from a buggy caller: on the injected failure it
    neither rolls back nor closes — the tool-return probe is INV-10."""
    fns = activity_fns()
    ctx = make_ctx(cast(aiosqlite.Connection, sim))
    injected = False
    try:
        await fns["log_activity"](
            caller="cc", project_name=project, summary="leak attempt", ctx=ctx
        )
    except SimInjectedError:
        injected = True
    return {"injected": injected, "tx_open_at_return": sim.in_transaction}


async def _activity_writer(
    writer_id: str,
    db_path: Path,
    sim_clock: SimClock,
    rng: Random,
    trace: list[TraceEvent],
    scheduler: SimScheduler,
    caller: str,
    project: str,
) -> dict[str, Any]:
    sim = SimConnection(
        db_path, sim_clock, rng, trace, scheduler=scheduler, writer_id=writer_id
    )
    fns = activity_fns()
    ctx = make_ctx(cast(aiosqlite.Connection, sim))
    try:
        for round_no in range(2):
            await fns["log_activity"](
                caller=caller,
                project_name=project,
                summary=f"{writer_id} round {round_no}",
                ctx=ctx,
            )
        return {"outcome": "completed"}
    finally:
        await sim.close()


async def _reader(
    writer_id: str,
    db_path: Path,
    sim_clock: SimClock,
    rng: Random,
    trace: list[TraceEvent],
    scheduler: SimScheduler,
) -> dict[str, Any]:
    """Continuous reader: each round holds an explicit read snapshot open
    across several scheduler grants — the wall a TRUNCATE must find a gap
    in. WAL readers never block on writers, so readers complete both arms."""
    sim = SimConnection(
        db_path, sim_clock, rng, trace, scheduler=scheduler, writer_id=writer_id
    )
    try:
        for _ in range(3):
            await sim.execute("BEGIN")
            cursor = await sim.execute("SELECT COUNT(*) FROM activity_log")
            await cursor.fetchall()
            await sim.execute("SELECT COUNT(*) FROM system_snapshots")
            await sim.commit()
        return {"outcome": "completed"}
    finally:
        await sim.close()


async def _checkpointer(
    db_path: Path,
    sim_clock: SimClock,
    rng: Random,
    trace: list[TraceEvent],
    scheduler: SimScheduler,
) -> list[dict[str, Any]]:
    sim = SimConnection(
        db_path, sim_clock, rng, trace, scheduler=scheduler, writer_id="ck"
    )
    wal_path = Path(str(db_path) + "-wal")
    attempts: list[dict[str, Any]] = []
    try:
        for _ in range(CHECKPOINT_ATTEMPTS):
            result = await checkpoint_wal(cast(aiosqlite.Connection, sim))
            truncated = result["busy"] == 0
            # INV-11's batch liveness label: red means a whole seed batch
            # where no checkpoint ever won a reader/writer-free moment.
            sometimes(_TRUNCATED_LABEL, truncated)
            attempts.append(
                {
                    "busy": result["busy"],
                    "wal_size": wal_path.stat().st_size if wal_path.exists() else 0,
                }
            )
        return attempts
    finally:
        await sim.close()


async def run_wal_starvation(base: Path, seed: int, leak: bool) -> dict[str, Any]:
    """One seeded WC-9 run; per-seed oracle asserts inside.

    Phase 1 runs the (possibly faulted) leaker alone — the R4 interleaving
    puts the leak FIRST so the red arm's poisoning is total by
    construction. Phase 2 releases writers, readers, and the checkpointer
    under the same scheduler and RNG.
    """
    sim_clock = SimClock()
    rng = Random(seed)
    trace: list[TraceEvent] = []
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / f"wal-{'red' if leak else 'control'}-{seed}.db"
    project = "WalProj"

    setup = await open_sim_db(db_path, sim_clock, rng, trace)
    await setup.close()

    scheduler = SimScheduler(rng, trace)
    faults = (
        FaultPlan([_FTS_ERROR], rng, trace, liveness_p=1.0, fire_p=1.0)
        if leak
        else None
    )
    leaker_conn = SimConnection(
        db_path,
        sim_clock,
        rng,
        trace,
        scheduler=scheduler,
        writer_id="leaker",
        faults=faults,
    )

    clock.install(sim_clock.now)
    try:
        phase1 = await scheduler.run({"leaker": _leaker(leaker_conn, project)})
        leak_result = phase1["leaker"]

        writers: dict[str, Any] = {
            "w1": _activity_writer(
                "w1", db_path, sim_clock, rng, trace, scheduler, "codex", project
            ),
            "w2": _activity_writer(
                "w2", db_path, sim_clock, rng, trace, scheduler, "notion_os", project
            ),
            "rd1": _reader("rd1", db_path, sim_clock, rng, trace, scheduler),
            "rd2": _reader("rd2", db_path, sim_clock, rng, trace, scheduler),
            "ck": _checkpointer(db_path, sim_clock, rng, trace, scheduler),
        }
        async with asyncio.timeout(60):
            results = await scheduler.run(writers, max_grants=GRANT_BUDGET)
    finally:
        clock.reset()
        leaker_conn.teardown()

    attempts: list[dict[str, Any]] = results["ck"] or []
    truncated_attempts = [a for a in attempts if a["busy"] == 0]
    starved = sorted(w for w in ("w1", "w2") if results[w] is None)

    if leak:
        # INV-10 fires at the shim probe: the tool raised out with its
        # transaction still open. Expected-RED on current code — nothing
        # in log_activity rolls back on an FTS failure.
        assert leak_result["injected"], f"seed {seed}: declared fault did not fire"
        assert leak_result["tx_open_at_return"], f"seed {seed}: no leak"
        # The wall is total: writers starve, TRUNCATE never completes.
        assert starved == ["w1", "w2"], f"seed {seed}: starved={starved}"
        assert len(attempts) == CHECKPOINT_ATTEMPTS
        assert not truncated_attempts, f"seed {seed}: checkpoint won behind a leak"
    else:
        assert not leak_result["injected"] and not leak_result["tx_open_at_return"]
        assert results["w1"] == {"outcome": "completed"}, f"seed {seed}"
        assert results["w2"] == {"outcome": "completed"}, f"seed {seed}"
        assert len(attempts) == CHECKPOINT_ATTEMPTS
        # Post-checkpoint WAL size is bounded (0 after TRUNCATE) whenever
        # a checkpoint completed with no transaction open.
        for attempt in truncated_attempts:
            assert attempt["wal_size"] == 0, f"seed {seed}: {attempt}"

    return {
        "leak": leak_result,
        "starved": starved,
        "truncated": len(truncated_attempts),
        "hash": trace_hash(trace),
    }


async def run_wal_starvation_red(base: Path, seed: int) -> dict[str, Any]:
    return await run_wal_starvation(base, seed, leak=True)


async def run_wal_starvation_control(base: Path, seed: int) -> dict[str, Any]:
    return await run_wal_starvation(base, seed, leak=False)


async def test_leaked_tx_poisons_batch_liveness(tmp_path: Path) -> None:
    """WC-9 arm red: every seed leaks (INV-10 fires at the probe), starves
    both writers, and defeats every TRUNCATE — sometimes("wal_truncated")
    is 0 across the whole poisoned batch, INV-11 red at the batch report."""
    before = sometimes_counts().get(_TRUNCATED_LABEL, 0)
    for seed in SEED_BATCH:
        await run_wal_starvation(tmp_path, seed, leak=True)
    assert sometimes_counts().get(_TRUNCATED_LABEL, 0) == before


async def test_control_batch_liveness_holds(tmp_path: Path) -> None:
    """WC-9 arm green: same actors, no leak — across the batch some
    schedule always leaves a reader-free moment, TRUNCATE completes there,
    and the batch counter is nonzero (INV-11 holds)."""
    before = sometimes_counts().get(_TRUNCATED_LABEL, 0)
    truncated_total = 0
    for seed in SEED_BATCH:
        outcome = await run_wal_starvation(tmp_path, seed, leak=False)
        truncated_total += outcome["truncated"]
    assert truncated_total > 0, "no checkpoint completed across the control batch"
    assert sometimes_counts().get(_TRUNCATED_LABEL, 0) == before + truncated_total


async def test_wal_starvation_replay_is_bit_identical(tmp_path: Path) -> None:
    """G1 extended to the budget-drained arm: cancellation at the grant
    budget is itself a deterministic, replayable event."""
    first = await run_wal_starvation(tmp_path / "a", PINNED_SEED, leak=True)
    second = await run_wal_starvation(tmp_path / "b", PINNED_SEED, leak=True)
    assert first["hash"] == second["hash"]
    assert first["starved"] == second["starved"]
    assert first["truncated"] == second["truncated"]
