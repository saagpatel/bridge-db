"""Phase 2 scenario RC-3: the section-CAS lost update, warn vs enforce (R4 §A).

Two writers both read section S at the same version. B writes with the CAS
token and commits; A writes BLIND (legacy path, no token) and displaces
B's committed content. Under the shipped default ``CONTEXT_CAS_MODE=warn``
the blind write is accepted with only a flag — two writers who read one
version both get ok:True and B's work is silently gone. Under ``enforce``
the same interleaving rejects A's blind write with a durable receipt.

GAP LEDGER (INV-4, "PARTIAL — violated by design in the default config"):
the pinned warn-mode seed IS R3's named deliverable — the evidence case
for flipping the default to enforce. The enforce arm on the same seed is
the CONTROL showing the invariant holds when the config does its job.
"""

import asyncio
import sqlite3
from pathlib import Path
from random import Random
from typing import Any, cast

import aiosqlite
from conftest import CaptureMCP, make_ctx

from bridge_db import clock, config
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

# Pinned by the 2026-07-10 discovery sweep (17/30 seeds reached the lost
# update — the warn-mode hole is wide): both writers read the same version,
# the CAS commit lands first, the blind write displaces it.
LOST_UPDATE_SEED = 2
SEED_SWEEP = range(0, 30)
SECTION = "career"


def context_fns() -> dict[str, Any]:
    cap = CaptureMCP()
    context_mod.register(cap)
    return cap.fns


async def _section_writer(
    writer_id: str,
    db_path: Path,
    sim_clock: SimClock,
    rng: Random,
    trace: list[TraceEvent],
    scheduler: SimScheduler,
    caller: str,
    blind: bool,
) -> dict[str, Any]:
    sim = SimConnection(
        db_path, sim_clock, rng, trace, scheduler=scheduler, writer_id=writer_id
    )
    fns = context_fns()
    ctx = make_ctx(cast(aiosqlite.Connection, sim))
    try:
        read = await fns["get_section"](section_name=SECTION, ctx=ctx)
        cas_kwargs: dict[str, Any] = (
            {} if blind else {"if_match_version": read["version"]}
        )
        result = await fns["update_section"](
            caller=caller,
            section_name=SECTION,
            content=f"content from {writer_id}",
            ctx=ctx,
            **cas_kwargs,
        )
        return {
            "ok": result["ok"],
            "read_version": read["version"],
            "reason_code": result.get("reason_code"),
            "legacy_blind_write": bool(result.get("legacy_blind_write")),
        }
    finally:
        await sim.close()


async def run_cas_pingpong(base: Path, seed: int, mode: str = "warn") -> dict[str, Any]:
    """One seeded RC-3 run under the given CAS mode; per-seed oracle inside.

    The oracle is mode-aware: in ``enforce`` the lost update must be
    impossible on every seed (INV-4 CONTROL); in ``warn`` it is permitted
    by design and reported back for the arm tests to pin. In both modes
    every rejected write must have left exactly one durable receipt.
    """
    sim_clock = SimClock()
    rng = Random(seed)
    trace: list[TraceEvent] = []
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / f"pingpong-{mode}-{seed}.db"

    setup = await open_sim_db(db_path, sim_clock, rng, trace)
    fns = context_fns()
    setup_ctx = make_ctx(cast(aiosqlite.Connection, setup))

    previous_mode = config.CONTEXT_CAS_MODE
    config.CONTEXT_CAS_MODE = mode
    clock.install(sim_clock.now)
    try:
        seeded = await fns["update_section"](
            caller="claude_ai",
            section_name=SECTION,
            content="baseline content",
            ctx=setup_ctx,
        )
        assert seeded["ok"]
        await setup.close()

        scheduler = SimScheduler(rng, trace)
        writers = {
            "blind": _section_writer(
                "blind", db_path, sim_clock, rng, trace, scheduler, "cc", blind=True
            ),
            "cas": _section_writer(
                "cas", db_path, sim_clock, rng, trace, scheduler, "codex", blind=False
            ),
        }
        async with asyncio.timeout(30):
            results = await scheduler.run(writers)
    finally:
        clock.reset()
        config.CONTEXT_CAS_MODE = previous_mode

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT content, version FROM context_sections WHERE section_name = ?",
        (SECTION,),
    ).fetchone()
    receipts = conn.execute(
        "SELECT COUNT(*) AS n FROM write_conflicts WHERE operation = 'update_section'"
    ).fetchone()["n"]
    conn.close()

    blind_result, cas_result = results["blind"], results["cas"]
    both_ok = blind_result["ok"] and cas_result["ok"]
    lost_update = both_ok and (
        blind_result["read_version"] == cas_result["read_version"]
    )
    rejected = sum(1 for r in results.values() if not r["ok"])

    assert row is not None
    # Every rejected write leaves exactly one durable receipt, either mode.
    assert receipts == rejected, f"seed {seed} mode={mode}"
    if mode == "enforce":
        # INV-4 CONTROL: a blind write can never displace committed work —
        # the lost-update shape must be unreachable on every seed.
        assert not blind_result["ok"], f"seed {seed}: blind write accepted in enforce"
        assert not lost_update, f"seed {seed}"

    return {
        "blind": blind_result,
        "cas": cas_result,
        "lost_update": lost_update,
        "final_content": row["content"],
        "final_version": row["version"],
        "receipts": receipts,
        "hash": trace_hash(trace),
    }


