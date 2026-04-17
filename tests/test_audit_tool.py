"""Tests for the audit_tail MCP tool."""

from pathlib import Path
from typing import Any

import pytest
from conftest import CaptureMCP

from bridge_db import audit, config
from bridge_db.tools import audit as audit_tool


@pytest.fixture
def fns() -> dict[str, Any]:
    cap = CaptureMCP()
    audit_tool.register(cap)
    return cap.fns


@pytest.fixture(autouse=True)
def patch_audit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", tmp_path / "audit.jsonl")


async def test_audit_tail_missing_log_returns_empty(fns: dict[str, Any]) -> None:
    assert await fns["audit_tail"]() == []


async def test_audit_tail_returns_newest_first(fns: dict[str, Any]) -> None:
    audit.log_audit("log_activity", "cc", "P1", ok=True)
    audit.log_audit("record_cost", "codex", "P2", ok=True)
    audit.log_audit("update_section", "claude_ai", "P3", ok=True)

    results = await fns["audit_tail"](limit=10)
    assert [r["tool"] for r in results] == ["update_section", "record_cost", "log_activity"]


async def test_audit_tail_limit_respected(fns: dict[str, Any]) -> None:
    for i in range(5):
        audit.log_audit("log_activity", "cc", f"P{i}", ok=True)
    results = await fns["audit_tail"](limit=2)
    assert len(results) == 2


async def test_audit_tail_filter_by_caller(fns: dict[str, Any]) -> None:
    audit.log_audit("log_activity", "cc", "A", ok=True)
    audit.log_audit("log_activity", "codex", "B", ok=True)
    audit.log_audit("log_activity", "cc", "C", ok=True)

    results = await fns["audit_tail"](caller="cc")
    assert {r["project"] for r in results} == {"A", "C"}


async def test_audit_tail_filter_by_tool(fns: dict[str, Any]) -> None:
    audit.log_audit("log_activity", "cc", "A", ok=True)
    audit.log_audit("record_cost", "cc", "B", ok=True)

    results = await fns["audit_tail"](tool="record_cost")
    assert len(results) == 1
    assert results[0]["tool"] == "record_cost"


async def test_audit_tail_filter_by_ok(fns: dict[str, Any]) -> None:
    audit.log_audit("log_activity", "cc", "A", ok=True)
    audit.log_audit("log_activity", "cc", "B", ok=False)
    audit.log_audit("log_activity", "cc", "C", ok=True)

    failures = await fns["audit_tail"](ok=False)
    assert [r["project"] for r in failures] == ["B"]
    successes = await fns["audit_tail"](ok=True)
    assert {r["project"] for r in successes} == {"A", "C"}


async def test_audit_tail_filter_by_since_date(fns: dict[str, Any]) -> None:
    """`since` compares as string; YYYY-MM-DD sorts before any ISO timestamp of the same day or later."""
    audit.log_audit("log_activity", "cc", "today", ok=True)

    # Empty string is "before everything" — all results
    all_results = await fns["audit_tail"](since="1970-01-01")
    assert len(all_results) == 1
    # Future date drops everything
    future = await fns["audit_tail"](since="2099-01-01")
    assert future == []


async def test_audit_tail_combined_filters(fns: dict[str, Any]) -> None:
    audit.log_audit("log_activity", "cc", "A", ok=True)
    audit.log_audit("log_activity", "cc", "B", ok=False)
    audit.log_audit("record_cost", "cc", "C", ok=False)

    results = await fns["audit_tail"](caller="cc", tool="log_activity", ok=False)
    assert len(results) == 1
    assert results[0]["project"] == "B"


async def test_audit_tail_skips_malformed_lines(fns: dict[str, Any], tmp_path: Path) -> None:
    """A bad line in the middle must not hide surrounding valid events."""
    audit.log_audit("log_activity", "cc", "before", ok=True)
    # Inject garbage directly into the log file
    with open(config.AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write("not valid json\n")
    audit.log_audit("log_activity", "cc", "after", ok=True)

    results = await fns["audit_tail"]()
    projects = {r["project"] for r in results}
    assert projects == {"before", "after"}
