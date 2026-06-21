"""Recall tool: lexical search across content_index (FTS5), plus JSONL query log."""

import json
import logging
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from bridge_db import config
from bridge_db.audit import iter_jsonl
from bridge_db.db import get_db
from bridge_db.instruction_boundary import instruction_boundary

logger = logging.getLogger("bridge_db.tools.recall")

_VALID_SCOPES: frozenset[str] = frozenset({"all", "section", "activity", "snapshot", "handoff"})

# Append-only JSONL log of recall queries, co-located with the audit log.
# Used during the Phase -1 dogfood week to decide whether the vector layer
# is worth building.
RECALL_LOG_PATH = config.AUDIT_LOG_PATH.parent / "recall_query_log.jsonl"


# Common English stop words: they match broadly and dilute bm25 ranking on
# intent-shaped queries without adding discriminating signal. Kept deliberately
# small; over-filtering hurts recall more than it helps precision.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "did", "do", "for",
        "from", "had", "has", "have", "how", "in", "is", "it", "of", "on", "or",
        "over", "the", "to", "was", "we", "what", "why", "with",
    }
)


def _tokens_for_match(q: str) -> list[str]:
    """Clean free-form input into FTS5-safe tokens, dropping stop words.

    FTS5 treats " ( ) * : - ^ as operators, so they are stripped to spaces. Stop
    words are then removed so bm25 ranks on discriminating terms. If every token
    is a stop word, the unfiltered tokens are kept (searching something beats
    searching nothing).
    """
    cleaned = re.sub(r"[^\w\s]", " ", q, flags=re.UNICODE)
    tokens = cleaned.split()
    if not tokens:
        return []
    content = [t for t in tokens if t.lower() not in _STOPWORDS]
    return content or tokens


def _match_candidates(tokens: list[str]) -> list[str]:
    """FTS5 MATCH expressions to try in order: AND first (precise), OR fallback.

    Each token is wrapped in double quotes so FTS5 treats it as a literal phrase,
    never as a bare operator: an unquoted query token like "OR"/"NOT"/"NEAR" would
    otherwise crash the MATCH or silently invert it. One token yields a single
    expression; multiple tokens yield the implicit-AND form first (every term in
    one row, high precision) and the OR form second (any term, high recall).
    """
    if not tokens:
        return []
    quoted = [f'"{t}"' for t in tokens]
    if len(quoted) == 1:
        return [quoted[0]]
    return [" ".join(quoted), " OR ".join(quoted)]


