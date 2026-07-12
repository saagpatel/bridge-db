"""Tests for DB schema creation, PRAGMAs, and migration idempotency."""

from pathlib import Path

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx

from bridge_db import config
from bridge_db.db import SCHEMA_VERSION, ensure_schema, open_db
from bridge_db.tools import activity as activity_mod


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
        "context_section_export_state",
        "context_sections",
        "cost_records",
        "pending_handoffs",
        "session_classification",
        "session_costs",
        "system_snapshots",
        "write_conflicts",
    }


async def test_schema_creates_indexes(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
    )
    indexes = {row[0] for row in await cursor.fetchall()}
    assert "idx_activity_source" in indexes
    assert "idx_activity_timestamp" in indexes
    assert "idx_snapshot_system" in indexes
    assert "idx_handoff_status" in indexes
    assert "idx_sc_project" in indexes
    assert "idx_sc_started" in indexes
    assert "idx_scl_routing" in indexes
    assert "idx_write_conflicts_status_created" in indexes
    assert "idx_write_conflicts_surface_target" in indexes


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


async def test_pending_handoffs_status_check_constraint(
    db: aiosqlite.Connection,
) -> None:
    await db.execute(
        "INSERT INTO pending_handoffs (project_name, status) VALUES ('P', 'pending')"
    )
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
            VALUES ('bridge-db', '/home/user/Projects/bridge-db', 'Phase -1');
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

        for table in (
            "context_sections",
            "activity_log",
            "system_snapshots",
            "pending_handoffs",
        ):
            cursor = await migrated.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
            count_row = await cursor.fetchone()
            assert count_row is not None
            assert count_row[0] == 1, f"{table} count changed during migration"

        # Shipped-sync state collapsed onto activity_log columns at v14; a plain
        # migration creates no dispositions.
        cursor = await migrated.execute(
            "SELECT COUNT(*) FROM activity_log WHERE sync_disposition IS NOT NULL"
        )
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


async def test_migration_v3_to_head_preserves_activity_and_collapses_receipts(
    tmp_path: Path,
) -> None:
    """A v3 DB migrates to HEAD without changing existing activity rows; the
    shipped_sync_receipts table added at v4 is collapsed onto activity_log
    columns and dropped by v14."""
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

        # The transient receipt/disposition tables are gone at HEAD; sync state
        # lives on the activity row and is NULL until a disposition is recorded.
        cursor = await migrated.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('shipped_sync_receipts', 'shipped_event_dispositions')"
        )
        assert await cursor.fetchall() == []

        cursor = await migrated.execute(
            "SELECT sync_disposition FROM activity_log WHERE id = 1"
        )
        disp = await cursor.fetchone()
        assert disp is not None
        assert disp["sync_disposition"] is None
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
        cursor = await migrated.execute(
            "SELECT project_name, canonical_key FROM pending_handoffs"
        )
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

    for table in (
        "context_sections",
        "activity_log",
        "system_snapshots",
        "pending_handoffs",
    ):
        cursor = await db.execute(f"SELECT source_trust FROM {table}")
        row = await cursor.fetchone()
        assert row is not None
        assert row["source_trust"] == "agent", f"{table} did not default to 'agent'"


_TRUST_TABLES = (
    "context_sections",
    "activity_log",
    "system_snapshots",
    "pending_handoffs",
)


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


async def test_source_trust_check_rejects_unknown_all_tables(
    db: aiosqlite.Connection,
) -> None:
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
            assert r["source_trust"] == "operator", (
                f"{table} backfill should be 'operator'"
            )

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


