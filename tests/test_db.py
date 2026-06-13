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
        "shipped_event_dispositions",
        "shipped_sync_receipts",
        "system_snapshots",
    }


async def test_schema_creates_indexes(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name")
    indexes = {row[0] for row in await cursor.fetchall()}
    assert "idx_activity_source" in indexes
    assert "idx_activity_timestamp" in indexes
    assert "idx_snapshot_system" in indexes
    assert "idx_handoff_status" in indexes
    assert "idx_shipped_disposition_type" in indexes
    assert "idx_shipped_sync_downstream" in indexes


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


async def test_migration_v2_to_current_populates_content_index(tmp_path: Path) -> None:
    """A v2 DB gains current tables and backfills content_index from source rows."""
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

        cursor = await migrated.execute("SELECT COUNT(*) FROM shipped_sync_receipts")
        receipt_row = await cursor.fetchone()
        assert receipt_row is not None
        assert receipt_row[0] == 0

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


async def test_migration_v3_to_v4_adds_shipped_sync_receipts(tmp_path: Path) -> None:
    """A v3 DB gains receipt storage without changing existing activity rows."""
    db = await aiosqlite.connect(str(tmp_path / "v3.db"))
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
        CREATE VIRTUAL TABLE content_index USING fts5(
            source_type UNINDEXED,
            source_id UNINDEXED,
            text,
            tokenize = 'porter unicode61 remove_diacritics 2'
        );
        INSERT INTO activity_log (source, timestamp, project_name, summary, tags)
            VALUES ('codex', '2026-05-09', 'personal-ops', 'merged PR set', '["SHIPPED"]');
        PRAGMA user_version = 3;
    """)
    await db.commit()
    await db.close()

    migrated = await open_db(tmp_path / "v3.db")
    try:
        cursor = await migrated.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == SCHEMA_VERSION

        cursor = await migrated.execute("SELECT COUNT(*) FROM activity_log")
        activity_row = await cursor.fetchone()
        assert activity_row is not None
        assert activity_row[0] == 1

        await migrated.execute(
            """
            INSERT INTO shipped_sync_receipts (
                activity_id, downstream_system, downstream_ref, synced_by
            )
            VALUES (1, 'notion', 'page-123', 'codex')
            """
        )
        await migrated.commit()

        cursor = await migrated.execute("SELECT downstream_ref FROM shipped_sync_receipts")
        receipt = await cursor.fetchone()
        assert receipt is not None
        assert receipt["downstream_ref"] == "page-123"
    finally:
        await migrated.close()


async def test_migration_v5_to_v6_adds_handoff_canonical_key(tmp_path: Path) -> None:
    """A v5 DB gains pending_handoffs.canonical_key without losing existing rows.

    open_db migrates through to HEAD, so this v5 fixture must carry every
    instruction-bearing table the later steps touch (the v6→v7 step ALTERs all
    four). The version assertion below therefore reflects HEAD, not v6; a v6→v7
    regression can surface here first.
    """
    db = await aiosqlite.connect(str(tmp_path / "v5.db"))
    db.row_factory = aiosqlite.Row
    await db.executescript("""
        PRAGMA journal_mode=WAL;
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
        -- Sibling instruction-bearing tables that exist in any real v5 DB.
        -- Required because open_db migrates to HEAD and the v6→v7 step ALTERs them.
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
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            canonical_key TEXT
        );
        CREATE TABLE system_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        CREATE VIRTUAL TABLE content_index USING fts5(
            source_type UNINDEXED,
            source_id UNINDEXED,
            text,
            tokenize = 'porter unicode61 remove_diacritics 2'
        );
        INSERT INTO pending_handoffs (project_name, phase)
            VALUES ('MyProject', 'Phase 2');
        PRAGMA user_version = 5;
    """)
    await db.commit()
    await db.close()

    migrated = await open_db(tmp_path / "v5.db")
    try:
        cursor = await migrated.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == SCHEMA_VERSION

        # Existing row preserved, new column defaults to NULL.
        cursor = await migrated.execute("SELECT project_name, canonical_key FROM pending_handoffs")
        existing = await cursor.fetchone()
        assert existing is not None
        assert existing["project_name"] == "MyProject"
        assert existing["canonical_key"] is None

        # New writes can populate the column.
        await migrated.execute(
            "INSERT INTO pending_handoffs (project_name, canonical_key) VALUES (?, ?)",
            ("IncidentMgmt", "incidentmgmt"),
        )
        await migrated.commit()
        cursor = await migrated.execute(
            "SELECT canonical_key FROM pending_handoffs WHERE project_name = 'IncidentMgmt'"
        )
        new_row = await cursor.fetchone()
        assert new_row is not None
        assert new_row["canonical_key"] == "incidentmgmt"
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


# ── source_trust provenance (v6 → v7) ──────────────────────────────────────


async def _create_v6_fixture(db_path: Path) -> None:
    """Build a populated v6 DB: the four instruction-bearing tables at v6 shape
    (canonical_key on activity_log + pending_handoffs, no source_trust anywhere),
    the content_index FTS table, one seeded row per table, user_version = 6."""
    db = await aiosqlite.connect(str(db_path))
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
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            canonical_key TEXT
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
            status TEXT NOT NULL DEFAULT 'pending',
            canonical_key TEXT
        );
        CREATE VIRTUAL TABLE content_index USING fts5(
            source_type UNINDEXED,
            source_id UNINDEXED,
            text,
            tokenize = 'porter unicode61 remove_diacritics 2'
        );
        INSERT INTO context_sections (section_name, owner, content)
            VALUES ('career', 'claude_ai', 'Staff Engineer target');
        INSERT INTO activity_log (source, timestamp, project_name, summary)
            VALUES ('cc', '2026-06-07', 'bridge-db', 'landed F1 handoffs');
        INSERT INTO system_snapshots (system, snapshot_date, data)
            VALUES ('cc', '2026-06-07', '{"active_projects": ["bridge-db"]}');
        INSERT INTO pending_handoffs (project_name, phase)
            VALUES ('MyProject', 'Phase 2');
        PRAGMA user_version = 6;
    """)
    await db.commit()
    await db.close()