def _log_recall(query: str, scope: str, limit: int, n_results: int, caller: str | None) -> None:
    """Append one line to the recall query log. Never raises."""
    try:
        event: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "query": query,
            "scope": scope,
            "limit": limit,
            "n_results": n_results,
            "caller": caller,
        }
        RECALL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(RECALL_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        logger.debug("recall log write failed", exc_info=True)


def collect_recall_stats(days: int = 7) -> dict[str, Any]:
    """Roll up recall query log usage over the last `days` days."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat().replace("+00:00", "Z")

    total = 0
    misses = 0
    empty = 0
    scope_counter: Counter[str] = Counter()
    query_counts: Counter[str] = Counter()
    query_result_sums: dict[str, int] = defaultdict(int)

    for record in iter_jsonl(RECALL_LOG_PATH):
        ts = record.get("ts")
        if not isinstance(ts, str) or ts < cutoff:
            continue
        total += 1
        n_results = record.get("n_results", 0)
        if not isinstance(n_results, int):
            n_results = 0
        if n_results == 0:
            misses += 1
        query = record.get("query", "")
        if not isinstance(query, str) or not query.strip():
            empty += 1
        else:
            query_counts[query] += 1
            query_result_sums[query] += n_results
        scope = record.get("scope")
        if isinstance(scope, str):
            scope_counter[scope] += 1

    top_queries = [
        {
            "query": q,
            "count": c,
            "avg_results": round(query_result_sums[q] / c, 2),
        }
        for q, c in query_counts.most_common(10)
    ]

    miss_rate = round(misses / total, 4) if total else 0.0

    return {
        "window_days": days,
        "total_queries": total,
        "miss_rate": miss_rate,
        "empty_query_count": empty,
        "top_queries": top_queries,
        "scope_breakdown": dict(scope_counter),
    }


async def _preview_and_trust(db: Any, source_type: str, source_id: str) -> tuple[str, str | None]:
    """Return (preview, source_trust) for a result, joined from the source row.

    Returns ("", None) if the source row is missing (orphan FTS entry) — this is
    defensive; gc_fts_orphans should prevent that case. source_trust is read from
    the source row, not content_index (which stays UNINDEXED metadata only).
    """
    if source_type == "section":
        cursor = await db.execute(
            "SELECT content, source_trust FROM context_sections WHERE section_name = ?",
            (source_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return "", None
        return (row["content"] or "")[:200], row["source_trust"]
    if source_type == "activity":
        cursor = await db.execute(
            "SELECT summary, project_name, source_trust FROM activity_log WHERE id = ?",
            (int(source_id),),
        )
        row = await cursor.fetchone()
        if row is None:
            return "", None
        return f"{row['project_name']}: {row['summary']}"[:200], row["source_trust"]
    if source_type == "snapshot":
        cursor = await db.execute(
            "SELECT data, source_trust FROM system_snapshots WHERE id = ?", (int(source_id),)
        )
        row = await cursor.fetchone()
        if row is None:
            return "", None
        return (row["data"] or "")[:200], row["source_trust"]
    if source_type == "handoff":
        cursor = await db.execute(
            "SELECT project_name, phase, source_trust FROM pending_handoffs WHERE id = ?",
            (int(source_id),),
        )
        row = await cursor.fetchone()
        if row is None:
            return "", None
        phase = f" ({row['phase']})" if row["phase"] else ""
        return f"{row['project_name']}{phase}"[:200], row["source_trust"]
    return "", None


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def recall(
        query: Annotated[
            str, Field(description="Free-form text to match against bridge-db content")
        ],
        limit: Annotated[int, Field(description="Max results to return", ge=1, le=50)] = 10,
        scope: Annotated[
            Literal["all", "section", "activity", "snapshot", "handoff"],
            Field(description="Limit results to one source type, or 'all'"),
        ] = "all",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> list[dict[str, Any]]:
        """Lexical search over sections, activity, snapshots, and handoffs via FTS5.

        Returns results ranked by bm25. Query syntax is sanitized — special
        FTS5 operators in the input are stripped.
        """
        if scope not in _VALID_SCOPES:
            raise ToolError(f"Invalid scope '{scope}'. Allowed: {sorted(_VALID_SCOPES)}")

        clamped_limit = max(1, min(limit, 50))
        tokens = _tokens_for_match(query)

        if not tokens:
            _log_recall(query, scope, clamped_limit, 0, None)
            return []

        db = get_db(ctx)

        scope_clause = ""
        scope_param: list[Any] = []
        if scope != "all":
            scope_clause = " AND source_type = ?"
            scope_param.append(scope)

        # AND-first, OR-fallback: try the precise (implicit-AND) match, and widen
        # to OR only when AND returns nothing. Keeps partial-match recall while
        # gaining precision whenever every term co-occurs in a row.
        rows: list[Any] = []
        for match_expr in _match_candidates(tokens):
            cursor = await db.execute(
                f"""
                SELECT
                    source_type,
                    source_id,
                    snippet(content_index, 2, '[', ']', '…', 12) AS snippet,
                    bm25(content_index) AS bm25_score
                FROM content_index
                WHERE content_index MATCH ?{scope_clause}
                ORDER BY bm25_score
                LIMIT ?
                """,  # noqa: S608 — scope_clause is from a closed literal
                [match_expr, *scope_param, clamped_limit],
            )
            rows = await cursor.fetchall()
            if rows:
                break

        results: list[dict[str, Any]] = []
        for r in rows:
            preview, source_trust = await _preview_and_trust(db, r["source_type"], r["source_id"])
            results.append(
                {
                    "source_type": r["source_type"],
                    "source_id": r["source_id"],
                    "snippet": r["snippet"],
                    "bm25_score": r["bm25_score"],
                    "preview": preview,
                    "source_trust": source_trust,
                    "instruction_boundary": instruction_boundary(source_trust),
                }
            )

        _log_recall(query, scope, clamped_limit, len(results), None)
        return results

    @mcp.tool()
    async def recall_stats(
        days: Annotated[
            int, Field(description="Window in days (counting back from now)", ge=1, le=365)
        ] = 7,
    ) -> dict[str, Any]:
        """Roll-up analytics over recall_query_log.jsonl for the last `days` days.

        Answers "is recall earning its keep?" — returns total volume, miss rate,
        top queries by count, and per-scope usage. Empty-string queries are
        counted separately so they don't distort the top-queries ranking.
        """
        return collect_recall_stats(days)
