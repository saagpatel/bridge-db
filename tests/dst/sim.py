"""Deterministic-simulation substrate (DST Phase 1, R3 §1-2).

Three pieces, all harness-side:

- ``SimClock`` — logical UTC time that advances only when ticked. Installed
  into ``bridge_db.clock`` it captures every Python-side timestamp; wired
  into ``SimConnection``'s ``strftime`` override it captures every SQL-side
  column default (probe-verified GO on sqlite 3.50.4 / CPython 3.12.13).
- ``SimConnection`` — satisfies the aiosqlite surface the tools use
  (execute/executescript/commit/rollback/close + cursor
  fetchone/fetchall/rowcount/lastrowid, per the Phase 1 usage audit) but
  executes on a synchronous ``sqlite3`` connection inline in the event
  loop. Removing aiosqlite's executor thread removes the one concurrency
  source the harness cannot seed; Phase 2's scheduler adds a yield point
  in ``_pre_op``.
- Trace — every connection op appends one structured event; ``trace_hash``
  is the replay-comparison key. With seeded time and a fixed op sequence,
  the final DB file is a pure function of (seed, scenario, git SHA).
"""

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from random import Random
from typing import Any, Literal, NoReturn

from bridge_db.db import apply_pragmas, ensure_schema
from bridge_db.invariants import sometimes

TraceEvent = dict[str, Any]


