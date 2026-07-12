"""Phase 2 scenario RC-3: the section-CAS lost update (R4 §A) — now closed.

Two writers race on section S: one blind (no if_match), one honest CAS
writer (if_match_version). CAS became unconditional for existing rows on
2026-07-12, when the ``BRIDGE_DB_CONTEXT_CAS_MODE`` canary was cut after a
live-caller audit found zero blind writers: a blind write against an
existing row is now rejected with a durable ``missing_cas`` receipt on
every interleaving, not just under a config dial. The lost-update shape
this file used to reproduce under ``warn`` mode is therefore categorically
unreachable — INV-4 is a permanent invariant, not a mode-gated one.

HISTORY: the config knob ("warn" vs "enforce") and the pinned warn-mode
lost-update seed (17/30 sweep seeds reached the lost update) are archived
in git history as the evidence that flipped the shipped default to
`enforce` on 2026-07-10, and then motivated cutting the knob entirely two
days later once a live-caller audit found nothing still depended on
`warn`. See the pre-2026-07-12 revision of this file for the archived
warn-arm scenario.

``stale_cas`` is a separate, still-live mechanism untouched by the canary
cut — two HONEST CAS writers (both holding an ``if_match_version`` token)
can still race each other; the loser gets ``stale_cas``, not
``missing_cas``. Since a blind writer can no longer win a race against an
existing row, this file's ``stale_cas`` coverage moves to a cas-vs-cas
scenario below.
"""

import asyncio
import sqlite3
from pathlib import Path
from random import Random
from typing import Any, cast

import aiosqlite
from conftest import CaptureMCP, make_ctx

from bridge_db import clock
from bridge_db.invariants import sometimes_counts
from bridge_db.tools import context as context_mod
from dst.sim import (
    SimClock,
    SimConnection,
    SimScheduler,
    TraceEvent,
    open_sim_db,
    trace_hash,
)

# missing_cas is now unconditional, not race-dependent — any seed reaches
# it. Pinned for bit-identical replay coverage (G1).
MISSING_CAS_SEED = 0
SEED_SWEEP = range(0, 30)

# Two honest CAS writers, both reading the baseline version: pinned by the
# 2026-07-12 discovery sweep (29/30 seeds produce a stale_cas loser). Seed
# 10 schedules the writers sequentially instead of concurrently and is kept
# below as the non-race control.
STALE_CAS_SEED = 0
SEQUENTIAL_CONTROL_SEED = 10

SECTION = "career"


def context_fns() -> dict[str, Any]:
    cap = CaptureMCP()
    context_mod.register(cap)
    return cap.fns


async def _blind_writer(
    writer_id: str,
    db_path: Path,
    sim_clock: SimClock,
    rng: Random,
    trace: list[TraceEvent],
    scheduler: SimScheduler,
    caller: str,
) -> dict[str, Any]:
    sim = SimConnection(
        db_path, sim_clock, rng, trace, scheduler=scheduler, writer_id=writer_id
    )
    fns = context_fns()
    ctx = make_ctx(cast(aiosqlite.Connection, sim))
    try:
        read = await fns["get_section"](section_name=SECTION, ctx=ctx)
        result = await fns["update_section"](
            caller=caller,
            section_name=SECTION,
            content=f"content from {writer_id}",
            ctx=ctx,
        )
        return {
            "ok": result["ok"],
            "read_version": read["version"],
            "reason_code": result.get("reason_code"),
        }
    finally:
        await sim.close()


async def _cas_writer(
    writer_id: str,
    db_path: Path,
    sim_clock: SimClock,
    rng: Random,
    trace: list[TraceEvent],
    scheduler: SimScheduler,
    caller: str,
) -> dict[str, Any]:
    sim = SimConnection(
        db_path, sim_clock, rng, trace, scheduler=scheduler, writer_id=writer_id
    )
    fns = context_fns()
    ctx = make_ctx(cast(aiosqlite.Connection, sim))
    try:
        read = await fns["get_section"](section_name=SECTION, ctx=ctx)
        result = await fns["update_section"](
            caller=caller,
            section_name=SECTION,
            content=f"content from {writer_id}",
            if_match_version=read["version"],
            ctx=ctx,
        )
        return {
            "ok": result["ok"],
            "read_version": read["version"],
            "reason_code": result.get("reason_code"),
        }
    finally:
        await sim.close()


