"""Tests for DB schema creation, PRAGMAs, and migration idempotency."""

from pathlib import Path

import aiosqlite
import pytest

from bridge_db.db import SCHEMA_VERSION, ensure_schema, open_db


async def test_schema_creates_all_tables(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    tables = {row[0] for row in await cursor.fetchall()}
    assert tables == {
        "activity_log",
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
