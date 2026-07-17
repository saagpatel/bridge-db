"""Shared pytest fixtures for bridge-db tests."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import aiosqlite
import pytest
from mcp.server.fastmcp import FastMCP

from bridge_db import config
from bridge_db.db import open_db
from bridge_db.invariants import reset_sometimes_counts
from bridge_db.tools import recall as recall_tool


@pytest.fixture(autouse=True)
def isolate_jsonl_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests from appending audit or recall events to live operator logs.

    Also pins AUTH_MODE to "off" so the suite is env-independent: tests that
    call make_ctx(db) with principal=None pass regardless of the shell env.
    Individual tests that need warn/enforce override this with their own
    monkeypatch.setattr(config, "AUTH_MODE", ...) — monkeypatch is per-test
    so those overrides do not bleed into other tests.
    """
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(
        config, "AUDIT_FAILURE_LOG_PATH", tmp_path / "audit_failures.jsonl"
    )
    monkeypatch.setattr(
        recall_tool, "RECALL_LOG_PATH", tmp_path / "recall_query_log.jsonl"
    )
    monkeypatch.setattr(config, "AUTH_MODE", "off")


@pytest.fixture(autouse=True)
def reset_invariant_counters() -> None:
    """Clear the module-global sometimes() counters between every test.

    Any test that drives _upsert_section, pick_up_handoff, clear_handoff, or
    log_activity increments them; without a suite-wide reset, counter
    assertions become order-dependent.
    """
    reset_sometimes_counts()


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Open a real SQLite DB (tmp_path, WAL mode) with schema applied."""
    conn = await open_db(tmp_path / "test.db")
    yield conn
    await conn.close()


def make_ctx(
    conn: aiosqlite.Connection,
    principal: str | None = None,
    credential_hash: str | None = None,
) -> Any:
    """Build a minimal mock Context satisfying ctx.request_context.lifespan_context.

    `principal` mirrors AppContext.principal — the channel-bound identity used
    by auth.require_caller. Defaults to None (unbound), matching legacy tests.
    """
    # Alias before the class body: `principal = principal` inside a class body
    # raises NameError (class bodies cannot close over a name they also bind).
    bound_principal = principal
    bound_credential_hash = credential_hash

    class _AppContext:
        db = conn
        principal = bound_principal
        credential_hash = bound_credential_hash

    class _RequestContext:
        lifespan_context = _AppContext()

    ctx = MagicMock()
    ctx.request_context = _RequestContext()
    return ctx


class CaptureMCP(FastMCP):
    """FastMCP subclass that captures registered tool functions by name.

    Usage:
        cap = CaptureMCP("test")
        some_module.register(cap)
        result = await cap.fns["log_activity"](arg1=..., ctx=make_ctx(db))
    """

    def __init__(self, name: str = "test") -> None:
        super().__init__(name)
        self.fns: dict[str, Any] = {}

    def tool(self) -> Any:  # type: ignore[override]
        def decorator(fn: Any) -> Any:
            self.fns[fn.__name__] = fn
            return fn

        return decorator


# ── Sample data factories ────────────────────────────────────────────────────


def make_activity(
    source: str = "cc",
    project_name: str = "TestProject",
    summary: str = "Did some work",
    branch: str | None = "feat/test",
    tags: list[str] | None = None,
    timestamp: str = "2026-04-14",
) -> dict[str, Any]:
    return {
        "source": source,
        "project_name": project_name,
        "summary": summary,
        "branch": branch,
        "tags": json.dumps(tags or []),
        "timestamp": timestamp,
    }


def make_handoff(
    project_name: str = "TestProject",
    project_path: str | None = "/home/user/Projects/TestProject",
    roadmap_file: str | None = "ROADMAP.md",
    phase: str | None = "Phase 2",
) -> dict[str, Any]:
    return {
        "project_name": project_name,
        "project_path": project_path,
        "roadmap_file": roadmap_file,
        "phase": phase,
    }
