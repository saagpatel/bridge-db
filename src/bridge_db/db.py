"""Database schema, migrations, and connection setup."""

import logging
from pathlib import Path
from typing import Any, cast

import aiosqlite

logger = logging.getLogger("bridge_db.db")

# Schema version — increment when adding migrations
SCHEMA_VERSION = 2

# Full DDL for v2 schema (initial create on a fresh DB)
_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS context_sections (
    section_name TEXT PRIMARY KEY,
    owner TEXT NOT NULL CHECK(owner IN ('claude_ai', 'cc', 'codex')),
    content TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL CHECK(source IN ('cc', 'codex', 'claude_ai', 'notion_os', 'personal_ops')),
    timestamp TEXT NOT NULL,
    project_name TEXT NOT NULL,
    summary TEXT NOT NULL,
    branch TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_activity_source ON activity_log(source);
CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON activity_log(timestamp DESC);

CREATE TABLE IF NOT EXISTS system_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    system TEXT NOT NULL CHECK(system IN ('cc', 'codex')),
    snapshot_date TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_snapshot_system ON system_snapshots(system, created_at DESC);

CREATE TABLE IF NOT EXISTS pending_handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL,
    project_path TEXT,
    roadmap_file TEXT,
    phase TEXT,
    dispatched_from TEXT NOT NULL DEFAULT 'claude_ai',
    dispatched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    picked_up_at TEXT,
    cleared_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'active', 'cleared'))
);

CREATE INDEX IF NOT EXISTS idx_handoff_status ON pending_handoffs(status);

CREATE TABLE IF NOT EXISTS cost_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    system TEXT NOT NULL CHECK(system IN ('cc', 'codex', 'notion_os', 'personal_ops')),
    month TEXT NOT NULL,
    amount REAL NOT NULL,
    notes TEXT,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(system, month)
);
"""

# Migration from v1 → v2: expand CHECK constraints on activity_log and cost_records.
# SQLite cannot ALTER COLUMN check constraints; must rename+recreate.
_MIGRATION_V1_TO_V2 = """
ALTER TABLE activity_log RENAME TO activity_log_v1;

CREATE TABLE activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL CHECK(source IN ('cc', 'codex', 'claude_ai', 'notion_os', 'personal_ops')),
    timestamp TEXT NOT NULL,
    project_name TEXT NOT NULL,
    summary TEXT NOT NULL,
    branch TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

INSERT INTO activity_log SELECT * FROM activity_log_v1;
DROP TABLE activity_log_v1;

CREATE INDEX IF NOT EXISTS idx_activity_source ON activity_log(source);
CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON activity_log(timestamp DESC);

ALTER TABLE cost_records RENAME TO cost_records_v1;

CREATE TABLE cost_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    system TEXT NOT NULL CHECK(system IN ('cc', 'codex', 'notion_os', 'personal_ops')),
    month TEXT NOT NULL,
    amount REAL NOT NULL,
    notes TEXT,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(system, month)
);

INSERT INTO cost_records SELECT * FROM cost_records_v1;
DROP TABLE cost_records_v1;
"""


async def apply_pragmas(db: aiosqlite.Connection) -> None:
    """Apply all required PRAGMAs. Safe to call on every connection open."""
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA cache_size=-64000")
    await db.commit()


async def ensure_schema(db: aiosqlite.Connection) -> None:
    """Create tables if not present; run any pending migrations."""
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    current_version: int = row[0] if row else 0  # type: ignore[index]

    if current_version == 0:
        # Fresh DB: apply full v2 DDL directly
        logger.info("Initializing fresh schema v%d", SCHEMA_VERSION)
        await db.executescript(_SCHEMA_DDL)
        await db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        await db.commit()
        logger.info("Schema v%d initialized", SCHEMA_VERSION)
    elif current_version == 1:
        # Existing v1 DB: run incremental migration
        logger.info("Migrating schema v1 → v2")
        await db.executescript(_MIGRATION_V1_TO_V2)
        await db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        await db.commit()
        logger.info("Schema migrated to v%d", SCHEMA_VERSION)
    else:
        logger.debug("Schema already at v%d", current_version)


async def open_db(db_path: Path) -> aiosqlite.Connection:
    """Open a connection, apply pragmas and schema. Caller must close."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await apply_pragmas(db)
    await ensure_schema(db)
    return db


def get_db(ctx: Any) -> aiosqlite.Connection:
    """Extract the typed DB connection from a FastMCP tool context.

    The MCP SDK types lifespan_context as Unknown; this cast surfaces the real type.
    """
    return cast(aiosqlite.Connection, ctx.request_context.lifespan_context.db)
