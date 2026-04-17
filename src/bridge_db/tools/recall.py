"""Recall tool: lexical search across content_index (FTS5), plus JSONL query log."""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from bridge_db import config
from bridge_db.db import get_db

logger = logging.getLogger("bridge_db.tools.recall")

_VALID_SCOPES: frozenset[str] = frozenset({"all", "section", "activity", "snapshot", "handoff"})

# Append-only JSONL log of recall queries, co-located with the audit log.
# Used during the Phase -1 dogfood week to decide whether the vector layer
# is worth building.
RECALL_LOG_PATH = config.AUDIT_LOG_PATH.parent / "recall_query_log.jsonl"


def _sanitize_fts5_query(q: str) -> str:
    """Strip FTS5 special characters and collapse whitespace.

    FTS5 treats " ( ) * : - ^ as operators. User-facing `recall` should accept
    free-form strings; sanitizing gives an AND-of-terms behavior without
    surprising syntax errors on hyphens like "bridge-db".
    """
    cleaned = re.sub(r"[^\w\s]", " ", q, flags=re.UNICODE)
    return " ".join(cleaned.split())


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


async def _preview_for(db: Any, source_type: str, source_id: str) -> str:
    """Return a short preview string for a result, joined from the source row.

    Returns empty string if the source row is missing (orphan FTS entry) —
    this is defensive; gc_fts_orphans should prevent that case.
    """
    if source_type == "section":
        cursor = await db.execute(
            "SELECT content FROM context_sections WHERE section_name = ?", (source_id,)
        )
        row = await cursor.fetchone()
        return (row["content"] or "")[:200] if row else ""
    if source_type == "activity":
        cursor = await db.execute(
            "SELECT summary, project_name FROM activity_log WHERE id = ?", (int(source_id),)
        )
        row = await cursor.fetchone()
        if row is None:
            return ""
        return f"{row['project_name']}: {row['summary']}"[:200]
    if source_type == "snapshot":
        cursor = await db.execute(
            "SELECT data FROM system_snapshots WHERE id = ?", (int(source_id),)
        )
        row = await cursor.fetchone()
        return (row["data"] or "")[:200] if row else ""
    if source_type == "handoff":
        cursor = await db.execute(
            "SELECT project_name, phase FROM pending_handoffs WHERE id = ?", (int(source_id),)
        )
        row = await cursor.fetchone()
        if row is None:
            return ""
        phase = f" ({row['phase']})" if row["phase"] else ""
        return f"{row['project_name']}{phase}"[:200]
    return ""


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
        sanitized = _sanitize_fts5_query(query)

        if not sanitized:
            _log_recall(query, scope, clamped_limit, 0, None)
            return []

        db = get_db(ctx)

        params: list[Any] = [sanitized]
        scope_clause = ""
        if scope != "all":
            scope_clause = " AND source_type = ?"
            params.append(scope)
        params.append(clamped_limit)

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
            params,
        )
        rows = await cursor.fetchall()

        results: list[dict[str, Any]] = []
        for r in rows:
            preview = await _preview_for(db, r["source_type"], r["source_id"])
            results.append(
                {
                    "source_type": r["source_type"],
                    "source_id": r["source_id"],
                    "snippet": r["snippet"],
                    "bm25_score": r["bm25_score"],
                    "preview": preview,
                }
            )

        _log_recall(query, scope, clamped_limit, len(results), None)
        return results
