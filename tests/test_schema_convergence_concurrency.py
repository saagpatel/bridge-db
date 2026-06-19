"""Schema-convergence and concurrent-writer tests for bridge-db.

Test 1 — Schema convergence:
    A fresh-install DB (empty → ensure_schema) and a DB migrated from v1 (the
    oldest migration entry-point) both end at SCHEMA_VERSION.  Assert the
    resulting sqlite_master table/index DDL is identical after normalization.
    Fails if _SCHEMA_DDL and the migration chain ever drift apart.

Test 2 — Concurrent writer / WAL busy-timeout:
    Two connections to the same WAL DB.  Connection A holds an open write
    transaction; connection B attempts a write.  With busy_timeout=5000 ms and
    WAL mode, B should block then succeed after A commits — no corruption, no
    OperationalError("database is locked").
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import aiosqlite

from bridge_db.db import SCHEMA_VERSION, apply_pragmas, open_db

# ── helpers ──────────────────────────────────────────────────────────────────


def _normalize_ddl(sql: str) -> str:
    """Normalize a DDL string for structural comparison.

    sqlite_master stores each CREATE TABLE verbatim.  When columns are added
    via ALTER TABLE ADD COLUMN, SQLite splices the new column definition into
    the stored text in a way that can introduce a trailing space before the
    column separator, producing " ," instead of ",".  This is a serialization
    artifact — not a schema difference — so we normalize it away along with
    all other whitespace variation.

    Steps:
      1. Collapse all whitespace runs to a single space.
      2. Strip space before commas introduced by ALTER TABLE ADD COLUMN.
      3. Strip space before closing paren (same origin).
      4. Lower-case for case-insensitive comparison.
    """
    normalized = re.sub(r"\s+", " ", sql).strip()
    normalized = re.sub(r"\s+,", ",", normalized)
    normalized = re.sub(r"\s+\)", ")", normalized)
    return normalized.lower()


async def _dump_schema(db: aiosqlite.Connection) -> dict[str, str]:
    """Return {name: normalized_sql} for all user tables and explicit indexes.

    Excludes:
    - sqlite internal tables (sqlite_*)
    - FTS5 shadow tables (content_index_*)
    - The FTS5 virtual table itself (content_index) — its DDL is stable but
      the shadow-table names it generates are internal implementation details
      of the SQLite version and cannot be compared across connections.
    """
    cursor = await db.execute(
        """
        SELECT name, sql
        FROM   sqlite_master
        WHERE  type IN ('table', 'index')
          AND  name NOT LIKE 'sqlite_%'
          AND  name NOT LIKE 'content_index%'
          AND  sql IS NOT NULL
        ORDER  BY name
        """
    )
    rows = await cursor.fetchall()
    return {row[0]: _normalize_ddl(row[1]) for row in rows}


async def _build_v1_db(db_path: Path) -> None:
    """Construct a minimal but complete v1 DB.

    v1 is the oldest version the migration ladder supports (user_version=1).
    The schema below is intentionally the narrowest v1 shape: activity_log
    with the original 3-caller CHECK and cost_records with the 2-system CHECK.
    Other tables introduced later (context_sections, system_snapshots,
    pending_handoffs) are absent — the v1→v2 migration creates them.
    """
    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await db.executescript("""
        PRAGMA journal_mode=WAL;

        CREATE TABLE activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL CHECK(source IN ('cc', 'codex', 'claude_ai')),
            timestamp TEXT NOT NULL,
            project_name TEXT NOT NULL,
            summary TEXT NOT NULL,
            branch TEXT,
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE TABLE cost_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system TEXT NOT NULL CHECK(system IN ('cc', 'codex')),
            month TEXT NOT NULL,
            amount REAL NOT NULL,
            notes TEXT,
            recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            UNIQUE(system, month)
        );

        INSERT INTO activity_log (source, timestamp, project_name, summary)
            VALUES ('cc', '2026-01-01', 'legacy-project', 'v1 seed row');
        INSERT INTO cost_records (system, month, amount)
            VALUES ('cc', '2026-01', 10.0);

        PRAGMA user_version = 1;
    """)
    await db.commit()
    await db.close()


# ── Test 1: schema convergence ────────────────────────────────────────────────


async def test_fresh_vs_migrated_schema_convergence(tmp_path: Path) -> None:
    """Fresh-install DDL and the v1→HEAD migration chain produce identical schemas.

    If _SCHEMA_DDL gains a column (or table/index) that no migration step
    adds, the two dumps diverge and this test fails — catching the drift
    before it hits a live operator DB.
    """
    # Path A: fresh install on an empty DB.
    fresh_db = await open_db(tmp_path / "fresh.db")
    fresh_schema = await _dump_schema(fresh_db)
    await fresh_db.close()

    # Sanity: fresh DB reached the expected version.
    assert len(fresh_schema) > 0, "fresh schema dump is empty — open_db may not have run DDL"

    # Path B: build a v1 DB and migrate it all the way to SCHEMA_VERSION.
    await _build_v1_db(tmp_path / "v1.db")
    migrated_db = await open_db(tmp_path / "v1.db")

    # Confirm migration reached SCHEMA_VERSION before comparing schemas.
    cursor = await migrated_db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == SCHEMA_VERSION, f"migrated DB stopped at v{row[0]}, expected v{SCHEMA_VERSION}"

    # Seed data from v1 must survive migration — proves rows were not silently dropped.
    cursor = await migrated_db.execute(
        "SELECT project_name FROM activity_log WHERE project_name = 'legacy-project'"
    )
    seed_row = await cursor.fetchone()
    assert seed_row is not None, "v1 seed row was lost during migration"

    migrated_schema = await _dump_schema(migrated_db)
    await migrated_db.close()

    # Identify divergence explicitly so the failure message is actionable.
    fresh_names = set(fresh_schema)
    migrated_names = set(migrated_schema)

    only_in_fresh = fresh_names - migrated_names
    only_in_migrated = migrated_names - fresh_names
    ddl_mismatches = {
        name for name in fresh_names & migrated_names if fresh_schema[name] != migrated_schema[name]
    }

    assert not only_in_fresh, (
        f"Tables/indexes present in fresh install but missing from migrated DB: {only_in_fresh}"
    )
    assert not only_in_migrated, (
        f"Tables/indexes present in migrated DB but missing from fresh install: {only_in_migrated}"
    )
    assert not ddl_mismatches, (
        "DDL diverges between fresh install and migrated DB for: "
        + ", ".join(
            f"{n!r}\n  fresh:    {fresh_schema[n]!r}\n  migrated: {migrated_schema[n]!r}"
            for n in sorted(ddl_mismatches)
        )
    )


# ── Test 2: concurrent-writer / WAL busy-timeout ──────────────────────────────


async def test_concurrent_writers_wal_busy_then_succeed(tmp_path: Path) -> None:
    """Two concurrent writers on a WAL DB: the second blocks then succeeds.

    Protocol (deterministic, no sleeps):
    1. Both connections open with WAL mode + busy_timeout=5000 ms.
    2. Connection A opens a write transaction (BEGIN IMMEDIATE) and inserts a
       row but does NOT commit.
    3. An asyncio.Event signals connection B that the write lock is held.
    4. Connection B issues its own write.  In WAL mode a second writer can
       sometimes proceed concurrently (WAL allows one writer at a time but not
       two simultaneous BEGIN IMMEDIATEs); with busy_timeout it retries rather
       than raising immediately.
    5. Connection A commits, releasing the lock.
    6. Connection B's write completes without error.
    7. Both rows are verified in the DB — no data was corrupted or lost.

    The test asserts:
    - No OperationalError("database is locked") from connection B.
    - Both written rows are present and correct in the final DB.
    - The DB remains at SCHEMA_VERSION (no corruption).
    """
    db_path = tmp_path / "concurrent.db"

    # Seed the DB and close: open_db applies WAL + busy_timeout + schema.
    seed_conn = await open_db(db_path)
    await seed_conn.close()

    # Track whether B completed and what error (if any) it raised.
    b_error: list[Exception] = []
    b_done = asyncio.Event()
    a_lock_held = asyncio.Event()

    async def writer_a() -> None:
        """Hold a write transaction, signal B, then commit."""
        conn = await aiosqlite.connect(str(db_path))
        conn.row_factory = aiosqlite.Row
        await apply_pragmas(conn)
        try:
            # BEGIN IMMEDIATE acquires a reserved lock, blocking other writers.
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute(
                "INSERT INTO activity_log (source, timestamp, project_name, summary) "
                "VALUES ('cc', '2026-06-19', 'writer-a-project', 'writer-a inserted')"
            )
            # Signal B that the lock is held, then yield so B can attempt its write.
            a_lock_held.set()
            # Let B run before we commit.  asyncio.sleep(0) yields the event loop
            # without a real time dependency; B will block inside SQLite (C layer)
            # on the busy_timeout path, not in Python — so this is safe.
            await asyncio.sleep(0)
            await conn.commit()
        finally:
            await conn.close()

    async def writer_b() -> None:
        """Wait until A holds the lock, then attempt a write."""
        conn = await aiosqlite.connect(str(db_path))
        conn.row_factory = aiosqlite.Row
        await apply_pragmas(conn)
        try:
            await a_lock_held.wait()
            # This write will hit the SQLite busy handler (busy_timeout=5000).
            # In WAL mode a second simultaneous writer is permitted only after the
            # first commits; the C-level busy handler retries up to the timeout.
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute(
                "INSERT INTO activity_log (source, timestamp, project_name, summary) "
                "VALUES ('codex', '2026-06-19', 'writer-b-project', 'writer-b inserted')"
            )
            await conn.commit()
        except Exception as exc:
            b_error.append(exc)
        finally:
            await conn.close()
            b_done.set()

    # Run both writers concurrently.
    await asyncio.gather(writer_a(), writer_b())
    await b_done.wait()

    # B must have succeeded with no error.
    assert not b_error, (
        f"Writer B raised an error while A held the write lock: {b_error[0]!r}\n"
        "Expected: block via busy_timeout then succeed after A commits."
    )

    # Verify both rows are present — no silent data loss or corruption.
    verify_conn = await open_db(db_path)
    try:
        cursor = await verify_conn.execute(
            "SELECT project_name FROM activity_log ORDER BY project_name"
        )
        rows = await cursor.fetchall()
        project_names = {row["project_name"] for row in rows}

        assert "writer-a-project" in project_names, (
            "Writer A's row is missing — transaction may not have committed"
        )
        assert "writer-b-project" in project_names, (
            "Writer B's row is missing — concurrent write was lost or rolled back"
        )

        # Schema version unchanged — confirms no corruption.
        cursor = await verify_conn.execute("PRAGMA user_version")
        version_row = await cursor.fetchone()
        assert version_row is not None
        assert version_row[0] == SCHEMA_VERSION, (
            f"DB version changed to {version_row[0]} after concurrent writes — possible corruption"
        )
    finally:
        await verify_conn.close()
