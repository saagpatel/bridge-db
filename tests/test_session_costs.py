"""Tests for session_costs table — schema, insert/read, UNIQUE constraint, and v8→v9 migration."""

from pathlib import Path

import aiosqlite
import pytest

from bridge_db.db import SCHEMA_VERSION, open_db

# ── Schema tests (fresh DB via shared db fixture) ──────────────────────────


async def test_session_costs_table_exists(db: aiosqlite.Connection) -> None:
    """session_costs is present in a freshly-created DB."""
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_costs'"
    )
    row = await cursor.fetchone()
    assert row is not None, "session_costs table not found"


async def test_session_costs_columns(db: aiosqlite.Connection) -> None:
    """session_costs has the expected column set."""
    cursor = await db.execute("PRAGMA table_info(session_costs)")
    cols = {row["name"] for row in await cursor.fetchall()}
    expected = {
        "id",
        "session_id",
        "project_name",
        "started_at",
        "cost_usd",
        "model_breakdown",
        "source",
        "recorded_at",
    }
    assert expected <= cols, f"Missing columns: {expected - cols}"


async def test_session_costs_indexes(db: aiosqlite.Connection) -> None:
    """idx_sc_project and idx_sc_started are created on fresh schema."""
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name IN ('idx_sc_project', 'idx_sc_started')"
    )
    found = {row[0] for row in await cursor.fetchall()}
    assert found == {"idx_sc_project", "idx_sc_started"}


# ── Insert / read-back ──────────────────────────────────────────────────────