async def test_schema_source_trust_defaults_to_agent(db: aiosqlite.Connection) -> None:
    """Fresh DB: every instruction-bearing table defaults source_trust to 'agent'."""
    await db.execute(
        "INSERT INTO context_sections (section_name, owner, content) "
        "VALUES ('career', 'claude_ai', 'x')"
    )
    await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary) "
        "VALUES ('cc', '2026-06-10', 'bridge-db', 'work')"
    )
    await db.execute(
        "INSERT INTO system_snapshots (system, snapshot_date, data) "
        "VALUES ('cc', '2026-06-10', '{}')"
    )
    await db.execute("INSERT INTO pending_handoffs (project_name) VALUES ('bridge-db')")
    await db.commit()

    for table in ("context_sections", "activity_log", "system_snapshots", "pending_handoffs"):
        cursor = await db.execute(f"SELECT source_trust FROM {table}")
        row = await cursor.fetchone()
        assert row is not None
        assert row["source_trust"] == "agent", f"{table} did not default to 'agent'"


_TRUST_TABLES = ("context_sections", "activity_log", "system_snapshots", "pending_handoffs")


async def _insert_with_trust(db: aiosqlite.Connection, table: str, trust: str) -> None:
    """Insert one minimal valid row into `table` with an explicit source_trust.

    Each table has a distinct NOT NULL column set; `trust` is embedded in the
    natural/PK key so repeated inserts on context_sections (PK section_name)
    don't collide.
    """
    key = f"{table}-{trust}"
    if table == "context_sections":
        await db.execute(
            "INSERT INTO context_sections (section_name, owner, content, source_trust) "
            "VALUES (?, 'claude_ai', 'x', ?)",
            (key, trust),
        )
    elif table == "activity_log":
        await db.execute(
            "INSERT INTO activity_log (source, timestamp, project_name, summary, source_trust) "
            "VALUES ('cc', '2026-06-10', ?, 'w', ?)",
            (key, trust),
        )
    elif table == "system_snapshots":
        await db.execute(
            "INSERT INTO system_snapshots (system, snapshot_date, data, source_trust) "
            "VALUES ('cc', '2026-06-10', '{}', ?)",
            (trust,),
        )
    elif table == "pending_handoffs":
        await db.execute(
            "INSERT INTO pending_handoffs (project_name, source_trust) VALUES (?, ?)",
            (key, trust),
        )
    else:  # pragma: no cover - guards against a typo'd table name in a test
        raise AssertionError(f"unknown table {table}")


async def test_source_trust_accepts_all_valid_values(db: aiosqlite.Connection) -> None:
    """Every table's CHECK admits each SourceTrust value — guards against a
    misspelled or missing literal in any one of the four DDL CHECK clauses."""
    for table in _TRUST_TABLES:
        for trust in ("operator", "agent", "ingested"):
            await _insert_with_trust(db, table, trust)
    await db.commit()


async def test_source_trust_check_rejects_unknown_all_tables(db: aiosqlite.Connection) -> None:
    """Every table's CHECK rejects a value outside the SourceTrust set — guards
    against a dropped CHECK clause on any one of the four ALTER/DDL statements."""
    for table in _TRUST_TABLES:
        with pytest.raises(aiosqlite.IntegrityError):
            await _insert_with_trust(db, table, "untrusted")


