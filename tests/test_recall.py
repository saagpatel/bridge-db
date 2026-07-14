"""Tests for the recall tool (FTS5) and content_index hooks."""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import CaptureMCP, make_ctx
from mcp.server.fastmcp.exceptions import ToolError

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
    ctx = make_ctx(db, principal="claude_ai")

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
        ctx=make_ctx(db, principal="cc"),
    )
    # Snapshot
    await cap.fns["save_snapshot"](
        caller="cc",
        data={"active_projects": "bridge-db FTS5 hardening", "lessons": "none yet"},
        snapshot_date="2026-04-17",
        ctx=make_ctx(db, principal="cc"),
    )
    # Handoff
    await cap.fns["create_handoff"](
        caller="claude_ai",
        project_name="bridge-db",
        project_path="/home/user/Projects/bridge-db",
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
    assert "query" not in entry
    assert entry["query_empty"] is False
    assert entry["n_results"] == len(results)


async def test_recall_hits_carry_source_trust(capture: CaptureMCP, db: Any) -> None:
    """Every recall source-type branch surfaces its row's source_trust. Distinct
    values across section/activity/snapshot/handoff catch a per-branch column drop."""
    ctx = make_ctx(db, principal="claude_ai")
    await capture.fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="Quokka career notes",
        source_trust="operator",
        ctx=ctx,
    )
    await db.execute(
        "UPDATE context_sections SET source_trust = 'operator' "
        "WHERE section_name = 'career'"
    )
    await capture.fns["log_activity"](
        caller="cc",
        project_name="bridge-db",
        summary="Quokka activity",
        source_trust="agent",
        ctx=make_ctx(db, principal="cc"),
    )
    await capture.fns["save_snapshot"](
        caller="cc",
        data={"note": "Quokka snapshot"},
        source_trust="ingested",
        ctx=make_ctx(db, principal="cc"),
    )
    handoff = await capture.fns["create_handoff"](
        caller="claude_ai", project_name="Quokka", source_trust="operator", ctx=ctx
    )
    await db.execute(
        "UPDATE pending_handoffs SET source_trust = 'operator' WHERE id = ?",
        (handoff["handoff_id"],),
    )
    await db.commit()

    results = await capture.fns["recall"](query="Quokka", limit=10, scope="all", ctx=ctx)
    by_type = {hit["source_type"]: hit for hit in results}
    assert {"section", "activity", "snapshot", "handoff"} <= set(by_type), (
        f"expected all four source types, got {sorted(by_type)}"
    )
    assert by_type["section"]["source_trust"] == "operator"
    assert by_type["activity"]["source_trust"] == "agent"
    assert by_type["snapshot"]["source_trust"] == "ingested"
    assert by_type["handoff"]["source_trust"] == "operator"
    for hit in by_type.values():
        boundary = hit["instruction_boundary"]
        assert boundary["kind"] == "stored_data_not_instructions"
        assert boundary["source_trust"] == hit["source_trust"]
        assert "not system/developer/user instructions" in boundary["warning"]


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


