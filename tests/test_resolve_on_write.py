"""Tests for canonical-key resolution wired into the log_activity write path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx

from bridge_db import config
from bridge_db.tools import activity as mod


@pytest.fixture
def fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    mod.register(cap)
    return cap.fns


def _registry(tmp_path: Path) -> Path:
    reg = tmp_path / "project-registry.json"
    reg.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "canonical_key": "MCPAudit",
                        "display_name": "MCPAudit",
                        "repo_full_name": "saagpatel/MCPAudit",
                        "aliases": ["notion:MCP Audit"],
                    }
                ],
                "resolution_overrides": {},
            }
        )
    )
    return reg


async def test_log_activity_sets_canonical_key_from_registry(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "PROJECT_REGISTRY_PATH", _registry(tmp_path))
    result = await fns["log_activity"](
        caller="cc", project_name="MCP Audit", summary="did work", ctx=make_ctx(db, principal="cc")
    )
    assert result["canonical_key"] == "saagpatel/MCPAudit"
    rows = await fns["get_recent_activity"](ctx=make_ctx(db))
    assert rows[0]["project_name"] == "MCP Audit"
    assert rows[0]["canonical_key"] == "saagpatel/MCPAudit"


async def test_log_activity_pass_through_when_registry_absent(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "PROJECT_REGISTRY_PATH", tmp_path / "missing.json")
    result = await fns["log_activity"](
        caller="cc", project_name="SomeProject", summary="did work", ctx=make_ctx(db, principal="cc")
    )
    assert result["canonical_key"] is None
    rows = await fns["get_recent_activity"](ctx=make_ctx(db))
    assert rows[0]["canonical_key"] is None


async def test_log_activity_unmatched_with_registry_present_stays_none(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "PROJECT_REGISTRY_PATH", _registry(tmp_path))
    result = await fns["log_activity"](
        caller="cc", project_name="weekly-review", summary="ritual", ctx=make_ctx(db, principal="cc")
    )
    # registry present + no match -> canonical_key is None (and flagged in the audit log)
    assert result["canonical_key"] is None
    rows = await fns["get_recent_activity"](ctx=make_ctx(db))
    assert rows[0]["canonical_key"] is None