async def test_migration_v7_to_head_collapses_disposition_state(
    tmp_path: Path,
) -> None:
    """A v7 DB migrates to HEAD; the disposition table added at v8 is collapsed
    onto activity_log's sync_disposition column and dropped by v14, and the
    disposition-type CHECK moves onto the activity row."""
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
        CREATE TABLE context_sections (
            section_name TEXT PRIMARY KEY,
            owner TEXT NOT NULL CHECK(owner IN ('claude_ai', 'cc', 'codex')),
            content TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            source_trust TEXT NOT NULL DEFAULT 'agent' CHECK(source_trust IN ('operator', 'agent', 'ingested'))
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
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'active', 'cleared')),
            canonical_key TEXT,
            source_trust TEXT NOT NULL DEFAULT 'agent' CHECK(source_trust IN ('operator', 'agent', 'ingested'))
        );
        INSERT INTO activity_log (source, timestamp, project_name, summary, tags)
            VALUES ('cc', '2026-06-13', 'fable-outputs', 'manual artifact', '["SHIPPED"]');
        INSERT INTO context_sections (section_name, owner, content, source_trust)
            VALUES ('career', 'claude_ai', 'legacy career', 'operator');
        CREATE VIRTUAL TABLE IF NOT EXISTS content_index USING fts5(
            source_type UNINDEXED,
            source_id UNINDEXED,
            text,
            tokenize = 'porter unicode61 remove_diacritics 2'
        );
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

        # The disposition table added at v8 is collapsed onto activity_log and
        # dropped by v14; the seeded SHIPPED row survives with NULL sync state.
        cursor = await migrated.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('shipped_sync_receipts', 'shipped_event_dispositions')"
        )
        assert await cursor.fetchall() == []

        cursor = await migrated.execute(
            "SELECT sync_disposition FROM activity_log WHERE id = 1"
        )
        seeded = await cursor.fetchone()
        assert seeded is not None
        assert seeded["sync_disposition"] is None

        # The disposition-type CHECK moved onto the activity row: a value outside
        # the allowed set is rejected by the column constraint.
        await migrated.execute(
            "UPDATE activity_log SET sync_disposition = 'unsynced_by_policy' WHERE id = 1"
        )
        await migrated.commit()
        with pytest.raises(aiosqlite.IntegrityError):
            await migrated.execute(
                "UPDATE activity_log SET sync_disposition = 'bogus' WHERE id = 1"
            )
    finally:
        await migrated.close()


