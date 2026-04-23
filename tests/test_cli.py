"""Tests for the bridge-db CLI helpers."""

import os
import subprocess
import sys
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


@pytest.mark.parametrize(
    ("flag", "expected_text"),
    [
        ("--status", "bridge-db status"),
        ("--doctor", "DB opens (WAL + schema)"),
    ],
)
def test_cli_entrypoints_smoke(flag: str, expected_text: str, tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "bridge.db"
    bridge_path = tmp_path / "claude_ai_context.md"
    audit_log_path = tmp_path / "audit.log"
    bridge_path.write_text("# bridge\n", encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")
    env["BRIDGE_DB_PATH"] = str(db_path)
    env["BRIDGE_FILE_PATH"] = str(bridge_path)
    env["BRIDGE_DB_AUDIT_LOG_PATH"] = str(audit_log_path)

    bootstrap = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import asyncio
import os
from pathlib import Path
from bridge_db.db import open_db


async def main() -> None:
    db = await open_db(Path(os.environ["BRIDGE_DB_PATH"]))
    await db.close()


asyncio.run(main())
""",
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bootstrap.returncode == 0, bootstrap.stderr

    legacy_env = env.copy()
    legacy_env.pop("BRIDGE_DB_PATH")
    legacy_env.pop("BRIDGE_FILE_PATH")
    legacy_env.pop("BRIDGE_DB_AUDIT_LOG_PATH")
    legacy_env["HOME"] = str(tmp_path / "legacy-home")
    legacy_env["DB_PATH"] = str(db_path)
    legacy_env["AUDIT_LOG_PATH"] = str(audit_log_path)

    legacy_result = subprocess.run(
        [sys.executable, "-m", "bridge_db", flag],
        cwd=repo_root,
        env=legacy_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert legacy_result.returncode != 0

    result = subprocess.run(
        [sys.executable, "-m", "bridge_db", flag],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert expected_text in result.stdout
    if flag == "--doctor":
        assert str(db_path) in result.stdout
        assert str(audit_log_path) in result.stdout
    if flag == "--status":
        assert "contexts=0" in result.stdout
