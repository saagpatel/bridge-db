"""Handoff queue tools: create_handoff, get_pending_handoffs, pick_up_handoff, clear_handoff."""

import logging
from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from bridge_db.audit import log_audit
from bridge_db.db import fts_text_for_handoff, get_db, upsert_fts_entry
from bridge_db.models import CallerID
from bridge_db.project_resolver import resolve as resolve_project

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
        resolution = resolve_project(project_name)
        cursor = await db.execute(
            """
            INSERT INTO pending_handoffs
                (project_name, project_path, roadmap_file, phase, dispatched_from, canonical_key)
            VALUES (?, ?, ?, ?, 'claude_ai', ?)
            """,
            (project_name, project_path, roadmap_file, phase, resolution.canonical_key),
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

        log_audit("create_handoff", caller, project_name, ok=True)
        if resolution.registry_present and not resolution.matched:
            # Drift: a real handoff with no canonical match. Surface it via the
            # audit log rather than silently recording an unresolvable name, so a
            # later clear_handoff alias can't expect a canonical match that the
            # registry never produced.
            log_audit(
                "create_handoff.unmatched_project",
                caller,
                project_name,
                ok=True,
                detail="no canonical match in project-registry; flagged for triage",
            )
            logger.warning(
                "create_handoff: unmatched project_name %r (no canonical key)", project_name
            )

        logger.info("handoff created: id=%d project=%s", handoff_id, project_name)
        return {
            "ok": True,
            "handoff_id": handoff_id,
            "project_name": project_name,
            "canonical_key": resolution.canonical_key,
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
                   dispatched_from, dispatched_at, status, canonical_key
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
                "canonical_key": r["canonical_key"],
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
            "SELECT id, project_name, status, canonical_key FROM pending_handoffs WHERE id = ?",
            (handoff_id,),
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
            "canonical_key": row["canonical_key"],
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
        # Match by exact project_name OR — when the incoming name resolves through
        # the canonical registry — by shared canonical_key, so a handoff dispatched
        # as "IncidentMgmt" still clears when /end passes "IncidentManagement" (both
        # resolve to the same canonical key). The exact-name path is always present,
        # preserving today's behavior for rows with no canonical_key (F1).
        canonical = resolve_project(project_name).canonical_key
        if canonical is not None:
            match_sql = "(project_name = ? OR canonical_key = ?)"
            match_params: tuple[str, ...] = (project_name, canonical)
        else:
            match_sql = "project_name = ?"
            match_params = (project_name,)

        cursor = await db.execute(
            f"""
            SELECT id
            FROM pending_handoffs
            WHERE {match_sql} AND status != 'cleared'
            ORDER BY dispatched_at DESC, id DESC
            """,
            match_params,
        )
        rows = await cursor.fetchall()
        if not rows:
            # Not an error — handoff may not exist; /end calls this opportunistically
            return {"ok": True, "cleared": False, "reason": "No active handoff found for project"}

        handoff_ids = [row["id"] for row in rows]
        await db.execute(
            f"""
            UPDATE pending_handoffs
            SET status = 'cleared', cleared_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE {match_sql} AND status != 'cleared'
            """,
            match_params,
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
            "canonical_key": canonical,
        }
