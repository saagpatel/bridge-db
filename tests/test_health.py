"""Tests for the health MCP tool."""

import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx

from bridge_db import config
from bridge_db.db import (
    SCHEMA_VERSION,
    fts_text_for_activity,
    fts_text_for_section,
    fts_text_for_snapshot,
    upsert_fts_entry,
)
from bridge_db.tools import health as mod


@pytest.fixture
def fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    mod.register(cap)
    return cap.fns


@pytest.fixture(autouse=True)
def patch_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point DB_PATH at the test DB so db_exists reflects reality."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")


async def test_health_returns_ok_on_healthy_db(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    # Create the DB file so db_exists=True
    (tmp_path / "test.db").touch()
    ctx = make_ctx(db)
    result = await fns["health"](ctx=ctx)
    assert result["ok"] is True
    assert result["db_exists"] is True
    assert result["schema_version"] == SCHEMA_VERSION


async def test_health_row_counts_reflect_data(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / "test.db").touch()
    await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary) "
        "VALUES ('cc', '2026-04-14', 'P', 'S')"
    )
    await db.commit()
    ctx = make_ctx(db)
    result = await fns["health"](ctx=ctx)
    assert result["ok"] is False
    assert result["row_counts"]["activity_log"] == 1
    assert result["row_counts"]["context_sections"] == 0
    assert result["row_counts"]["pending_handoffs"] == 0
    assert result["row_counts"]["system_snapshots"] == 0
    assert result["row_counts"]["cost_records"] == 0
    assert result["fts_index"]["ok"] is False
    assert result["fts_index"]["missing"] == 1
    assert result["fts_index"]["orphaned"] == 0


async def test_health_unprocessed_shipped_count(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / "test.db").touch()
    # One SHIPPED + one SHIPPED+PROCESSED
    await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags) "
        "VALUES ('cc', '2026-04-14', 'A', 'S', ?)",
        (json.dumps(["SHIPPED"]),),
    )
    await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags) "
        "VALUES ('cc', '2026-04-14', 'B', 'S', ?)",
        (json.dumps(["SHIPPED", "PROCESSED"]),),
    )
    await db.commit()
    ctx = make_ctx(db)
    result = await fns["health"](ctx=ctx)
    assert result["unprocessed_shipped_count"] == 1


async def test_health_counts_processed_shipped_without_receipts(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / "test.db").touch()
    # One legacy-processed event without a receipt and one receipt-backed event.
    cursor = await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags) "
        "VALUES ('cc', '2026-04-14', 'A', 'S', ?) RETURNING id",
        (json.dumps(["SHIPPED", "PROCESSED"]),),
    )
    receiptless_row = await cursor.fetchone()
    assert receiptless_row is not None
    receiptless_id = int(receiptless_row[0])
    cursor = await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags) "
        "VALUES ('cc', '2026-04-14', 'B', 'S', ?) RETURNING id",
        (json.dumps(["SHIPPED", "PROCESSED"]),),
    )
    receipted_row = await cursor.fetchone()
    assert receipted_row is not None
    receipted_id = int(receipted_row[0])
    await db.execute(
        "INSERT INTO shipped_sync_receipts "
        "(activity_id, downstream_system, downstream_ref, synced_by) "
        "VALUES (?, 'notion', 'https://notion.so/example', 'codex')",
        (receipted_id,),
    )
    await db.commit()

    result = await fns["health"](ctx=make_ctx(db))

    assert receiptless_id != receipted_id
    assert result["processed_shipped_without_receipt_count"] == 1


async def test_health_bridge_file_info(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "test.db").touch()
    bridge = tmp_path / "bridge.md"
    bridge.write_text("# test")
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge)
    ctx = make_ctx(db)
    result = await fns["health"](ctx=ctx)
    assert result["bridge_file_exists"] is True
    assert isinstance(result["bridge_file_age_seconds"], float)
    assert result["bridge_file_age_seconds"] >= 0


async def test_health_bridge_file_missing(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "test.db").touch()
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", tmp_path / "nonexistent.md")
    ctx = make_ctx(db)
    result = await fns["health"](ctx=ctx)
    assert result["ok"] is False
    assert result["bridge_file_exists"] is False
    assert result["bridge_file_age_seconds"] is None