async def test_migration_v13_to_v14_collapses_shipped_tables_losslessly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A v13 DB with existing receipts + dispositions migrates to v14 with every
    row's shipped-sync state copied onto activity_log columns, the two child
    tables dropped, and get_shipped_events output preserved field-for-field."""
    monkeypatch.setattr(
        config, "PROJECT_REGISTRY_PATH", tmp_path / "missing-registry.json"
    )
    monkeypatch.setattr(
        config, "META_SHIPPED_EVENTS_PATH", tmp_path / "missing-meta.json"
    )

    db = await aiosqlite.connect(str(tmp_path / "v13.db"))
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
        CREATE TABLE shipped_event_dispositions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_id INTEGER NOT NULL UNIQUE,
            disposition_type TEXT NOT NULL CHECK(disposition_type IN (
                'unsynced_by_policy', 'no_durable_target',
                'superseded_without_receipt', 'declined_mapping'
            )),
            policy_ref TEXT,
            reason TEXT NOT NULL,
            decided_by TEXT NOT NULL CHECK(decided_by IN ('cc', 'codex', 'claude_ai', 'notion_os', 'personal_ops')),
            decided_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            notes TEXT,
            FOREIGN KEY(activity_id) REFERENCES activity_log(id) ON DELETE CASCADE
        );
        CREATE VIRTUAL TABLE content_index USING fts5(
            source_type UNINDEXED, source_id UNINDEXED, text,
            tokenize = 'porter unicode61 remove_diacritics 2'
        );

        INSERT INTO activity_log (id, source, timestamp, project_name, summary, tags, created_at)
            VALUES (1, 'codex', '2026-07-03', 'synced-proj', 'shipped and synced',
                    '["SHIPPED","PROCESSED"]', '2026-07-03T00:00:00Z');
        INSERT INTO activity_log (id, source, timestamp, project_name, summary, tags, created_at)
            VALUES (2, 'cc', '2026-07-02', 'policy-proj', 'shipped but policy',
                    '["SHIPPED"]', '2026-07-02T00:00:00Z');
        INSERT INTO activity_log (id, source, timestamp, project_name, summary, tags, created_at)
            VALUES (3, 'cc', '2026-07-01', 'open-proj', 'shipped unresolved',
                    '["SHIPPED"]', '2026-07-01T00:00:00Z');

        INSERT INTO shipped_sync_receipts
            (activity_id, downstream_system, downstream_ref, synced_by, synced_at, notes)
            VALUES (1, 'notion', 'page-1', 'codex', '2026-07-03T09:00:00Z', 'synced note');
        INSERT INTO shipped_event_dispositions
            (activity_id, disposition_type, policy_ref, reason, decided_by, decided_at, notes)
            VALUES (2, 'unsynced_by_policy', '/p.md', 'experimental', 'cc',
                    '2026-07-02T09:00:00Z', 'policy note');

        PRAGMA user_version = 13;
    """)
    await db.commit()
    await db.close()

    # The expected shipped-sync sub-objects mirror the pre-migration v13 read
    # contract exactly — this is the byte-identical target.
    expected_synced_receipt = {
        "downstream_system": "notion",
        "downstream_ref": "page-1",
        "synced_by": "codex",
        "synced_at": "2026-07-03T09:00:00Z",
        "notes": "synced note",
    }
    expected_policy_disposition = {
        "disposition_type": "unsynced_by_policy",
        "policy_ref": "/p.md",
        "reason": "experimental",
        "decided_by": "cc",
        "decided_at": "2026-07-02T09:00:00Z",
        "notes": "policy note",
    }

    migrated = await open_db(tmp_path / "v13.db")
    try:
        cursor = await migrated.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == SCHEMA_VERSION

        # Old child tables dropped; state now lives on the activity rows.
        cursor = await migrated.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('shipped_sync_receipts', 'shipped_event_dispositions')"
        )
        assert await cursor.fetchall() == []

        # Receipt row copied verbatim into the synced-disposition columns.
        cursor = await migrated.execute(
            "SELECT sync_disposition, sync_downstream_system, sync_downstream_ref, "
            "sync_disposition_by, synced_at, sync_note FROM activity_log WHERE id = 1"
        )
        r1 = await cursor.fetchone()
        assert r1 is not None
        assert r1["sync_disposition"] == "synced"
        assert r1["sync_downstream_system"] == "notion"
        assert r1["sync_downstream_ref"] == "page-1"
        assert r1["sync_disposition_by"] == "codex"
        assert r1["synced_at"] == "2026-07-03T09:00:00Z"
        assert r1["sync_note"] == "synced note"

        # Disposition row copied verbatim into the policy-disposition columns.
        cursor = await migrated.execute(
            "SELECT sync_disposition, sync_policy_ref, sync_reason, sync_disposition_by, "
            "synced_at, sync_note FROM activity_log WHERE id = 2"
        )
        r2 = await cursor.fetchone()
        assert r2 is not None
        assert r2["sync_disposition"] == "unsynced_by_policy"
        assert r2["sync_policy_ref"] == "/p.md"
        assert r2["sync_reason"] == "experimental"
        assert r2["sync_disposition_by"] == "cc"
        assert r2["synced_at"] == "2026-07-02T09:00:00Z"
        assert r2["sync_note"] == "policy note"

        cursor = await migrated.execute(
            "SELECT sync_disposition FROM activity_log WHERE id = 3"
        )
        r3 = await cursor.fetchone()
        assert r3 is not None
        assert r3["sync_disposition"] is None

        # get_shipped_events reproduces the pre-migration read contract exactly.
        cap = CaptureMCP()
        activity_mod.register(cap)
        ctx = make_ctx(migrated)

        events = await cap.fns["get_shipped_events"](ctx=ctx)
        assert [e["id"] for e in events] == [1, 2, 3]  # newest-first by timestamp
        by_id = {e["id"]: e for e in events}
        assert by_id[1]["sync_receipt"] == expected_synced_receipt
        assert by_id[1]["policy_disposition"] is None
        assert by_id[1]["tags"] == ["SHIPPED", "PROCESSED"]
        assert by_id[2]["policy_disposition"] == expected_policy_disposition
        assert by_id[2]["sync_receipt"] is None
        assert by_id[3]["sync_receipt"] is None
        assert by_id[3]["policy_disposition"] is None

        unprocessed = await cap.fns["get_shipped_events"](
            unprocessed_only=True, ctx=ctx
        )
        assert [e["id"] for e in unprocessed] == [3]
    finally:
        await migrated.close()