async def run_cas_pingpong_warn(base: Path, seed: int) -> dict[str, Any]:
    return await run_cas_pingpong(base, seed, mode="warn")


async def run_cas_pingpong_enforce(base: Path, seed: int) -> dict[str, Any]:
    return await run_cas_pingpong(base, seed, mode="enforce")


async def test_warn_mode_lost_update_rederived(tmp_path: Path) -> None:
    """RC-3 acceptance, arm 1: the pinned seed reproduces the warn-mode lost
    update — both writers read one version, both got ok:True, the CAS
    writer's committed content is displaced by the blind write with zero
    conflict receipts. INV-4 RED: the evidence case for flipping the
    default to enforce."""
    outcome = await run_cas_pingpong(tmp_path, LOST_UPDATE_SEED, mode="warn")
    assert outcome["lost_update"]
    assert outcome["blind"]["legacy_blind_write"]
    assert outcome["final_content"] == "content from blind"
    assert outcome["receipts"] == 0  # the displacement is silent — the gap
    assert sometimes_counts().get("legacy_blind_write_accepted", 0) >= 1


async def test_enforce_mode_same_seed_holds(tmp_path: Path) -> None:
    """RC-3 acceptance, arm 2: the same seed under enforce — the blind
    write is rejected with a durable missing_cas receipt, exactly one
    writer wins, INV-4 holds."""
    outcome = await run_cas_pingpong(tmp_path, LOST_UPDATE_SEED, mode="enforce")
    assert not outcome["blind"]["ok"]
    assert outcome["blind"]["reason_code"] == "missing_cas"
    assert outcome["cas"]["ok"]
    assert outcome["final_content"] == "content from cas"
    assert outcome["receipts"] == 1
    assert sometimes_counts().get("missing_cas_rejection", 0) >= 1


async def test_lost_update_reachable_across_sweep(tmp_path: Path) -> None:
    """Non-vacuity: the warn-mode sweep must reach the lost-update shape on
    at least one seed — otherwise the arm-1 pin is a fluke of one
    interleaving rather than a reachable behavior."""
    lost_seeds: list[int] = []
    for seed in SEED_SWEEP:
        outcome = await run_cas_pingpong(tmp_path, seed, mode="warn")
        if outcome["lost_update"]:
            lost_seeds.append(seed)
    print(f"lost-update seeds: {lost_seeds}")
    assert lost_seeds, "no seed in the sweep produced the warn-mode lost update"


async def test_cas_pingpong_replay_is_bit_identical(tmp_path: Path) -> None:
    """G1 extended to config arms: same seed, fresh runs, identical traces."""
    first = await run_cas_pingpong(tmp_path / "a", LOST_UPDATE_SEED, mode="warn")
    second = await run_cas_pingpong(tmp_path / "b", LOST_UPDATE_SEED, mode="warn")
    assert first["hash"] == second["hash"]
    assert first["lost_update"] == second["lost_update"]
