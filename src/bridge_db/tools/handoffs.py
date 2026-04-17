"""Handoff queue tools: create_handoff, get_pending_handoffs, pick_up_handoff, clear_handoff."""

import logging
from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from bridge_db.db import fts_text_for_handoff, get_db, upsert_fts_entry
from bridge_db.models import CallerID

logger = logging.getLogger("bridge_db.tools.handoffs")


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def create_handoff(
        caller: Annotated[
            CallerID, Field(description="Must be 'claude_ai' — only Claude.ai dispatches handoffs")
        ],
        project_name: Annotated[str, Field(description="Name of the project being handed off")],
        project_path: Annotated[
            str | None, Field(description="Absolute path to the project directory")
        ] = None,
        roadmap_file: Annotated[
            str | None, Field(description="Relative path to the roadmap/plan file")
        ] = None,
        phase: Annotated[
            str | None, Field(description="Phase or step to start from, e.g. 'Phase 2'")
        ] = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Create a project handoff for Claude Code or Codex to pick up. Only claude_ai may dispatch."""
        if caller != "claude_ai":
            raise ToolError(f"Only 'claude_ai' may create handoffs; caller was '{caller}'")

        db = get_db(ctx)
        cursor = await db.execute(
            """
            INSERT INTO pending_handoffs (project_name, project_path, roadmap_file, phase, dispatched_from)
            VALUES (?, ?, ?, ?, 'claude_ai')
            """,
            (project_name, project_path, roadmap_file, phase),
        )
        handoff_id = cursor.lastrowid

        if handoff_id is not None:
            await upsert_fts_entry(
                db,
                "handoff",
                str(handoff_id),
                fts_text_for_handoff(project_name, project_path, roadmap_file, phase),
            )

        await db.commit()

        logger.info("handoff created: id=%d project=%s", handoff_id, project_name)
        return {
            "ok": True,
            "handoff_id": handoff_id,
            "project_name": project_name,
            "status": "pending",
        }

    @mcp.tool()
    async def get_pending_handoffs(
        ctx: Context = None,  # type: ignore[assignment]
    ) -> list[dict[str, Any]]:
        """Return all pending handoffs, newest first. Used by /start to surface priority work."""
        db = get_db(ctx)
        cursor = await db.execute(
            """
            SELECT id, project_name, project_path, roadmap_file, phase,
                   dispatched_from, dispatched_at, status
            FROM pending_handoffs
            WHERE status = 'pending'
            ORDER BY dispatched_at DESC, id DESC
            """
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "project_name": r["project_name"],
                "project_path": r["project_path"],
                "roadmap_file": r["roadmap_file"],
                "phase": r["phase"],
                "dispatched_from": r["dispatched_from"],
                "dispatched_at": r["dispatched_at"],
                "status": r["status"],
            }
            for r in rows
        ]

    @mcp.tool()
    async def pick_up_handoff(
        caller: Annotated[
            CallerID, Field(description="The system picking up the handoff: 'cc' or 'codex'")
        ],
        handoff_id: Annotated[int, Field(description="ID of the handoff to pick up")],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Mark a handoff as active (in progress). Only 'cc' or 'codex' may pick up."""
        if caller not in ("cc", "codex"):
            raise ToolError(f"Only 'cc' or 'codex' may pick up handoffs; caller was '{caller}'")

        db = get_db(ctx)
        cursor = await db.execute(
            "SELECT id, project_name, status FROM pending_handoffs WHERE id = ?", (handoff_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise ToolError(f"No handoff found with id {handoff_id}")
        if row["status"] != "pending":
            raise ToolError(f"Handoff {handoff_id} is not pending (status: {row['status']})")

        await db.execute(
            """
            UPDATE pending_handoffs
            SET status = 'active', picked_up_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE id = ?
            """,
            (handoff_id,),
        )
        await db.commit()
        logger.info("handoff picked up: id=%d by %s", handoff_id, caller)
        return {
            "ok": True,
            "handoff_id": handoff_id,
            "project_name": row["project_name"],
            "status": "active",
        }

    @mcp.tool()
    async def clear_handoff(
        caller: Annotated[
            CallerID, Field(description="Must be 'cc' or 'codex' — clears matched handoffs")
        ],
        project_name: Annotated[str, Field(description="Project name to match and clear")],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Clear a handoff by project name (mark as done). Called by /end after completing project work."""
        if caller not in ("cc", "codex"):
            raise ToolError(f"Only 'cc' or 'codex' may clear handoffs; caller was '{caller}'")

        db = get_db(ctx)
        cursor = await db.execute(
            """
            SELECT id
            FROM pending_handoffs
            WHERE project_name = ? AND status != 'cleared'
            ORDER BY dispatched_at DESC, id DESC
            """,
            (project_name,),
        )
        rows = await cursor.fetchall()
        if not rows:
            # Not an error — handoff may not exist; /end calls this opportunistically
            return {"ok": True, "cleared": False, "reason": "No active handoff found for project"}

        handoff_ids = [row["id"] for row in rows]
        await db.execute(
            """
            UPDATE pending_handoffs
            SET status = 'cleared', cleared_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE project_name = ? AND status != 'cleared'
            """,
            (project_name,),
        )
        await db.commit()
        logger.info(
            "handoffs cleared: project=%s by %s count=%d", project_name, caller, len(handoff_ids)
        )
        return {
            "ok": True,
            "cleared": True,
            "handoff_id": handoff_ids[0],
            "handoff_ids": handoff_ids,
            "cleared_count": len(handoff_ids),
            "project_name": project_name,
        }