async def test_migration_v9_to_v10_adds_context_versions_and_conflict_tables(
    tmp_path: Path,
) -> None:
    """A v9 DB gains integer context CAS plus export-state and conflict receipts."""
    db = await aiosqlite.connect(str(tmp_path / "v9.db"))
    db.row_factory = aiosqlite.Row
    await db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE context_sections (
            section_name TEXT PRIMARY KEY,
            owner TEXT NOT NULL CHECK(owner IN ('claude_ai', 'cc', 'codex')),
            content TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            source_trust TEXT NOT NULL DEFAULT 'agent' CHECK(source_trust IN ('operator', 'agent', 'ingested'))
        );
        INSERT INTO context_sections (section_name, owner, content, source_trust)
            VALUES ('career', 'claude_ai', 'legacy content', 'operator');
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
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'active', 'cleared')),
            canonical_key TEXT,
            source_trust TEXT NOT NULL DEFAULT 'agent' CHECK(source_trust IN ('operator', 'agent', 'ingested'))
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS content_index USING fts5(
            source_type UNINDEXED,
            source_id UNINDEXED,
            text,
            tokenize = 'porter unicode61 remove_diacritics 2'
        );
        PRAGMA user_version = 9;
    """)
    await db.commit()
    await db.close()

    migrated = await open_db(tmp_path / "v9.db")
    try:
        cursor = await migrated.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == SCHEMA_VERSION

        cursor = await migrated.execute(
            "SELECT content, source_trust, version FROM context_sections WHERE section_name = 'career'"
        )
        section = await cursor.fetchone()
        assert section is not None
        assert section["content"] == "legacy content"
        assert section["source_trust"] == "operator"
        assert section["version"] == 1

        cursor = await migrated.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table'
              AND name IN ('context_section_export_state', 'write_conflicts')
            ORDER BY name
            """
        )
        tables = [r["name"] for r in await cursor.fetchall()]
        assert tables == ["context_section_export_state", "write_conflicts"]
    finally:
        await migrated.close()


async def test_migration_v10_to_v11_reindexes_activity_tags(tmp_path: Path) -> None:
    """A v10 DB rebuilds content_index so activity tags become recall-able.

    The B3 change made fts_text_for_activity append tags, but it shipped without a
    version bump, so DBs already at v10 keep content_index rows built before tags
    were indexed: recall("SHIPPED") misses every historical row. The v11 migration
    runs reindex_all_activity_fts to rebuild the activity rows. This pins that fix.
    """
    db_path = tmp_path / "v10.db"

    # Fresh DB, seed a tagged activity row, then simulate the pre-v11 state: a
    # content_index entry built WITHOUT the tag, and user_version knocked to 10.
    db = await open_db(db_path)
    await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags) "
        "VALUES ('cc', '2026-06-01', 'bridge-db', 'recall precision landed', ?)",
        ('["SHIPPED"]',),
    )
    cursor = await db.execute("SELECT id FROM activity_log LIMIT 1")
    row = await cursor.fetchone()
    assert row is not None
    act_id = str(row["id"])
    await db.execute("DELETE FROM content_index WHERE source_type = 'activity'")
    await db.execute(
        "INSERT INTO content_index (source_type, source_id, text) VALUES ('activity', ?, ?)",
        (act_id, "bridge-db\nrecall precision landed"),
    )
    # open_db() bootstraps a fresh DB at the current schema, which already has
    # the columns added by later ALTER migrations: pending_handoffs.claimed_by
    # (v13) and activity_log's sync_* disposition columns (v14). Drop them back
    # off so the rewound version below matches a real pre-v13 DB and the ladder's
    # ALTER ADD COLUMN steps apply cleanly instead of hitting a duplicate column.
    await db.execute("ALTER TABLE pending_handoffs DROP COLUMN claimed_by")
    for _sync_col in (
        "sync_disposition",
        "sync_disposition_by",
        "synced_at",
        "sync_downstream_system",
        "sync_downstream_ref",
        "sync_policy_ref",
        "sync_reason",
        "sync_note",
    ):
        await db.execute(f"ALTER TABLE activity_log DROP COLUMN {_sync_col}")  # noqa: S608
    await db.execute("PRAGMA user_version = 10")
    await db.commit()
    await db.close()

    # Reopen: the v10 to v11 migration rebuilds content_index with tags included.
    migrated = await open_db(db_path)
    try:
        cursor = await migrated.execute("PRAGMA user_version")
        version_row = await cursor.fetchone()
        assert version_row is not None
        assert version_row[0] == SCHEMA_VERSION

        cursor = await migrated.execute(
            "SELECT source_id FROM content_index WHERE content_index MATCH 'SHIPPED'"
        )
        hits = [r["source_id"] for r in await cursor.fetchall()]
        assert act_id in hits, "v11 migration must reindex activity tags for recall"
    finally:
        await migrated.close()


