"""Tests for the health MCP tool."""

import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx

from bridge_db import config
from bridge_db.db import SCHEMA_VERSION
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
    assert result["row_counts"]["activity_log"] == 1
    assert result["row_counts"]["context_sections"] == 0
    assert result["row_counts"]["pending_handoffs"] == 0
    assert result["row_counts"]["system_snapshots"] == 0
    assert result["row_counts"]["cost_records"] == 0


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
    await db.execute(
        "INSERT INTO system_snapshots (system, snapshot_date, data) VALUES (?, ?, ?)",
        ("cc", "2026-04-17", '{"active_projects":"- bridge-db"}'),
    )
    await db.execute(
        "INSERT INTO activity_log (source, timestamp, project_name, summary, tags) "
        "VALUES ('cc', '2026-04-17', 'bridge-db', 'checked operator status', ?)",
        (json.dumps(["SHIPPED"]),),
    )
    await db.commit()

    ctx = make_ctx(db)
    result = await fns["status"](ctx=ctx)

    assert result["ok"] is True
    assert result["overall"] == "healthy"
    assert result["row_counts"]["context_sections"] == 1
    assert result["signals"]["pending_handoffs"] == 0
    assert result["signals"]["unprocessed_shipped"] == 1
    assert result["latest_snapshots"]["cc"] == "2026-04-17"
    assert result["latest_activity"]["cc"] == "2026-04-17 (bridge-db)"