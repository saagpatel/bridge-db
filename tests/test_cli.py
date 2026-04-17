"""Tests for the bridge-db CLI helpers."""

from pathlib import Path

import pytest

import bridge_db.config as cfg
from bridge_db.__main__ import run_status
from bridge_db.db import open_db


@pytest.mark.asyncio
async def test_run_status_reports_healthy_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "bridge.db"
    bridge_path = tmp_path / "claude_ai_context.md"
    bridge_path.write_text("# bridge\n", encoding="utf-8")

    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "BRIDGE_FILE_PATH", bridge_path)

    db = await open_db(db_path)
    try:
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
            "VALUES (?, ?, ?, ?, ?)",
            ("cc", "2026-04-17", "bridge-db", "checked operator status", '["SHIPPED"]'),
        )
        await db.commit()
    finally:
        await db.close()

    ok = await run_status()
    captured = capsys.readouterr().out

    assert ok is True
    assert "Overall: healthy" in captured
    assert "contexts=1" in captured
    assert "pending_handoffs=0" in captured
    assert "unprocessed_shipped=1" in captured
    assert "cc=2026-04-17" in captured
    assert '"cc": "2026-04-17 (bridge-db)"' in captured


@pytest.mark.asyncio
async def test_run_status_reports_degraded_when_bridge_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "bridge.db"
    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "BRIDGE_FILE_PATH", tmp_path / "missing.md")

    db = await open_db(db_path)
    await db.close()

    ok = await run_status()
    captured = capsys.readouterr().out

    assert ok is False
    assert "Overall: degraded" in captured
    assert "exists=False, age=missing" in captured