async def test_migration_v11_to_v12_adds_session_classification(tmp_path: Path) -> None:
    """A v11 DB gains the heuristic session_classification sidecar at v12."""
    db_path = tmp_path / "v11.db"

    db = await open_db(db_path)
    await db.execute(
        """
        INSERT INTO session_costs (session_id, project_name, started_at, cost_usd)
        VALUES ('sess-v11', 'cost-tracker', '2026-07-01T00:00:00Z', 4.25)
        """
    )
    # open_db() bootstraps a fresh DB at the current schema, which already has
    # the columns added by later ALTER migrations: pending_handoffs.claimed_by
    # (v13) and activity_log's sync_* disposition columns (v14). Drop them back
    # off so the rewound version below matches a real pre-v13 DB and the ladder's
    # ALTER ADD COLUMN steps apply cleanly instead of hitting a duplicate column.
    await db.execute("ALTER TABLE pending_handoffs DROP COLUMN claimed_by")
    for _sync_col in (
        "sync_disposition",
        "sync_disposition_by",
        "synced_at",
        "sync_downstream_system",
        "sync_downstream_ref",
        "sync_policy_ref",
        "sync_reason",
        "sync_note",
    ):
        await db.execute(f"ALTER TABLE activity_log DROP COLUMN {_sync_col}")  # noqa: S608
    await db.execute("PRAGMA user_version = 11")
    await db.commit()
    await db.close()

    migrated = await open_db(db_path)
    try:
        cursor = await migrated.execute("PRAGMA user_version")
        version_row = await cursor.fetchone()
        assert version_row is not None
        assert version_row[0] == SCHEMA_VERSION

        cursor = await migrated.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_classification'"
        )
        table_row = await cursor.fetchone()
        assert table_row is not None

        cursor = await migrated.execute("PRAGMA table_info(session_classification)")
        columns = {row["name"] for row in await cursor.fetchall()}
        assert {
            "session_id",
            "role",
            "task_class",
            "routing_basis",
            "dominant_model",
            "confidence",
            "method",
            "classified_at",
        } <= columns

        await migrated.execute(
            """
            INSERT INTO session_classification (
                session_id, role, task_class, routing_basis, dominant_model, confidence, method
            )
            VALUES ('sess-v11', 'solo', 'implementation', 'over-powered', 'opus', 0.75, 'derived-v1')
            """
        )
        await migrated.commit()
        cursor = await migrated.execute(
            "SELECT routing_basis FROM session_classification WHERE session_id = 'sess-v11'"
        )
        class_row = await cursor.fetchone()
        assert class_row is not None
        assert class_row["routing_basis"] == "over-powered"
    finally:
        await migrated.close()


async def test_migration_v11_to_v12_is_idempotent(tmp_path: Path) -> None:
    """Re-opening a migrated v12 DB does not raise; version stays current."""
    db_path = tmp_path / "v11_idem.db"

    db = await open_db(db_path)
    # open_db() bootstraps a fresh DB at the current schema, which already has
    # the columns added by later ALTER migrations: pending_handoffs.claimed_by
    # (v13) and activity_log's sync_* disposition columns (v14). Drop them back
    # off so the rewound version below matches a real pre-v13 DB and the ladder's
    # ALTER ADD COLUMN steps apply cleanly instead of hitting a duplicate column.
    await db.execute("ALTER TABLE pending_handoffs DROP COLUMN claimed_by")
    for _sync_col in (
        "sync_disposition",
        "sync_disposition_by",
        "synced_at",
        "sync_downstream_system",
        "sync_downstream_ref",
        "sync_policy_ref",
        "sync_reason",
        "sync_note",
    ):
        await db.execute(f"ALTER TABLE activity_log DROP COLUMN {_sync_col}")  # noqa: S608
    await db.execute("PRAGMA user_version = 11")
    await db.commit()
    await db.close()

    first = await open_db(db_path)
    await first.close()

    second = await open_db(db_path)
    try:
        cursor = await second.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == SCHEMA_VERSION

        cursor = await second.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_scl_routing'"
        )
        index_row = await cursor.fetchone()
        assert index_row is not None
    finally:
        await second.close()
