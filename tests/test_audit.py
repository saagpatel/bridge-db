"""Tests for JSONL audit log."""

import json
from pathlib import Path

import pytest

from bridge_db import audit, config


@pytest.fixture(autouse=True)
def patch_audit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect audit log to tmp_path so tests don't pollute the real log."""
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", tmp_path / "audit.jsonl")


async def test_log_audit_creates_file(tmp_path: Path) -> None:
    audit.log_audit("log_activity", "cc", "TestProject", ok=True)
    assert config.AUDIT_LOG_PATH.exists()


async def test_log_audit_writes_valid_json(tmp_path: Path) -> None:
    audit.log_audit("log_activity", "cc", "TestProject", ok=True, detail="extra")
    lines = config.AUDIT_LOG_PATH.read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["tool"] == "log_activity"
    assert event["caller"] == "cc"
    assert event["project"] == "TestProject"
    assert event["ok"] is True
    assert event["detail"] == "extra"
    assert "ts" in event


async def test_log_audit_appends_multiple_events(tmp_path: Path) -> None:
    audit.log_audit("log_activity", "cc", "P1", ok=True)
    audit.log_audit("record_cost", "codex", None, ok=False, detail="err")
    lines = config.AUDIT_LOG_PATH.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["tool"] == "log_activity"
    assert json.loads(lines[1])["tool"] == "record_cost"


async def test_log_audit_never_raises_on_bad_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unwriteable path must not propagate — audit failure is silent."""
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", Path("/no/such/dir/audit.jsonl"))
    # Should not raise
    audit.log_audit("health", None, None, ok=True)


async def test_log_audit_ts_format(tmp_path: Path) -> None:
    audit.log_audit("health", None, None, ok=True)
    event = json.loads(config.AUDIT_LOG_PATH.read_text().splitlines()[0])
    # Must end with Z (UTC marker)
    assert event["ts"].endswith("Z")


def test_iter_jsonl_missing_path_returns_empty(tmp_path: Path) -> None:
    """Missing file must yield nothing, not raise."""
    records = list(audit.iter_jsonl(tmp_path / "does_not_exist.jsonl"))
    assert records == []


def test_iter_jsonl_empty_file_returns_empty(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert list(audit.iter_jsonl(empty)) == []


def test_iter_jsonl_skips_blank_and_malformed_lines(tmp_path: Path) -> None:
    """Blank lines and bad JSON are skipped; surrounding valid records still yielded."""
    path = tmp_path / "mixed.jsonl"
    path.write_text(
        '{"tool":"a","ok":true}\n'
        "\n"
        "   \n"
        "this is not json\n"
        '{"tool":"b","ok":false}\n'
        "{not a dict but valid json: 42}\n"
        "[1,2,3]\n"  # valid JSON but not a dict — should be skipped
        '{"tool":"c","ok":true}\n',
        encoding="utf-8",
    )
    records = list(audit.iter_jsonl(path))
    tools = [r["tool"] for r in records]
    assert tools == ["a", "b", "c"]