async def test_migration_v6_to_v7_adds_source_trust(tmp_path: Path) -> None:
    """A v6 DB gains source_trust on all four tables with conservative backfill."""
    await _create_v6_fixture(tmp_path / "v6.db")

    migrated = await open_db(tmp_path / "v6.db")
    try:
        cursor = await migrated.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == SCHEMA_VERSION

        # Owner-authored history → 'operator'.
        for table in ("context_sections", "pending_handoffs"):
            cursor = await migrated.execute(f"SELECT source_trust FROM {table}")
            r = await cursor.fetchone()
            assert r is not None
            assert r["source_trust"] == "operator", f"{table} backfill should be 'operator'"

        # Agent-authored history keeps the default.
        for table in ("activity_log", "system_snapshots"):
            cursor = await migrated.execute(f"SELECT source_trust FROM {table}")
            r = await cursor.fetchone()
            assert r is not None
            assert r["source_trust"] == "agent", f"{table} backfill should stay 'agent'"

        # No data loss: seeded content survives the migration.
        cursor = await migrated.execute(
            "SELECT content FROM context_sections WHERE section_name = 'career'"
        )
        r = await cursor.fetchone()
        assert r is not None
        assert r["content"] == "Staff Engineer target"

        cursor = await migrated.execute(
            "SELECT phase FROM pending_handoffs WHERE project_name = 'MyProject'"
        )
        r = await cursor.fetchone()
        assert r is not None
        assert r["phase"] == "Phase 2"

        # The migration-defined CHECKs (separate SQL from _SCHEMA_DDL) must be
        # complete on every table: each admits all valid values and rejects unknowns.
        for table in _TRUST_TABLES:
            await _insert_with_trust(migrated, table, "ingested")
            with pytest.raises(aiosqlite.IntegrityError):
                await _insert_with_trust(migrated, table, "untrusted")
        await migrated.commit()
    finally:
        await migrated.close()


async def test_migration_v6_to_v7_is_idempotent(tmp_path: Path) -> None:
    """Re-opening a migrated v7 DB is a no-op — the version gate blocks re-running ALTER."""
    await _create_v6_fixture(tmp_path / "v6.db")

    first = await open_db(tmp_path / "v6.db")
    await first.close()

    # Second open must not raise "duplicate column name"; version stays at v7.
    second = await open_db(tmp_path / "v6.db")
    try:
        cursor = await second.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == SCHEMA_VERSION

        # Backfill is stable across the no-op re-open.
        cursor = await second.execute("SELECT source_trust FROM context_sections")
        r = await cursor.fetchone()
        assert r is not None
        assert r["source_trust"] == "operator"
    finally:
        await second.close()


async def test_migration_v7_to_v8_adds_shipped_event_dispositions(tmp_path: Path) -> None:
    """A v7 DB gains the non-receipt shipped-event disposition table."""
    db = await aiosqlite.connect(str(tmp_path / "v7.db"))
    db.row_factory = aiosqlite.Row
    await db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL CHECK(source IN ('cc', 'codex', 'claude_ai', 'notion_os', 'personal_ops')),
            timestamp TEXT NOT NULL,
            project_name TEXT NOT NULL,
            summary TEXT NOT NULL,
            branch TEXT,
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            canonical_key TEXT,
            source_trust TEXT NOT NULL DEFAULT 'agent' CHECK(source_trust IN ('operator', 'agent', 'ingested'))
        );
        CREATE TABLE shipped_sync_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_id INTEGER NOT NULL UNIQUE,
            downstream_system TEXT NOT NULL,
            downstream_ref TEXT NOT NULL,
            synced_by TEXT NOT NULL CHECK(synced_by IN ('cc', 'codex', 'claude_ai', 'notion_os', 'personal_ops')),
            synced_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            notes TEXT,
            FOREIGN KEY(activity_id) REFERENCES activity_log(id) ON DELETE CASCADE
        );
        INSERT INTO activity_log (source, timestamp, project_name, summary, tags)
            VALUES ('cc', '2026-06-13', 'fable-outputs', 'manual artifact', '["SHIPPED"]');
        PRAGMA user_version = 7;
    """)
    await db.commit()
    await db.close()

    migrated = await open_db(tmp_path / "v7.db")
    try:
        cursor = await migrated.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == SCHEMA_VERSION

        await migrated.execute(
            """
            INSERT INTO shipped_event_dispositions (
                activity_id, disposition_type, reason, decided_by
            )
            VALUES (1, 'unsynced_by_policy', 'experimental artifact', 'codex')
            """
        )
        await migrated.commit()

        cursor = await migrated.execute(
            "SELECT disposition_type FROM shipped_event_dispositions WHERE activity_id = 1"
        )
        disposition = await cursor.fetchone()
        assert disposition is not None
        assert disposition["disposition_type"] == "unsynced_by_policy"

        await migrated.execute(
            "INSERT INTO activity_log (source, timestamp, project_name, summary, tags) "
            "VALUES ('cc', '2026-06-13', 'fable-outputs', 'another artifact', '[\"SHIPPED\"]')"
        )
        await migrated.commit()

        with pytest.raises(aiosqlite.IntegrityError):
            await migrated.execute(
                """
                INSERT INTO shipped_event_dispositions (
                    activity_id, disposition_type, reason, decided_by
                )
                VALUES (2, 'bogus', 'invalid', 'codex')
                """
            )
    finally:
        await migrated.close()