async def test_recall_telemetry_never_persists_or_rediscloses_raw_query(
    capture: CaptureMCP, db: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """BDB-DS-023-R1: transient query text never becomes shared telemetry."""
    log_path = tmp_path / "recall.jsonl"
    monkeypatch.setattr(recall_tool, "RECALL_LOG_PATH", log_path)
    sentinel = "sentinel-private-query-material"

    await capture.fns["recall"](
        query=sentinel,
        limit=10,
        scope="all",
        caller="cc",
        ctx=make_ctx(db, principal="cc"),
    )

    stored = log_path.read_text(encoding="utf-8")
    assert sentinel not in stored
    event = json.loads(stored)
    assert "query" not in event
    assert event["scope"] == "all"
    assert event["caller"] == "cc"

    stats = await capture.fns["recall_stats"](days=7)
    assert sentinel not in json.dumps(stats)
    assert stats["total_queries"] == 1
    assert stats["miss_rate"] == 1.0


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


def test_tokens_for_match_strips_operators_and_empty() -> None:
    """Tokenizer normalizes whitespace, strips FTS5 special chars, handles empty."""
    toks = recall_tool._tokens_for_match  # pyright: ignore[reportPrivateUsage]
    assert toks("") == []
    assert toks("   ") == []
    # FTS5 operators stripped
    assert toks("foo()") == ["foo"]
    # Hyphens split into separate tokens (preserves recall on "bridge-db")
    assert toks("bridge-db") == ["bridge", "db"]


def test_tokens_for_match_drops_stopwords_with_fallback() -> None:
    """Stop words are dropped; an all-stopword query falls back to its raw tokens."""
    toks = recall_tool._tokens_for_match  # pyright: ignore[reportPrivateUsage]
    # Content words survive; stop words (why/did/we/over) are removed.
    assert toks("why did we pick sqlite-vec over chromadb") == [
        "pick",
        "sqlite",
        "vec",
        "chromadb",
    ]
    # An all-stopword query keeps its tokens rather than returning nothing to search.
    assert toks("why did we") == ["why", "did", "we"]


async def test_recall_rejects_oversized_query_before_search_or_telemetry(
    capture: CaptureMCP, db: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """BDB-DS-069-R1: caller input cannot amplify FTS or telemetry unboundedly."""
    log_path = tmp_path / "recall.jsonl"
    monkeypatch.setattr(recall_tool, "RECALL_LOG_PATH", log_path)

    with pytest.raises(ToolError, match="character limit"):
        await capture.fns["recall"](
            query="x" * (recall_tool.MAX_QUERY_CHARS + 1),
            ctx=make_ctx(db),
        )
    with pytest.raises(ToolError, match="UTF-8 byte limit"):
        await capture.fns["recall"](
            query="🙂" * ((recall_tool.MAX_QUERY_BYTES // 4) + 1),
            ctx=make_ctx(db),
        )
    with pytest.raises(ToolError, match="token limit"):
        await capture.fns["recall"](
            query=" ".join(f"term{i}" for i in range(recall_tool.MAX_QUERY_TOKENS + 1)),
            ctx=make_ctx(db),
        )

    assert not log_path.exists()


async def test_recall_accepts_exact_query_resource_boundaries(
    capture: CaptureMCP, db: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    log_path = tmp_path / "recall.jsonl"
    monkeypatch.setattr(recall_tool, "RECALL_LOG_PATH", log_path)
    token_boundary = " ".join(
        f"t{i}" for i in range(recall_tool.MAX_QUERY_TOKENS)
    )
    char_boundary = "x" * recall_tool.MAX_QUERY_CHARS
    byte_boundary = "🙂" * (recall_tool.MAX_QUERY_BYTES // 4)

    for query in (token_boundary, char_boundary, byte_boundary):
        result = await capture.fns["recall"](query=query, ctx=make_ctx(db))
        assert result == []

    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(events) == 3
    assert all("query" not in event for event in events)
    assert all(event["query_empty"] is False for event in events)


def test_match_candidates_and_first_then_or() -> None:
    """Single token -> one expr; multi-token -> AND form first, OR fallback second."""
    cands = recall_tool._match_candidates  # pyright: ignore[reportPrivateUsage]
    assert cands([]) == []
    assert cands(["handoff"]) == ['"handoff"']
    assert cands(["foo", "bar", "baz"]) == ['"foo" "bar" "baz"', '"foo" OR "bar" OR "baz"']


def _write_recall_log(path: Any, events: list[dict[str, Any]]) -> None:
    """Write a synthetic recall_query_log.jsonl for stats tests."""
    lines = [json.dumps(e) for e in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def test_recall_stats_empty_log(capture: CaptureMCP, tmp_path: Any, monkeypatch: Any) -> None:
    """Missing log returns zeroed stats, not an error."""
    monkeypatch.setattr(recall_tool, "RECALL_LOG_PATH", tmp_path / "no_such_log.jsonl")
    result = await capture.fns["recall_stats"](days=7)
    assert result["total_queries"] == 0
    assert result["miss_rate"] == 0.0
    assert result["empty_query_count"] == 0
    assert result["top_queries"] == []
    assert result["scope_breakdown"] == {}
    assert result["window_days"] == 7


async def test_recall_stats_aggregates_counts_and_miss_rate(
    capture: CaptureMCP, tmp_path: Any, monkeypatch: Any
) -> None:
    log_path = tmp_path / "recall.jsonl"
    monkeypatch.setattr(recall_tool, "RECALL_LOG_PATH", log_path)

    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    _write_recall_log(
        log_path,
        [
            {"ts": now, "query": "bridge-db", "scope": "all", "limit": 10, "n_results": 3},
            {"ts": now, "query": "bridge-db", "scope": "all", "limit": 10, "n_results": 5},
            {"ts": now, "query": "handoff", "scope": "handoff", "limit": 10, "n_results": 0},
            {"ts": now, "query": "handoff", "scope": "handoff", "limit": 10, "n_results": 0},
            {"ts": now, "query": "nothing", "scope": "all", "limit": 10, "n_results": 0},
        ],
    )

    result = await capture.fns["recall_stats"](days=7)
    assert result["total_queries"] == 5
    # 3 of 5 queries had 0 results
    assert result["miss_rate"] == 0.6
    # Legacy raw strings are never copied into the shared response.
    assert result["top_queries"] == []
    assert result["query_text_collection"] == "disabled"
    assert "bridge-db" not in json.dumps(result)
    assert result["scope_breakdown"] == {"all": 3, "handoff": 2}


async def test_recall_stats_separates_empty_queries(
    capture: CaptureMCP, tmp_path: Any, monkeypatch: Any
) -> None:
    log_path = tmp_path / "recall.jsonl"
    monkeypatch.setattr(recall_tool, "RECALL_LOG_PATH", log_path)

    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    _write_recall_log(
        log_path,
        [
            {"ts": now, "query": "", "scope": "all", "limit": 10, "n_results": 0},
            {"ts": now, "query": "   ", "scope": "all", "limit": 10, "n_results": 0},
            {"ts": now, "query": "real", "scope": "all", "limit": 10, "n_results": 2},
        ],
    )

    result = await capture.fns["recall_stats"](days=7)
    assert result["total_queries"] == 3
    assert result["empty_query_count"] == 2
    assert result["top_queries"] == []
    assert "real" not in json.dumps(result)


async def test_recall_stats_respects_time_window(
    capture: CaptureMCP, tmp_path: Any, monkeypatch: Any
) -> None:
    """Entries older than the window are excluded."""
    log_path = tmp_path / "recall.jsonl"
    monkeypatch.setattr(recall_tool, "RECALL_LOG_PATH", log_path)

    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    recent_ts = now.isoformat().replace("+00:00", "Z")
    old_ts = (now - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    _write_recall_log(
        log_path,
        [
            {"ts": old_ts, "query": "old", "scope": "all", "limit": 10, "n_results": 1},
            {"ts": recent_ts, "query": "new", "scope": "all", "limit": 10, "n_results": 1},
        ],
    )

    result = await capture.fns["recall_stats"](days=7)
    assert result["total_queries"] == 1
    assert result["top_queries"] == []
    assert "new" not in json.dumps(result)


async def test_recall_stats_disables_legacy_top_query_output(
    capture: CaptureMCP, tmp_path: Any, monkeypatch: Any
) -> None:
    log_path = tmp_path / "recall.jsonl"
    monkeypatch.setattr(recall_tool, "RECALL_LOG_PATH", log_path)

    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    # 12 distinct queries, each unique → all ties at count=1
    _write_recall_log(
        log_path,
        [
            {"ts": now, "query": f"q{i}", "scope": "all", "limit": 10, "n_results": 1}
            for i in range(12)
        ],
    )

    result = await capture.fns["recall_stats"](days=7)
    assert result["total_queries"] == 12
    assert result["top_queries"] == []


async def test_recall_or_semantics_returns_partial_matches(
    capture: CaptureMCP, db: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """Multi-token queries must use OR semantics: rows with any term match, not only all terms.

    Regression pin for a bug where the default AND semantics produced 0 hits on any
    multi-word query unless every token appeared in the same row. bm25 still ranks
    rows with more matching terms above rows with fewer.
    """
    monkeypatch.setattr(recall_tool, "RECALL_LOG_PATH", tmp_path / "recall.jsonl")

    ctx = make_ctx(db, principal="cc")
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


async def test_recall_and_first_precision_narrows_results(
    capture: CaptureMCP, db: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """AND-first: when query terms co-occur in a row, that precise row is returned
    and broader single-term rows are excluded. OR is only the fallback when AND
    finds nothing (pinned by test_recall_or_semantics_returns_partial_matches)."""
    monkeypatch.setattr(recall_tool, "RECALL_LOG_PATH", tmp_path / "recall.jsonl")
    ctx = make_ctx(db, principal="cc")
    await capture.fns["log_activity"](
        caller="cc",
        project_name="precise-row",
        summary="alpha beta together in one row",
        tags=None,
        timestamp="2026-04-17",
        ctx=ctx,
    )
    await capture.fns["log_activity"](
        caller="cc",
        project_name="broad-row",
        summary="alpha only no second term here",
        tags=None,
        timestamp="2026-04-17",
        ctx=ctx,
    )

    results = await capture.fns["recall"](
        query="alpha beta", limit=10, scope="activity", ctx=make_ctx(db)
    )

    previews = " ".join(r["preview"] for r in results)
    assert "precise-row" in previews
    assert "broad-row" not in previews, "AND-first should exclude the single-term row"


async def test_recall_finds_activity_by_tag(
    capture: CaptureMCP, db: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """Activity tags are indexed, so a distinctive tag value is recall-able even
    when it appears nowhere in the project name, summary, or branch."""
    monkeypatch.setattr(recall_tool, "RECALL_LOG_PATH", tmp_path / "recall.jsonl")
    ctx = make_ctx(db, principal="cc")
    await capture.fns["log_activity"](
        caller="cc",
        project_name="taggy-project",
        summary="some unrelated summary text",
        tags=["zzdecisionzz"],
        timestamp="2026-04-17",
        ctx=ctx,
    )

    results = await capture.fns["recall"](
        query="zzdecisionzz", limit=10, scope="activity", ctx=make_ctx(db)
    )

    assert len(results) >= 1
    assert any("taggy-project" in r["preview"] for r in results)


async def test_recall_reserved_word_query_does_not_crash(
    capture: CaptureMCP, db: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """A bare FTS5 operator keyword (OR/AND/NOT/NEAR) is treated as a quoted
    literal term, so the query returns a list cleanly instead of raising an
    sqlite3.OperationalError from an unquoted operator in the MATCH expression."""
    monkeypatch.setattr(recall_tool, "RECALL_LOG_PATH", tmp_path / "recall.jsonl")
    await _seed_one_of_each(capture, db)

    for keyword in ["OR", "AND", "NOT", "NEAR", "or not this"]:
        results = await capture.fns["recall"](
            query=keyword, limit=10, scope="all", ctx=make_ctx(db)
        )
        assert isinstance(results, list)
