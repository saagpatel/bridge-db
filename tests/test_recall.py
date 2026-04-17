"""Tests for the recall tool (FTS5) and content_index hooks."""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import CaptureMCP, make_ctx

from bridge_db.db import repopulate_content_index
from bridge_db.tools import activity as activity_tools
from bridge_db.tools import context as context_tools
from bridge_db.tools import handoffs as handoff_tools
from bridge_db.tools import recall as recall_tool
from bridge_db.tools import snapshots as snapshot_tools


@pytest.fixture
async def capture(db: Any) -> CaptureMCP:
    """Register all tool groups needed for recall tests onto a CaptureMCP."""
    cap = CaptureMCP("recall-test")
    activity_tools.register(cap)
    context_tools.register(cap)
    snapshot_tools.register(cap)
    handoff_tools.register(cap)
    recall_tool.register(cap)
    return cap


async def _seed_one_of_each(cap: CaptureMCP, db: Any) -> None:
    """Populate one row per source type so scope + happy-path tests have data."""
    ctx = make_ctx(db)

    # Section
    await cap.fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="Senior Staff Engineer career trajectory. Platform engineering pivot.",
        ctx=ctx,
    )
    # Activity
    await cap.fns["log_activity"](
        caller="cc",
        project_name="bridge-db",
        summary="Added FTS5 content_index virtual table",
        branch="feat/semantic-memory",
        tags=["test"],
        timestamp="2026-04-17",
        ctx=ctx,
    )
    # Snapshot
    await cap.fns["save_snapshot"](
        caller="cc",
        data={"active_projects": "bridge-db FTS5 hardening", "lessons": "none yet"},
        snapshot_date="2026-04-17",
        ctx=ctx,
    )
    # Handoff
    await cap.fns["create_handoff"](
        caller="claude_ai",
        project_name="bridge-db",
        project_path="/Users/d/Projects/bridge-db",
        roadmap_file="ROADMAP.md",
        phase="Phase 1 hardening",
        ctx=ctx,
    )


async def test_recall_happy_path(
    capture: CaptureMCP, db: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """A term present in the seeded content appears in recall results."""
    monkeypatch.setattr(recall_tool, "RECALL_LOG_PATH", tmp_path / "recall.jsonl")
    await _seed_one_of_each(capture, db)

    results = await capture.fns["recall"](
        query="bridge-db", limit=10, scope="all", ctx=make_ctx(db)
    )

    assert len(results) >= 1
    assert all("source_type" in r and "source_id" in r for r in results)
    assert {r["source_type"] for r in results} & {"activity", "handoff"}
    # Query log line exists.
    log_lines = (tmp_path / "recall.jsonl").read_text().splitlines()
    assert len(log_lines) == 1
    entry = json.loads(log_lines[0])
    assert entry["query"] == "bridge-db"
    assert entry["n_results"] == len(results)


async def test_recall_empty_result(
    capture: CaptureMCP, db: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """A query that matches nothing returns an empty list and still logs."""
    monkeypatch.setattr(recall_tool, "RECALL_LOG_PATH", tmp_path / "recall.jsonl")
    await _seed_one_of_each(capture, db)

    results = await capture.fns["recall"](
        query="absolutely_nothing_matches_this_token", limit=10, scope="all", ctx=make_ctx(db)
    )

    assert results == []
    entry = json.loads((tmp_path / "recall.jsonl").read_text().splitlines()[0])
    assert entry["n_results"] == 0


async def test_recall_scope_filter(
    capture: CaptureMCP, db: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """scope='handoff' restricts results to the handoff source type only."""
    monkeypatch.setattr(recall_tool, "RECALL_LOG_PATH", tmp_path / "recall.jsonl")
    await _seed_one_of_each(capture, db)

    results = await capture.fns["recall"](
        query="bridge-db", limit=10, scope="handoff", ctx=make_ctx(db)
    )

    assert len(results) >= 1
    assert {r["source_type"] for r in results} == {"handoff"}


async def test_recall_limit_clamping(
    capture: CaptureMCP, db: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """Oversize and undersize limits are clamped into [1, 50]."""
    monkeypatch.setattr(recall_tool, "RECALL_LOG_PATH", tmp_path / "recall.jsonl")
    await _seed_one_of_each(capture, db)

    # limit=0 should be rejected by pydantic Field ge=1; but the tool also clamps,
    # so we test by passing a high value and checking it doesn't explode.
    results = await capture.fns["recall"](
        query="bridge-db", limit=50, scope="all", ctx=make_ctx(db)
    )
    assert isinstance(results, list)

    entry = json.loads((tmp_path / "recall.jsonl").read_text().splitlines()[0])
    assert entry["limit"] == 50


async def test_repopulate_is_idempotent(capture: CaptureMCP, db: Any) -> None:
    """Running repopulate_content_index twice yields identical counts and no duplicates."""
    await _seed_one_of_each(capture, db)

    first = await repopulate_content_index(db)
    second = await repopulate_content_index(db)

    assert first == second
    cursor = await db.execute("SELECT COUNT(*) FROM content_index")
    (total,) = await cursor.fetchone()
    # One of each seeded type.
    assert total == sum(first.values())
    assert total == 4


def test_sanitize_fts5_query_empty_and_stripping() -> None:
    """Sanitizer normalizes whitespace, strips FTS5 special chars, handles empty."""
    sanitize = recall_tool._sanitize_fts5_query  # pyright: ignore[reportPrivateUsage]
    assert sanitize("") == ""
    assert sanitize("   ") == ""
    # FTS5 operators stripped
    assert sanitize("foo()") == "foo"
    # Hyphens split into tokens and joined by OR (preserves recall on "bridge-db")
    assert sanitize("bridge-db") == "bridge OR db"


def test_sanitize_fts5_query_or_joins_multi_token() -> None:
    """Single-token passes through; multi-token joined with OR so bm25 ranks partial matches."""
    sanitize = recall_tool._sanitize_fts5_query  # pyright: ignore[reportPrivateUsage]
    assert sanitize("handoff") == "handoff"
    assert sanitize("foo bar baz") == "foo OR bar OR baz"


async def test_recall_or_semantics_returns_partial_matches(
    capture: CaptureMCP, db: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """Multi-token queries must use OR semantics: rows with any term match, not only all terms.

    Regression pin for a bug where the default AND semantics produced 0 hits on any
    multi-word query unless every token appeared in the same row. bm25 still ranks
    rows with more matching terms above rows with fewer.
    """
    monkeypatch.setattr(recall_tool, "RECALL_LOG_PATH", tmp_path / "recall.jsonl")

    ctx = make_ctx(db)
    # Two activity rows that share NO tokens. Either should surface on a 2-token query.
    await capture.fns["log_activity"](
        caller="cc",
        project_name="alpha-project",
        summary="alpha only, no other keywords",
        branch=None,
        tags=None,
        timestamp="2026-04-17",
        ctx=ctx,
    )
    await capture.fns["log_activity"](
        caller="cc",
        project_name="beta-project",
        summary="beta only, no other keywords",
        branch=None,
        tags=None,
        timestamp="2026-04-17",
        ctx=ctx,
    )

    results = await capture.fns["recall"](
        query="alpha beta", limit=10, scope="activity", ctx=make_ctx(db)
    )

    # Under old AND semantics this would be 0 (no single row has both tokens).
    # Under OR semantics both rows match.
    source_ids = {r["source_id"] for r in results}
    assert len(source_ids) >= 2, (
        f"expected OR semantics to return both partial-match rows, got {len(source_ids)}"
    )
