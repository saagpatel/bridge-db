"""Activity and shipped-event tools."""

import json
import logging
from datetime import date
from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from bridge_db import config
from bridge_db.audit import log_audit
from bridge_db.db import (
    fts_text_for_activity,
    gc_fts_orphans,
    get_db,
    upsert_fts_entry,
)
from bridge_db.models import ACTIVITY_SOURCES, CallerID, invalid_source_error

logger = logging.getLogger("bridge_db.tools.activity")


async def _export_bridge_markdown_after_processing(db: Any) -> None:
    """Keep the fallback bridge file current after shipped-event state changes."""
    try:
        from bridge_db.tools.export import build_markdown

        content = await build_markdown(db)
        config.BRIDGE_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.BRIDGE_FILE_PATH.write_text(content, encoding="utf-8")
        logger.info("auto-export triggered after shipped-event processing")
    except Exception:
        logger.warning("auto-export after shipped-event processing failed", exc_info=True)


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def log_activity(
        caller: Annotated[
            CallerID,
            Field(
                description=(
                    "The system logging this entry: 'cc', 'codex', 'claude_ai', "
                    "'notion_os', or 'personal_ops'"
                )
            ),
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

        cursor = await db.execute(
            """
            INSERT INTO activity_log (source, timestamp, project_name, summary, branch, tags)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (caller, ts, project_name, summary, branch, tags_json),
        )
        activity_id = cursor.lastrowid

        if activity_id is not None:
            await upsert_fts_entry(
                db,
                "activity",
                str(activity_id),
                fts_text_for_activity(project_name, summary, branch),
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
        # Drop FTS rows for any source row that the prune removed.
        await gc_fts_orphans(db, "activity")
        await db.commit()

        log_audit("log_activity", caller, project_name, ok=True)
        logger.info("logged activity: [%s] %s: %s", caller, project_name, summary)
        return {"ok": True, "source": caller, "project_name": project_name, "timestamp": ts}

    @mcp.tool()
    async def get_recent_activity(
        source: Annotated[
            str | None,
            Field(
                description=(
                    "Filter by source: 'cc', 'codex', 'claude_ai', "
                    "'notion_os', or 'personal_ops'. Omit for all."
                )
            ),
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
            if source not in ACTIVITY_SOURCES:
                raise ToolError(invalid_source_error(source))
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
            SELECT
                a.id,
                a.source,
                a.timestamp,
                a.project_name,
                a.summary,
                a.branch,
                a.tags,
                a.created_at,
                r.downstream_system,
                r.downstream_ref,
                r.synced_by,
                r.synced_at,
                r.notes AS sync_notes
            FROM activity_log AS a
            LEFT JOIN shipped_sync_receipts AS r ON r.activity_id = a.id
            {where}
            ORDER BY a.timestamp DESC
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
                "sync_receipt": (
                    {
                        "downstream_system": r["downstream_system"],
                        "downstream_ref": r["downstream_ref"],
                        "synced_by": r["synced_by"],
                        "synced_at": r["synced_at"],
                        "notes": r["sync_notes"],
                    }
                    if r["downstream_ref"] is not None
                    else None
                ),
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
        updated_ids: list[int] = []
        missing_ids: list[int] = []

        # Tags are not indexed in content_index (see fts_text_for_activity), so
        # updating tags does not require re-indexing. If fts_text_for_activity
        # ever starts including tags, add an upsert_fts_entry call here.
        for activity_id in activity_ids:
            cursor = await db.execute("SELECT tags FROM activity_log WHERE id = ?", (activity_id,))
            row = await cursor.fetchone()
            if row is None:
                logger.warning("mark_shipped_processed: id %d not found, skipping", activity_id)
                missing_ids.append(activity_id)
                continue
            current_tags: list[str] = json.loads(row["tags"])
            if "PROCESSED" not in current_tags:
                current_tags.append("PROCESSED")
                await db.execute(
                    "UPDATE activity_log SET tags = ? WHERE id = ?",
                    (json.dumps(current_tags), activity_id),
                )
                updated += 1
                updated_ids.append(activity_id)

        await db.commit()

        await _export_bridge_markdown_after_processing(db)

        log_audit(
            "mark_shipped_processed",
            None,
            None,
            ok=True,
            detail=(
                f"activity_ids={activity_ids} updated_ids={updated_ids} "
                f"missing_ids={missing_ids} updated={updated}/{len(activity_ids)}"
            ),
        )
        logger.info("mark_shipped_processed: updated %d/%d entries", updated, len(activity_ids))
        return {
            "ok": True,
            "updated": updated,
            "total": len(activity_ids),
            "activity_ids": activity_ids,
            "updated_ids": updated_ids,
            "missing_ids": missing_ids,
        }

    @mcp.tool()
    async def confirm_shipped_sync(
        caller: Annotated[CallerID, Field(description="System confirming downstream sync")],
        activity_id: Annotated[int, Field(description="SHIPPED activity entry that was synced")],
        downstream_system: Annotated[
            str,
            Field(description="External system updated, e.g. 'notion' or 'github'"),
        ],
        downstream_ref: Annotated[
            str,
            Field(description="Durable downstream reference, e.g. Notion page ID or URL"),
        ],
        notes: Annotated[
            str | None,
            Field(description="Optional short sync note or operator evidence"),
        ] = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Record downstream proof, then mark one SHIPPED activity event PROCESSED."""
        system = downstream_system.strip()
        ref = downstream_ref.strip()
        if not system:
            raise ToolError("downstream_system must not be empty")
        if not ref:
            raise ToolError("downstream_ref must not be empty")

        db = get_db(ctx)
        cursor = await db.execute(
            "SELECT tags, project_name FROM activity_log WHERE id = ?", (activity_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise ToolError(f"No activity entry found with id {activity_id}")

        current_tags: list[str] = json.loads(row["tags"])
        if "SHIPPED" not in current_tags:
            raise ToolError(f"Activity entry {activity_id} is not tagged SHIPPED")

        processed_added = False
        if "PROCESSED" not in current_tags:
            current_tags.append("PROCESSED")
            await db.execute(
                "UPDATE activity_log SET tags = ? WHERE id = ?",
                (json.dumps(current_tags), activity_id),
            )
            processed_added = True

        await db.execute(
            """
            INSERT INTO shipped_sync_receipts (
                activity_id, downstream_system, downstream_ref, synced_by, notes
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(activity_id) DO UPDATE SET
                downstream_system = excluded.downstream_system,
                downstream_ref = excluded.downstream_ref,
                synced_by = excluded.synced_by,
                synced_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                notes = excluded.notes
            """,
            (activity_id, system, ref, caller, notes),
        )
        await db.commit()

        await _export_bridge_markdown_after_processing(db)

        detail = f"activity_id={activity_id} downstream={system}:{ref}"
        log_audit("confirm_shipped_sync", caller, row["project_name"], ok=True, detail=detail)
        logger.info(
            "confirmed shipped sync: id=%d downstream=%s:%s by=%s",
            activity_id,
            system,
            ref,
            caller,
        )
        return {
            "ok": True,
            "activity_id": activity_id,
            "project_name": row["project_name"],
            "processed_added": processed_added,
            "downstream_system": system,
            "downstream_ref": ref,
            "synced_by": caller,
        }
