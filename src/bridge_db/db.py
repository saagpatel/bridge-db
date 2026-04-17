"""Database schema, migrations, and connection setup."""

import logging
from pathlib import Path
from typing import Any, cast

import aiosqlite

logger = logging.getLogger("bridge_db.db")

# Schema version — increment when adding migrations
SCHEMA_VERSION = 3

# Full DDL for current schema (initial create on a fresh DB)
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

CREATE VIRTUAL TABLE IF NOT EXISTS content_index USING fts5(
    source_type UNINDEXED,
    source_id UNINDEXED,
    text,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
"""

# Migration from v2 → v3: add content_index FTS5 virtual table.
# Rows are populated by repopulate_content_index() after the DDL runs.
_MIGRATION_V2_TO_V3 = """
CREATE VIRTUAL TABLE IF NOT EXISTS content_index USING fts5(
    source_type UNINDEXED,
    source_id UNINDEXED,
    text,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
"""

# Migration from v1 → v2: expand CHECK constraints on activity_log and cost_records.
# SQLite cannot ALTER COLUMN check constraints; must rename+recreate.
# Also ensures all other v2 tables exist (IF NOT EXISTS is a no-op on real v1
# DBs that already had them; defensive for reconstructed-from-minimal v1 DBs).
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

CREATE TABLE IF NOT EXISTS context_sections (
    section_name TEXT PRIMARY KEY,
    owner TEXT NOT NULL CHECK(owner IN ('claude_ai', 'cc', 'codex')),
    content TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

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
    """Create tables if not present; run any pending migrations in sequence."""
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    current_version: int = row[0] if row else 0  # type: ignore[index]

    if current_version > SCHEMA_VERSION:
        msg = (
            "Database schema version is newer than this bridge-db build supports "
            f"(db={current_version}, supported={SCHEMA_VERSION})."
        )
        logger.error(msg)
        raise RuntimeError(msg)

    if current_version == 0:
        logger.info("Initializing fresh schema v%d", SCHEMA_VERSION)
        await db.executescript(_SCHEMA_DDL)
        await db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        await db.commit()
        logger.info("Schema v%d initialized", SCHEMA_VERSION)
        return

    # Step-wise migration: each step advances user_version by 1 and commits,
    # so a failure mid-sequence leaves the DB at the last fully-migrated version.
    while current_version < SCHEMA_VERSION:
        if current_version == 1:
            logger.info("Migrating schema v1 → v2")
            await db.executescript(_MIGRATION_V1_TO_V2)
            current_version = 2
            await db.execute(f"PRAGMA user_version = {current_version}")
            await db.commit()
            logger.info("Schema migrated to v2")
        elif current_version == 2:
            logger.info("Migrating schema v2 → v3")
            await db.executescript(_MIGRATION_V2_TO_V3)
            await repopulate_content_index(db)
            current_version = 3
            await db.execute(f"PRAGMA user_version = {current_version}")
            await db.commit()
            logger.info("Schema migrated to v3")
        else:
            raise RuntimeError(f"No migration path defined from v{current_version}")

    logger.debug("Schema at v%d", current_version)


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


# ── FTS5 content index helpers ───────────────────────────────────────────────
# Callers are responsible for committing; these helpers only stage writes so
# they can be composed with other writes in the same tool transaction.


def fts_text_for_section(section_name: str, content: str) -> str:
    """Indexable text for a context_sections row."""
    return f"{section_name}\n{content}"


def fts_text_for_activity(project_name: str, summary: str, branch: str | None) -> str:
    """Indexable text for an activity_log row. Tags excluded (structural, not prose)."""
    parts = [project_name, summary]
    if branch:
        parts.append(branch)
    return "\n".join(parts)


def fts_text_for_snapshot(data: str) -> str:
    """Indexable text for a system_snapshots row. `data` is the JSON-encoded payload."""
    return data


def fts_text_for_handoff(
    project_name: str,
    project_path: str | None,
    roadmap_file: str | None,
    phase: str | None,
) -> str:
    """Indexable text for a pending_handoffs row."""
    parts = [project_name]
    for p in (project_path, roadmap_file, phase):
        if p:
            parts.append(p)
    return "\n".join(parts)


async def upsert_fts_entry(
    db: aiosqlite.Connection, source_type: str, source_id: str, text: str
) -> None:
    """Delete + insert the content_index row for a given source key."""
    await db.execute(
        "DELETE FROM content_index WHERE source_type = ? AND source_id = ?",
        (source_type, source_id),
    )
    await db.execute(
        "INSERT INTO content_index (source_type, source_id, text) VALUES (?, ?, ?)",
        (source_type, source_id, text),
    )


async def delete_fts_entry(db: aiosqlite.Connection, source_type: str, source_id: str) -> None:
    """Delete the content_index row for a given source key."""
    await db.execute(
        "DELETE FROM content_index WHERE source_type = ? AND source_id = ?",
        (source_type, source_id),
    )


async def gc_fts_orphans(db: aiosqlite.Connection, source_type: str) -> int:
    """Drop content_index rows whose source row no longer exists.

    Used after auto-prune deletes in activity_log and system_snapshots.
    Returns the number of orphan rows removed.
    """
    source_pk = {
        "section": ("context_sections", "section_name"),
        "activity": ("activity_log", "id"),
        "snapshot": ("system_snapshots", "id"),
        "handoff": ("pending_handoffs", "id"),
    }
    if source_type not in source_pk:
        raise ValueError(f"Unknown source_type for GC: {source_type}")
    table, pk = source_pk[source_type]
    cursor = await db.execute(
        f"""
        DELETE FROM content_index
        WHERE source_type = ?
          AND source_id NOT IN (SELECT CAST({pk} AS TEXT) FROM {table})
        """,  # noqa: S608 — table/pk come from a closed literal map
        (source_type,),
    )
    return cursor.rowcount or 0


async def repopulate_content_index(db: aiosqlite.Connection) -> dict[str, int]:
    """Rebuild content_index from all source tables. Idempotent — clears first."""
    await db.execute("DELETE FROM content_index")

    counts = {"section": 0, "activity": 0, "snapshot": 0, "handoff": 0}

    cursor = await db.execute("SELECT section_name, content FROM context_sections")
    for row in await cursor.fetchall():
        await db.execute(
            "INSERT INTO content_index (source_type, source_id, text) VALUES (?, ?, ?)",
            (
                "section",
                row["section_name"],
                fts_text_for_section(row["section_name"], row["content"]),
            ),
        )
        counts["section"] += 1

    cursor = await db.execute("SELECT id, project_name, summary, branch FROM activity_log")
    for row in await cursor.fetchall():
        await db.execute(
            "INSERT INTO content_index (source_type, source_id, text) VALUES (?, ?, ?)",
            (
                "activity",
                str(row["id"]),
                fts_text_for_activity(row["project_name"], row["summary"], row["branch"]),
            ),
        )
        counts["activity"] += 1

    cursor = await db.execute("SELECT id, data FROM system_snapshots")
    for row in await cursor.fetchall():
        await db.execute(
            "INSERT INTO content_index (source_type, source_id, text) VALUES (?, ?, ?)",
            ("snapshot", str(row["id"]), fts_text_for_snapshot(row["data"])),
        )
        counts["snapshot"] += 1

    cursor = await db.execute(
        "SELECT id, project_name, project_path, roadmap_file, phase FROM pending_handoffs"
    )
    for row in await cursor.fetchall():
        await db.execute(
            "INSERT INTO content_index (source_type, source_id, text) VALUES (?, ?, ?)",
            (
                "handoff",
                str(row["id"]),
                fts_text_for_handoff(
                    row["project_name"],
                    row["project_path"],
                    row["roadmap_file"],
                    row["phase"],
                ),
            ),
        )
        counts["handoff"] += 1

    await db.commit()
    logger.info("content_index repopulated: %s", counts)
    return counts
