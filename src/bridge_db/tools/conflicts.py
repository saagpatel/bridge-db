"""Write-conflict receipt tools."""

import json
from typing import Annotated, Any, cast

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from bridge_db.db import get_db

_VALID_SURFACES = frozenset({"context_section", "markdown_sync", "handoff"})
_VALID_STATUSES = frozenset({"open", "acknowledged", "resolved", "ignored"})


def _decode_detail(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = cast(object, json.loads(raw))
    except json.JSONDecodeError:
        return {"raw": raw}
    if isinstance(parsed, dict):
        return {str(key): value for key, value in cast(dict[object, Any], parsed).items()}
    return {"value": parsed}


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def get_write_conflicts(
        status: Annotated[
            str | None,
            Field(description="Optional status filter: open, acknowledged, resolved, ignored"),
        ] = "open",
        surface: Annotated[
            str | None,
            Field(description="Optional surface filter: context_section, markdown_sync, handoff"),
        ] = None,
        limit: Annotated[int, Field(description="Max receipts to return", ge=1, le=200)] = 20,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> list[dict[str, Any]]:
        """Return durable receipts for rejected or raced bridge writes."""
        if status is not None and status not in _VALID_STATUSES:
            raise ToolError(f"Invalid status '{status}'. Allowed: {sorted(_VALID_STATUSES)}")
        if surface is not None and surface not in _VALID_SURFACES:
            raise ToolError(f"Invalid surface '{surface}'. Allowed: {sorted(_VALID_SURFACES)}")

        conditions: list[str] = []
        params: list[Any] = []
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        if surface is not None:
            conditions.append("surface = ?")
            params.append(surface)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(limit)

        db = get_db(ctx)
        cursor = await db.execute(
            f"""
            SELECT
                id, surface, target_key, operation, attempted_by, principal,
                stale_version, current_version, stale_updated_at, current_updated_at,
                attempted_source_trust, current_source_trust,
                attempted_content_sha256, current_content_sha256,
                reason, status, detail_json, identity_hash, occurrence_count,
                last_seen_at, aggregation_state, created_at
            FROM write_conflicts
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,  # noqa: S608 — where is assembled from fixed predicates only.
            params,
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row["id"],
                "surface": row["surface"],
                "target_key": row["target_key"],
                "operation": row["operation"],
                "attempted_by": row["attempted_by"],
                "principal": row["principal"],
                "stale_version": row["stale_version"],
                "current_version": row["current_version"],
                "stale_updated_at": row["stale_updated_at"],
                "current_updated_at": row["current_updated_at"],
                "attempted_source_trust": row["attempted_source_trust"],
                "current_source_trust": row["current_source_trust"],
                "attempted_content_sha256": row["attempted_content_sha256"],
                "current_content_sha256": row["current_content_sha256"],
                "reason": row["reason"],
                "status": row["status"],
                "detail": _decode_detail(row["detail_json"]),
                "identity_hash": row["identity_hash"],
                "occurrence_count": row["occurrence_count"],
                "first_seen_at": row["created_at"],
                "last_seen_at": row["last_seen_at"] or row["created_at"],
                "aggregation_state": row["aggregation_state"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