async def test_status_returns_compact_operator_summary(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "test.db").touch()
    bridge = tmp_path / "bridge.md"
    bridge.write_text("# test", encoding="utf-8")
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge)

    await db.execute(
        "INSERT INTO context_sections (section_name, owner, content) VALUES (?, ?, ?)",
        ("career", "claude_ai", "Career notes"),
    )
    await upsert_fts_entry(
        db,
        "section",
        "career",
        fts_text_for_section("career", "Career notes"),
    )
    await db.execute(
        "INSERT INTO system_snapshots (system, snapshot_date, data) VALUES (?, ?, ?)",
        ("cc", "2026-04-17", '{"active_projects":"- bridge-db"}'),
    )
    await upsert_fts_entry(
        db,
        "snapshot",
        "1",
        fts_text_for_snapshot('{"active_projects":"- bridge-db"}'),
    )
    cursor = await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags) "
        "VALUES ('cc', '2026-04-17', 'bridge-db', 'checked operator status', ?) RETURNING id",
        (json.dumps(["SHIPPED"]),),
    )
    activity_row = await cursor.fetchone()
    assert activity_row is not None
    await upsert_fts_entry(
        db,
        "activity",
        str(activity_row[0]),
        fts_text_for_activity("bridge-db", "checked operator status", None),
    )
    await db.commit()

    ctx = make_ctx(db)
    result = await fns["status"](ctx=ctx)

    assert result["ok"] is True
    assert result["overall"] == "healthy"
    assert result["row_counts"]["context_sections"] == 1
    assert result["signals"]["pending_handoffs"] == 0
    assert result["signals"]["unprocessed_shipped"] == 1
    assert result["signals"]["processed_shipped_without_receipt"] == 0
    assert result["signals"]["fts_missing"] == 0
    assert result["signals"]["fts_orphaned"] == 0
    assert result["fts_index"]["ok"] is True
    assert result["latest_snapshots"]["cc"] == "2026-04-17"
    assert result["latest_activity"]["cc"] == "2026-04-17 (bridge-db)"


async def test_health_wal_absent_when_no_wal_file(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    """Missing WAL sibling file → size 0, warning False."""
    (tmp_path / "test.db").touch()
    # Ensure no sibling wal file
    wal = tmp_path / "test.db-wal"
    if wal.exists():
        wal.unlink()
    ctx = make_ctx(db)
    result = await fns["health"](ctx=ctx)
    assert result["wal_size_bytes"] == 0
    assert result["wal_warning"] is False


async def test_health_wal_size_reflects_file_size(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    """`wal_size_bytes` mirrors the real size of the sibling WAL file."""
    (tmp_path / "test.db").touch()
    wal = tmp_path / "test.db-wal"
    wal.write_bytes(b"x" * 1024)
    ctx = make_ctx(db)
    result = await fns["health"](ctx=ctx)
    assert result["wal_size_bytes"] == 1024
    assert result["wal_warning"] is False


async def test_health_wal_warning_at_threshold(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wal_warning flips True strictly above the configured threshold."""
    (tmp_path / "test.db").touch()
    monkeypatch.setattr(config, "WAL_SIZE_WARN_BYTES", 100)
    wal = tmp_path / "test.db-wal"

    wal.write_bytes(b"x" * 100)
    result = await fns["health"](ctx=make_ctx(db))
    # At threshold, not above → no warning
    assert result["wal_warning"] is False

    wal.write_bytes(b"x" * 101)
    result = await fns["health"](ctx=make_ctx(db))
    assert result["wal_warning"] is True


async def test_health_ok_unaffected_by_wal_warning(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wal_warning is a soft signal — `ok` stays True on an otherwise-healthy bridge."""
    (tmp_path / "test.db").touch()
    bridge = tmp_path / "bridge.md"
    bridge.write_text("# test")
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge)
    monkeypatch.setattr(config, "WAL_SIZE_WARN_BYTES", 100)
    (tmp_path / "test.db-wal").write_bytes(b"x" * 1024)

    result = await fns["health"](ctx=make_ctx(db))
    assert result["wal_warning"] is True
    assert result["ok"] is True