async def _seed_baseline(
    db_path: Path, sim_clock: SimClock, rng: Random, trace: list[TraceEvent]
) -> None:
    setup = await open_sim_db(db_path, sim_clock, rng, trace)
    fns = context_fns()
    setup_ctx = make_ctx(cast(aiosqlite.Connection, setup))
    seeded = await fns["update_section"](
        caller="claude_ai",
        section_name=SECTION,
        content="baseline content",
        ctx=setup_ctx,
    )
    assert seeded["ok"]
    await setup.close()


async def run_blind_vs_cas(base: Path, seed: int) -> dict[str, Any]:
    """One seeded run: a blind writer races an honest CAS writer against an
    existing section. missing_cas is unconditional — the blind writer must
    lose on every interleaving, and the one rejected write leaves exactly
    one durable receipt (INV-5)."""
    sim_clock = SimClock()
    rng = Random(seed)
    trace: list[TraceEvent] = []
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / f"blind-vs-cas-{seed}.db"

    clock.install(sim_clock.now)
    try:
        await _seed_baseline(db_path, sim_clock, rng, trace)
        scheduler = SimScheduler(rng, trace)
        writers = {
            "blind": _blind_writer(
                "blind", db_path, sim_clock, rng, trace, scheduler, "cc"
            ),
            "cas": _cas_writer(
                "cas", db_path, sim_clock, rng, trace, scheduler, "codex"
            ),
        }
        async with asyncio.timeout(30):
            results = await scheduler.run(writers)
    finally:
        clock.reset()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT content, version FROM context_sections WHERE section_name = ?",
        (SECTION,),
    ).fetchone()
    rejection_receipts = conn.execute(
        "SELECT COUNT(*) AS n FROM write_conflicts "
        "WHERE operation = 'update_section' AND reason = 'missing_cas'"
    ).fetchone()["n"]
    conn.close()

    blind_result, cas_result = results["blind"], results["cas"]
    assert row is not None
    # INV-4 CONTROL, now unconditional: the blind write can never win
    # against an existing row, on any interleaving.
    assert not blind_result["ok"], f"seed {seed}: blind write accepted"
    assert blind_result["reason_code"] == "missing_cas", f"seed {seed}"
    assert cas_result["ok"], f"seed {seed}: honest CAS writer was rejected"
    assert row["content"] == "content from cas", f"seed {seed}"
    assert rejection_receipts == 1, f"seed {seed}"

    return {
        "blind": blind_result,
        "cas": cas_result,
        "final_content": row["content"],
        "final_version": row["version"],
        "hash": trace_hash(trace),
    }


