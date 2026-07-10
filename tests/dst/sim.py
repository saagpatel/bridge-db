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

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from random import Random
from typing import Any

from bridge_db.db import apply_pragmas, ensure_schema

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
    ) -> None:
        self._clock = clock
        self._rng = rng
        self._trace = trace
        self._step = 0
        self._conn = sqlite3.connect(str(db_path))
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

    def _pre_op(self, op: str, sql: str) -> None:
        self._clock.tick(self._rng.randrange(0, 3))
        self._step += 1
        self._trace.append(
            {
                "step": self._step,
                "op": op,
                "sql": _fingerprint(sql),
                "clock": self._clock.now().isoformat(),
            }
        )

    async def execute(self, sql: str, parameters: Any = ()) -> SimCursor:
        self._pre_op("execute", sql)
        return SimCursor(self._conn.execute(sql, parameters))

    async def executescript(self, sql_script: str) -> SimCursor:
        self._pre_op("executescript", sql_script)
        return SimCursor(self._conn.executescript(sql_script))

    async def commit(self) -> None:
        self._pre_op("commit", "")
        self._conn.commit()

    async def rollback(self) -> None:
        self._pre_op("rollback", "")
        self._conn.rollback()

    async def close(self) -> None:
        self._pre_op("close", "")
        self._conn.close()


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