async def test_session_costs_insert_and_read(db: aiosqlite.Connection) -> None:
    """A row inserted into session_costs can be read back with correct values."""
    await db.execute(
        """
        INSERT INTO session_costs (session_id, project_name, started_at, cost_usd, source)
        VALUES ('sess-abc-001', 'bridge-db', '2026-06-19T10:00:00Z', 1.23, 'cc')
        """
    )
    await db.commit()

    cursor = await db.execute(
        "SELECT session_id, project_name, cost_usd, source FROM session_costs WHERE session_id = 'sess-abc-001'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["session_id"] == "sess-abc-001"
    assert row["project_name"] == "bridge-db"
    assert abs(row["cost_usd"] - 1.23) < 1e-9
    assert row["source"] == "cc"


async def test_session_costs_null_project(db: aiosqlite.Connection) -> None:
    """project_name is nullable — a row without it inserts cleanly."""
    await db.execute(
        """
        INSERT INTO session_costs (session_id, started_at, cost_usd)
        VALUES ('sess-no-project', '2026-06-19T11:00:00Z', 0.05)
        """
    )
    await db.commit()

    cursor = await db.execute(
        "SELECT project_name FROM session_costs WHERE session_id = 'sess-no-project'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["project_name"] is None


# ── UNIQUE constraint ───────────────────────────────────────────────────────


async def test_session_costs_unique_session_id(db: aiosqlite.Connection) -> None:
    """Inserting a duplicate session_id raises IntegrityError."""
    await db.execute(
        """
        INSERT INTO session_costs (session_id, started_at, cost_usd)
        VALUES ('sess-dup', '2026-06-19T12:00:00Z', 2.00)
        """
    )
    await db.commit()

    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            """
            INSERT INTO session_costs (session_id, started_at, cost_usd)
            VALUES ('sess-dup', '2026-06-19T12:01:00Z', 3.00)
            """
        )
        await db.commit()


# ── v8 → v9 migration ──────────────────────────────────────────────────────


async def test_migration_v8_to_v9_adds_session_costs(tmp_path: Path) -> None:
    """A v8 DB gains the session_costs table after open_db migrates it."""
    # Build a minimal v8 fixture — must include every table that later
    # migration steps touch (v6→v7 ALTERs four tables; v7→v8 adds dispositions).
    db_path = tmp_path / "v8.db"
    raw = await aiosqlite.connect(str(db_path))
    raw.row_factory = aiosqlite.Row
    await raw.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            project_name TEXT NOT NULL,
            summary TEXT NOT NULL,
            branch TEXT,
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            canonical_key TEXT,
            source_trust TEXT NOT NULL DEFAULT 'agent'
        );
        CREATE TABLE context_sections (
            section_name TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            source_trust TEXT NOT NULL DEFAULT 'agent'
        );
        CREATE TABLE system_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            source_trust TEXT NOT NULL DEFAULT 'agent'
        );
        CREATE TABLE pending_handoffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL,
            project_path TEXT,
            roadmap_file TEXT,
            phase TEXT,
            dispatched_from TEXT NOT NULL DEFAULT 'claude_ai',
            dispatched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            picked_up_at TEXT,
            cleared_at TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            canonical_key TEXT,
            source_trust TEXT NOT NULL DEFAULT 'agent'
        );
        CREATE TABLE cost_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system TEXT NOT NULL,
            month TEXT NOT NULL,
            amount REAL NOT NULL,
            notes TEXT,
            recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            UNIQUE(system, month)
        );
        CREATE TABLE shipped_sync_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_id INTEGER NOT NULL UNIQUE,
            downstream_system TEXT NOT NULL,
            downstream_ref TEXT NOT NULL,
            synced_by TEXT NOT NULL,
            synced_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            notes TEXT
        );
        CREATE TABLE shipped_event_dispositions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_id INTEGER NOT NULL UNIQUE,
            disposition_type TEXT NOT NULL,
            policy_ref TEXT,
            reason TEXT NOT NULL,
            decided_by TEXT NOT NULL,
            decided_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            notes TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS content_index USING fts5(
            source_type UNINDEXED,
            source_id UNINDEXED,
            text,
            tokenize = 'porter unicode61 remove_diacritics 2'
        );
        PRAGMA user_version = 8;
    """)
    await raw.commit()
    await raw.close()

    migrated = await open_db(db_path)
    try:
        # Version advanced to HEAD
        cursor = await migrated.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == SCHEMA_VERSION  # 9

        # session_costs table exists
        cursor = await migrated.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_costs'"
        )
        tbl = await cursor.fetchone()
        assert tbl is not None, "session_costs not created by v8→v9 migration"

        # Can insert and read back
        await migrated.execute(
            """
            INSERT INTO session_costs (session_id, started_at, cost_usd)
            VALUES ('migration-test-sess', '2026-06-19T00:00:00Z', 9.99)
            """
        )
        await migrated.commit()

        cursor = await migrated.execute(
            "SELECT cost_usd FROM session_costs WHERE session_id = 'migration-test-sess'"
        )
        cost_row = await cursor.fetchone()
        assert cost_row is not None
        assert abs(cost_row["cost_usd"] - 9.99) < 1e-9
    finally:
        await migrated.close()


async def test_migration_v8_to_v9_is_idempotent(tmp_path: Path) -> None:
    """Re-opening a migrated v9 DB does not raise; version stays at 9."""
    db_path = tmp_path / "v8_idem.db"
    raw = await aiosqlite.connect(str(db_path))
    raw.row_factory = aiosqlite.Row
    await raw.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            project_name TEXT NOT NULL,
            summary TEXT NOT NULL,
            branch TEXT,
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            canonical_key TEXT,
            source_trust TEXT NOT NULL DEFAULT 'agent'
        );
        CREATE TABLE context_sections (
            section_name TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            source_trust TEXT NOT NULL DEFAULT 'agent'
        );
        CREATE TABLE system_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            source_trust TEXT NOT NULL DEFAULT 'agent'
        );
        CREATE TABLE pending_handoffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL,
            project_path TEXT,
            roadmap_file TEXT,
            phase TEXT,
            dispatched_from TEXT NOT NULL DEFAULT 'claude_ai',
            dispatched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            picked_up_at TEXT,
            cleared_at TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            canonical_key TEXT,
            source_trust TEXT NOT NULL DEFAULT 'agent'
        );
        CREATE TABLE cost_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system TEXT NOT NULL,
            month TEXT NOT NULL,
            amount REAL NOT NULL,
            notes TEXT,
            recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            UNIQUE(system, month)
        );
        CREATE TABLE shipped_sync_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_id INTEGER NOT NULL UNIQUE,
            downstream_system TEXT NOT NULL,
            downstream_ref TEXT NOT NULL,
            synced_by TEXT NOT NULL,
            synced_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            notes TEXT
        );
        CREATE TABLE shipped_event_dispositions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_id INTEGER NOT NULL UNIQUE,
            disposition_type TEXT NOT NULL,
            policy_ref TEXT,
            reason TEXT NOT NULL,
            decided_by TEXT NOT NULL,
            decided_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            notes TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS content_index USING fts5(
            source_type UNINDEXED,
            source_id UNINDEXED,
            text,
            tokenize = 'porter unicode61 remove_diacritics 2'
        );
        PRAGMA user_version = 8;
    """)
    await raw.commit()
    await raw.close()

    # First open migrates v8 → v9
    first = await open_db(db_path)
    await first.close()

    # Second open must not raise and version stays at 9
    second = await open_db(db_path)
    try:
        cursor = await second.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == SCHEMA_VERSION
    finally:
        await second.close()
