"""Activity log tools: log_activity, get_recent_activity, get_shipped_events, mark_shipped_processed."""

import json
import logging
from datetime import date
from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from bridge_db import config
from bridge_db.db import get_db
from bridge_db.models import CallerID

logger = logging.getLogger("bridge_db.tools.activity")


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def log_activity(
        caller: Annotated[
            CallerID,
            Field(description="The system logging this entry: 'cc', 'codex', or 'claude_ai'"),
        ],
        project_name: Annotated[str, Field(description="Project name, e.g. 'bridge-db'")],
        summary: Annotated[str, Field(description="One-line description of what was done")],
        branch: Annotated[str | None, Field(description="Git branch name, if applicable")] = None,
        tags: Annotated[
            list[str] | None, Field(description="Optional tags, e.g. ['SHIPPED']")
        ] = None,
        timestamp: Annotated[
            str | None, Field(description="Date in YYYY-MM-DD format; defaults to today")
        ] = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Log a session activity entry. Auto-prunes to the most recent 50 entries per source."""
        db = get_db(ctx)
        ts = timestamp or str(date.today())
        tags_json = json.dumps(tags or [])

        await db.execute(
            """
            INSERT INTO activity_log (source, timestamp, project_name, summary, branch, tags)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (caller, ts, project_name, summary, branch, tags_json),
        )

        # Prune to retention limit per source
        await db.execute(
            """
            DELETE FROM activity_log
            WHERE source = ? AND id NOT IN (
                SELECT id FROM activity_log WHERE source = ?
                ORDER BY created_at DESC LIMIT ?
            )
            """,
            (caller, caller, config.ACTIVITY_RETENTION_PER_SOURCE),
        )
        await db.commit()

        logger.info("logged activity: [%s] %s: %s", caller, project_name, summary)
        return {"ok": True, "source": caller, "project_name": project_name, "timestamp": ts}

    @mcp.tool()
    async def get_recent_activity(
        source: Annotated[
            str | None,
            Field(description="Filter by source: 'cc', 'codex', or 'claude_ai'. Omit for all."),
        ] = None,
        limit: Annotated[int, Field(description="Max entries to return", ge=1, le=200)] = 20,
        since: Annotated[
            str | None, Field(description="Only entries on or after this YYYY-MM-DD date")
        ] = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> list[dict[str, Any]]:
        """Return recent activity entries, newest first."""
        db = get_db(ctx)

        conditions: list[str] = []
        params: list[Any] = []

        if source is not None:
            conditions.append("source = ?")
            params.append(source)
        if since is not None:
            conditions.append("timestamp >= ?")
            params.append(since)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)

        cursor = await db.execute(
            f"""
            SELECT id, source, timestamp, project_name, summary, branch, tags, created_at
            FROM activity_log
            {where}
            ORDER BY timestamp DESC, created_at DESC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "source": r["source"],
                "timestamp": r["timestamp"],
                "project_name": r["project_name"],
                "summary": r["summary"],
                "branch": r["branch"],
                "tags": json.loads(r["tags"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    @mcp.tool()
    async def get_shipped_events(
        since: Annotated[
            str | None, Field(description="Only shipped events on or after YYYY-MM-DD")
        ] = None,
        unprocessed_only: Annotated[
            bool, Field(description="If true, exclude events already marked PROCESSED")
        ] = False,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> list[dict[str, Any]]:
        """Return activity entries tagged SHIPPED, for Codex bridge-sync to sync to Notion."""
        db = get_db(ctx)

        conditions = ["EXISTS (SELECT 1 FROM json_each(tags) WHERE value = 'SHIPPED')"]
        params: list[Any] = []

        if since:
            conditions.append("timestamp >= ?")
            params.append(since)
        if unprocessed_only:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM json_each(tags) WHERE value = 'PROCESSED')"
            )

        where = "WHERE " + " AND ".join(conditions)
        cursor = await db.execute(
            f"""
            SELECT id, source, timestamp, project_name, summary, branch, tags, created_at
            FROM activity_log
            {where}
            ORDER BY timestamp DESC
            """,
            params,
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "source": r["source"],
                "timestamp": r["timestamp"],
                "project_name": r["project_name"],
                "summary": r["summary"],
                "branch": r["branch"],
                "tags": json.loads(r["tags"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    @mcp.tool()
    async def mark_shipped_processed(
        activity_ids: Annotated[
            list[int], Field(description="IDs of activity entries to mark as PROCESSED")
        ],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Add 'PROCESSED' tag to shipped events so Codex bridge-sync doesn't re-process them."""
        if not activity_ids:
            raise ToolError("activity_ids must not be empty")

        db = get_db(ctx)
        updated = 0

        for activity_id in activity_ids:
            cursor = await db.execute("SELECT tags FROM activity_log WHERE id = ?", (activity_id,))
            row = await cursor.fetchone()
            if row is None:
                logger.warning("mark_shipped_processed: id %d not found, skipping", activity_id)
                continue
            current_tags: list[str] = json.loads(row["tags"])
            if "PROCESSED" not in current_tags:
                current_tags.append("PROCESSED")
                await db.execute(
                    "UPDATE activity_log SET tags = ? WHERE id = ?",
                    (json.dumps(current_tags), activity_id),
                )
                updated += 1

        await db.commit()
        logger.info("mark_shipped_processed: updated %d/%d entries", updated, len(activity_ids))
        return {"ok": True, "updated": updated, "total": len(activity_ids)}
