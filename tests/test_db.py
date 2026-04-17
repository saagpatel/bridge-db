"""Tests for DB schema creation, PRAGMAs, and migration idempotency."""

from pathlib import Path

import aiosqlite
import pytest

from bridge_db.db import SCHEMA_VERSION, ensure_schema, open_db


async def test_schema_creates_all_tables(db: aiosqlite.Connection) -> None:
    # FTS5 creates shadow tables (content_index_{data,config,content,docsize,idx})
    # that are internal; filter them out and assert on user-facing tables.
    cursor = await db.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='table'
          AND name NOT LIKE 'sqlite_%'
          AND name NOT LIKE 'content_index_%'
        ORDER BY name
        """
    )
    tables = {row[0] for row in await cursor.fetchall()}
    assert tables == {
        "activity_log",
        "content_index",
        "context_sections",
        "cost_records",
        "pending_handoffs",
        "system_snapshots",
    }


async def test_schema_creates_indexes(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")
    indexes = {row[0] for row in await cursor.fetchall()}
    assert "idx_activity_source" in indexes
    assert "idx_activity_timestamp" in indexes
    assert "idx_snapshot_system" in indexes
    assert "idx_handoff_status" in indexes


async def test_pragma_wal_mode(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA journal_mode")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "wal"


async def test_pragma_foreign_keys(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA foreign_keys")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 1


async def test_user_version_set(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == SCHEMA_VERSION


async def test_ensure_schema_is_idempotent(tmp_path: Path) -> None:
    """Running ensure_schema twice on the same DB does not error."""
    db = await open_db(tmp_path / "idempotent.db")
    await ensure_schema(db)  # second call
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == SCHEMA_VERSION
    await db.close()


async def test_open_db_creates_parent_dirs(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c" / "bridge.db"
    db = await open_db(nested)
    assert nested.exists()
    await db.close()


async def test_activity_log_source_check_constraint(db: aiosqlite.Connection) -> None:
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO activity_log (source, timestamp, project_name, summary) "
            "VALUES ('invalid_source', '2026-01-01', 'P', 'S')"
        )


async def test_pending_handoffs_status_check_constraint(db: aiosqlite.Connection) -> None:
    await db.execute("INSERT INTO pending_handoffs (project_name, status) VALUES ('P', 'pending')")
    await db.commit()
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO pending_handoffs (project_name, status) VALUES ('P2', 'bogus')"
        )


async def test_cost_records_unique_system_month(db: aiosqlite.Connection) -> None:
    await db.execute(
        "INSERT INTO cost_records (system, month, amount) VALUES ('cc', '2026-04', 100.0)"
    )
    await db.commit()
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO cost_records (system, month, amount) VALUES ('cc', '2026-04', 200.0)"
        )


async def test_activity_log_accepts_new_callers(db: aiosqlite.Connection) -> None:
    """notion_os and personal_ops must be accepted by the v2 CHECK constraint."""
    await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary) "
        "VALUES ('notion_os', '2026-04-14', 'P', 'S')"
    )
    await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary) "
        "VALUES ('personal_ops', '2026-04-14', 'P', 'S')"
    )
    await db.commit()
    cursor = await db.execute("SELECT COUNT(*) FROM activity_log")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 2


async def test_cost_records_accepts_new_systems(db: aiosqlite.Connection) -> None:
    """notion_os and personal_ops must be accepted by the v2 CHECK constraint."""
    await db.execute(
        "INSERT INTO cost_records (system, month, amount) VALUES ('notion_os', '2026-04', 5.0)"
    )
    await db.execute(
        "INSERT INTO cost_records (system, month, amount) VALUES ('personal_ops', '2026-04', 3.0)"
    )
    await db.commit()
    cursor = await db.execute("SELECT COUNT(*) FROM cost_records")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 2


async def test_migration_v1_to_v2(tmp_path: Path) -> None:
    """A v1 database gets migrated to v2 with expanded CHECK constraints."""
    # Build a minimal v1 schema manually
    db = await aiosqlite.connect(str(tmp_path / "v1.db"))
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
            VALUES ('cc', '2026-01-01', 'OldProject', 'legacy entry');
        INSERT INTO cost_records (system, month, amount) VALUES ('cc', '2026-01', 42.0);
        PRAGMA user_version = 1;
    """)
    await db.commit()
    await db.close()

    # Re-open via open_db — migration should run automatically
    migrated = await open_db(tmp_path / "v1.db")

    # Schema version bumped to 2
    cursor = await migrated.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == SCHEMA_VERSION

    # Old data preserved
    cursor = await migrated.execute("SELECT project_name FROM activity_log")
    rows: list[aiosqlite.Row] = await cursor.fetchall()  # type: ignore[assignment]
    assert len(rows) == 1
    assert rows[0]["project_name"] == "OldProject"

    # New callers now accepted
    await migrated.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary) "
        "VALUES ('notion_os', '2026-04-14', 'New', 'S')"
    )
    await migrated.commit()

    await migrated.close()


async def test_migration_v2_to_v3_populates_content_index(tmp_path: Path) -> None:
    """A v2 DB gains content_index on v3 migration and is backfilled from source rows."""
    db = await aiosqlite.connect(str(tmp_path / "v2.db"))
    db.row_factory = aiosqlite.Row
    await db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE context_sections (
            section_name TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        CREATE TABLE activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            project_name TEXT NOT NULL,
            summary TEXT NOT NULL,
            branch TEXT,
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        CREATE TABLE system_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
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
            status TEXT NOT NULL DEFAULT 'pending'
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
        INSERT INTO context_sections (section_name, owner, content)
            VALUES ('career', 'claude_ai', 'staff engineer trajectory');
        INSERT INTO activity_log (source, timestamp, project_name, summary)
            VALUES ('cc', '2026-04-17', 'bridge-db', 'Phase -1 scaffolding');
        INSERT INTO system_snapshots (system, snapshot_date, data)
            VALUES ('cc', '2026-04-17', '{"active_projects":"bridge-db"}');
        INSERT INTO pending_handoffs (project_name, project_path, phase)
            VALUES ('bridge-db', '/Users/d/Projects/bridge-db', 'Phase -1');
        PRAGMA user_version = 2;
    """)
    await db.commit()
    await db.close()

    migrated = await open_db(tmp_path / "v2.db")
    try:
        cursor = await migrated.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == SCHEMA_VERSION

        for table in ("context_sections", "activity_log", "system_snapshots", "pending_handoffs"):
            cursor = await migrated.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
            count_row = await cursor.fetchone()
            assert count_row is not None
            assert count_row[0] == 1, f"{table} count changed during migration"

        cursor = await migrated.execute(
            "SELECT source_type, source_id FROM content_index ORDER BY source_type"
        )
        rows = await cursor.fetchall()
        types_ids = [(r["source_type"], r["source_id"]) for r in rows]
        assert types_ids == [
            ("activity", "1"),
            ("handoff", "1"),
            ("section", "career"),
            ("snapshot", "1"),
        ]

        cursor = await migrated.execute(
            "SELECT COUNT(*) FROM content_index WHERE content_index MATCH 'bridge'"
        )
        match_row = await cursor.fetchone()
        assert match_row is not None
        assert match_row[0] >= 2
    finally:
        await migrated.close()


async def test_ensure_schema_rejects_future_db_version(tmp_path: Path) -> None:
    db_path = tmp_path / "future.db"
    db = await aiosqlite.connect(str(db_path))
    await db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    await db.commit()

    with pytest.raises(RuntimeError, match="newer than this bridge-db build supports"):
        await ensure_schema(db)

    await db.close()