async def run_cas_vs_cas(base: Path, seed: int) -> dict[str, Any]:
    """One seeded run: two honest CAS writers race against an existing
    section. Unlike the blind writer above, both hold a real CAS token —
    exactly one can win when they read the same version; the loser gets a
    stale_cas receipt (not missing_cas). This is the corpus-reachable
    stale_cas scenario now that a blind writer can no longer produce it
    (see module docstring)."""
    sim_clock = SimClock()
    rng = Random(seed)
    trace: list[TraceEvent] = []
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / f"cas-vs-cas-{seed}.db"

    clock.install(sim_clock.now)
    try:
        await _seed_baseline(db_path, sim_clock, rng, trace)
        scheduler = SimScheduler(rng, trace)
        writers = {
            "a": _cas_writer("a", db_path, sim_clock, rng, trace, scheduler, "cc"),
            "b": _cas_writer("b", db_path, sim_clock, rng, trace, scheduler, "codex"),
        }
        async with asyncio.timeout(30):
            results = await scheduler.run(writers)
    finally:
        clock.reset()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT content, version FROM context_sections WHERE section_name = ?",
        (SECTION,),
    ).fetchone()
    stale_receipts = conn.execute(
        "SELECT COUNT(*) AS n FROM write_conflicts "
        "WHERE operation = 'update_section' AND reason = 'stale_cas'"
    ).fetchone()["n"]
    conn.close()

    a_result, b_result = results["a"], results["b"]
    both_ok = a_result["ok"] and b_result["ok"]
    same_read = a_result["read_version"] == b_result["read_version"]

    assert row is not None
    # Exactly one writer wins when both read the same version; a genuine
    # lost update between two honest CAS writers must stay unreachable.
    assert not (both_ok and same_read), (
        f"seed {seed}: lost update between two CAS writers"
    )

    return {
        "a": a_result,
        "b": b_result,
        "same_read": same_read,
        "both_ok": both_ok,
        "final_content": row["content"],
        "final_version": row["version"],
        "stale_receipts": stale_receipts,
        "hash": trace_hash(trace),
    }


async def test_missing_cas_blind_write_always_rejected(tmp_path: Path) -> None:
    """RC-3 acceptance (post-cut): the pinned seed rejects the blind write
    with a durable missing_cas receipt and the honest CAS writer's content
    survives."""
    outcome = await run_blind_vs_cas(tmp_path, MISSING_CAS_SEED)
    assert outcome["final_content"] == "content from cas"
    assert sometimes_counts().get("missing_cas_rejection", 0) >= 1


async def test_missing_cas_holds_across_full_seed_sweep(tmp_path: Path) -> None:
    """Non-vacuity, flipped from the old warn-mode sweep: missing_cas is no
    longer a config-gated possibility reached by one lucky seed — it must
    hold on every seed in the sweep, since the rejection no longer depends
    on interleaving."""
    for seed in SEED_SWEEP:
        await run_blind_vs_cas(tmp_path, seed)


async def test_stale_cas_between_two_cas_writers(tmp_path: Path) -> None:
    """stale_cas is untouched by the canary cut: two honest CAS writers
    that read the same version can still race, and the loser gets a
    stale_cas receipt while the winner's content lands."""
    outcome = await run_cas_vs_cas(tmp_path, STALE_CAS_SEED)
    assert outcome["same_read"]
    assert not outcome["both_ok"]
    assert outcome["stale_receipts"] == 1
    winner = "a" if outcome["a"]["ok"] else "b"
    assert outcome["final_content"] == f"content from {winner}"


async def test_cas_vs_cas_sequential_control_both_succeed(tmp_path: Path) -> None:
    """Control: when the scheduler happens to run the writers sequentially
    instead of concurrently, the second writer reads the first writer's
    committed version and both honest CAS writes succeed — proving the
    stale_cas above is a genuine race artifact, not something CAS always
    produces."""
    outcome = await run_cas_vs_cas(tmp_path, SEQUENTIAL_CONTROL_SEED)
    assert not outcome["same_read"]
    assert outcome["both_ok"]
    assert outcome["stale_receipts"] == 0


async def test_cas_pingpong_replay_is_bit_identical(tmp_path: Path) -> None:
    """G1 extended to both scenario shapes this file now covers: same seed,
    fresh runs, identical traces."""
    first = await run_blind_vs_cas(tmp_path / "a", MISSING_CAS_SEED)
    second = await run_blind_vs_cas(tmp_path / "b", MISSING_CAS_SEED)
    assert first["hash"] == second["hash"]

    third = await run_cas_vs_cas(tmp_path / "c", STALE_CAS_SEED)
    fourth = await run_cas_vs_cas(tmp_path / "d", STALE_CAS_SEED)
    assert third["hash"] == fourth["hash"]