class SimClock:
    """Logical UTC clock: time moves only on tick()."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start if start is not None else datetime(2030, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def tick(self, seconds: int) -> None:
        self._now += timedelta(seconds=seconds)


def trace_hash(trace: list[TraceEvent]) -> str:
    return hashlib.sha256(
        json.dumps(trace, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _fingerprint(sql: str) -> str:
    return " ".join(sql.split())[:80]


class SimCrash(Exception):
    """Simulated process death (R3 §1.1): the writer's connection is gone
    mid-tool-call and its open transaction is discarded, exactly as if the
    OS killed the process between two commits."""


FaultOp = Literal["execute", "pre-commit"]
FaultKind = Literal["crash", "busy", "delay"]

# R3 §1.1 buggify probabilities: each point coin-flips live/dead once per
# run, then a live point fires at p=0.25 per hit — both off the run RNG.
LIVENESS_P = 0.5
FIRE_P = 0.25


@dataclass(frozen=True)
class FaultPoint:
    """One (statement-fingerprint, op-type) buggify site (R3 §1.1).

    ``match`` is a substring tested against the statement fingerprint. For
    ``op="pre-commit"`` it is tested against the last statement executed in
    the current transaction — that is what makes a two-op window like
    rollback→receipt-commit (WC-6) landable with precision.
    ``label`` names a ``sometimes()`` counter fired on every hit, the
    non-vacuity evidence that the fault actually landed (gate G5).
    """

    match: str
    op: FaultOp
    kind: FaultKind
    label: str | None = None


class FaultPlan:
    """Per-run fault decisions, drawn from the run RNG in a fixed order."""

    def __init__(
        self, points: Sequence[FaultPoint], rng: Random, trace: list[TraceEvent]
    ) -> None:
        self._rng = rng
        self._trace = trace
        # Liveness is decided once per run per point, in registration order,
        # so the draw sequence — and therefore the whole run — is seed-stable.
        self._live = [(point, rng.random() < LIVENESS_P) for point in points]

    def fire(self, op: FaultOp, fingerprint: str, writer_id: str) -> FaultPoint | None:
        for point, live in self._live:
            if not live or point.op != op or point.match not in fingerprint:
                continue
            if self._rng.random() >= FIRE_P:
                continue
            self._trace.append(
                {
                    "op": "fault",
                    "kind": point.kind,
                    "writer": writer_id,
                    "sql": fingerprint,
                }
            )
            if point.label is not None:
                sometimes(point.label)
            return point
        return None


class SimCursor:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid

    async def fetchone(self) -> Any:
        return self._cursor.fetchone()

    async def fetchall(self) -> Any:
        return self._cursor.fetchall()


class SimConnection:
    """Async-facade over a synchronous sqlite3 connection, seeded.

    Every op: draws a clock tick from the run's RNG (0..2s — zero-second
    draws deliberately explore timestamp collisions), appends a trace
    event, then executes inline. The per-connection ``strftime`` override
    routes SQL 'now' through SimClock; any non-'now' strftime raises
    loudly rather than silently diverging from the built-in.
    """

    def __init__(
        self,
        db_path: Path,
        clock: SimClock,
        rng: Random,
        trace: list[TraceEvent],
        scheduler: "SimScheduler | None" = None,
        writer_id: str = "w0",
        faults: FaultPlan | None = None,
    ) -> None:
        self._clock = clock
        self._rng = rng
        self._trace = trace
        self._scheduler = scheduler
        self._writer_id = writer_id
        self._faults = faults
        self._last_fingerprint = ""
        self._closed = False
        self._step = 0
        # timeout=0: SQLITE_BUSY surfaces immediately instead of blocking in
        # the C-layer busy handler — the retry becomes a scheduler-owned,
        # seeded event (R3 §1.1) rather than invisible wall-clock waiting.
        self._conn = sqlite3.connect(str(db_path), timeout=0)
        self._conn.row_factory = sqlite3.Row
        self._conn.create_function("strftime", -1, self._sim_strftime)

    def _sim_strftime(self, *args: object) -> str:
        if len(args) == 2 and args[1] == "now":
            fmt = str(args[0])
            now = self._clock.now()
            if fmt == "%s":
                return str(int(now.timestamp()))
            return now.strftime(fmt)
        raise NotImplementedError(
            f"sim strftime only models strftime(fmt,'now'); got args={args!r} — "
            "extend the model rather than letting it silently diverge"
        )

    async def _yield_and_trace(self, op: str, sql: str) -> None:
        if self._scheduler is not None:
            await self._scheduler.yield_point(self._writer_id)
        self._clock.tick(self._rng.randrange(0, 3))
        self._step += 1
        self._trace.append(
            {
                "step": self._step,
                "writer": self._writer_id,
                "op": op,
                "sql": _fingerprint(sql),
                "clock": self._clock.now().isoformat(),
            }
        )

    def _crash(self) -> NoReturn:
        # Process death: the open transaction dies with the connection —
        # anything staged since the last commit is discarded.
        self._conn.rollback()
        self._conn.close()
        self._closed = True
        raise SimCrash(f"writer {self._writer_id} crashed by fault injection")

    async def _maybe_fault(self, op: FaultOp, fingerprint: str) -> bool:
        """Apply any firing fault; True means the caller should retry the op."""
        if self._faults is None:
            return False
        point = self._faults.fire(op, fingerprint, self._writer_id)
        if point is None:
            return False
        if point.kind == "crash":
            self._crash()
        if point.kind == "busy":
            return True  # injected BUSY: park again and retry, like the real one
        # delay: one forced extra scheduling round before the op proceeds
        await self._yield_and_trace("delay", fingerprint)
        return False

    async def execute(self, sql: str, parameters: Any = ()) -> SimCursor:
        while True:
            await self._yield_and_trace("execute", sql)
            fingerprint = _fingerprint(sql)
            self._last_fingerprint = fingerprint
            if await self._maybe_fault("execute", fingerprint):
                continue
            try:
                return SimCursor(self._conn.execute(sql, parameters))
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc):
                    raise
                # SQLITE_BUSY: a first-class schedulable event — park again
                # and retry when the scheduler next grants this writer.
                self._trace.append(
                    {"writer": self._writer_id, "op": "busy", "sql": fingerprint}
                )

    async def executescript(self, sql_script: str) -> SimCursor:
        await self._yield_and_trace("executescript", sql_script)
        self._last_fingerprint = _fingerprint(sql_script)
        return SimCursor(self._conn.executescript(sql_script))

    async def commit(self) -> None:
        while True:
            await self._yield_and_trace("commit", "")
            if await self._maybe_fault("pre-commit", self._last_fingerprint):
                continue
            try:
                self._conn.commit()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc):
                    raise
                self._trace.append({"writer": self._writer_id, "op": "busy_commit"})
                continue
            # Transaction boundary: pre-commit points key on statements of
            # the CURRENT tx only, so a later commit can't falsely match.
            self._last_fingerprint = ""
            return

    async def rollback(self) -> None:
        await self._yield_and_trace("rollback", "")
        self._conn.rollback()
        self._last_fingerprint = ""

    async def close(self) -> None:
        await self._yield_and_trace("close", "")
        if not self._closed:
            self._conn.close()
            self._closed = True


class SimScheduler:
    """Cooperative interleaving driver (R3 §1.3, Phase 2), seeded.

    Every writer task parks at every connection op (``yield_point``); this
    loop picks which parked writer proceeds using the run's RNG. All
    concurrency in a sim run is therefore an explicit, replayable sequence
    of grant events — there is no other scheduler.
    """

    def __init__(self, rng: Random, trace: list[TraceEvent]) -> None:
        self._rng = rng
        self._trace = trace
        self._parked: dict[str, asyncio.Event] = {}
        self._arrival = asyncio.Event()

    async def yield_point(self, writer_id: str) -> None:
        event = asyncio.Event()
        self._parked[writer_id] = event
        self._arrival.set()
        await event.wait()

    async def _finishing(self, coro: Coroutine[Any, Any, Any]) -> Any:
        try:
            return await coro
        finally:
            self._arrival.set()

    async def run(
        self, writers: Mapping[str, Coroutine[Any, Any, Any]]
    ) -> dict[str, Any]:
        """Drive writer coroutines to completion; return their results by id.

        A writer that raises propagates its exception here (writer scripts
        are expected to catch domain errors like ToolError themselves and
        return an outcome value instead).
        """
        tasks = {
            writer_id: asyncio.create_task(self._finishing(coro))
            for writer_id, coro in writers.items()
        }
        while True:
            live = [w for w, t in tasks.items() if not t.done()]
            if not live:
                break
            parked_live = sorted(w for w in live if w in self._parked)
            if len(parked_live) < len(live):
                # Some live writer is still running toward its next park
                # (or completion) — wait for the next arrival.
                self._arrival.clear()
                await self._arrival.wait()
                continue
            choice = self._rng.choice(parked_live)
            self._trace.append({"op": "grant", "writer": choice})
            self._parked.pop(choice).set()
        return {w: t.result() for w, t in tasks.items()}


async def open_sim_db(
    db_path: Path,
    clock: SimClock,
    rng: Random,
    trace: list[TraceEvent],
) -> SimConnection:
    """Open a SimConnection and build the real schema THROUGH the shim.

    Using the production apply_pragmas/ensure_schema on the sim surface
    keeps schema-time strftime defaults under the sim clock and proves the
    shim satisfies the production write path from the first statement.
    """
    sim = SimConnection(db_path, clock, rng, trace)
    await apply_pragmas(sim)  # type: ignore[arg-type]
    await ensure_schema(sim)  # type: ignore[arg-type]
    # R3 §1.1: SQLITE_BUSY is a scheduler-owned event in sim, never a
    # C-layer 15s wait. Single-connection in Phase 1, but set it now.
    await sim.execute("PRAGMA busy_timeout=0")
    return sim
