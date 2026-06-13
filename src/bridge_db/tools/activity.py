"""Activity and shipped-event tools."""

import json
import logging
from datetime import date
from typing import Annotated, Any, cast

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from bridge_db import config
from bridge_db.audit import log_audit
from bridge_db.auth import clamp_source_trust, require_caller
from bridge_db.db import (
    get_db,
    insert_activity_row,
)
from bridge_db.models import ACTIVITY_SOURCES, CallerID, SourceTrust, invalid_source_error
from bridge_db.project_resolver import resolve as resolve_project

logger = logging.getLogger("bridge_db.tools.activity")

_SHIPPED_EVENT_DISPOSITION_TYPES = {
    "unsynced_by_policy",
    "no_durable_target",
    "superseded_without_receipt",
    "declined_mapping",
}


def _normalize_policy_key(value: str) -> str:
    return value.strip().lower()


def _load_meta_shipped_event_policy(project_name: str) -> dict[str, Any] | None:
    """Return meta-event policy for SHIPPED rows that intentionally skip Notion."""
    try:
        raw = json.loads(config.META_SHIPPED_EVENTS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    policy_root = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
    projects = policy_root.get("projects")
    if not isinstance(projects, dict):
        return None
    project_policies = cast(dict[str, Any], projects)
    policy = project_policies.get(_normalize_policy_key(project_name))
    if not isinstance(policy, dict):
        return None
    policy_fields = cast(dict[str, Any], policy)
    reason = policy_fields.get("reason")
    record_outcome_in = policy_fields.get("record_outcome_in")
    return {
        "reason": reason
        if isinstance(reason, str) and reason
        else "meta event has no Notion target",
        "record_outcome_in": (
            record_outcome_in
            if isinstance(record_outcome_in, str) and record_outcome_in
            else "bridge-db shipped_sync_receipts"
        ),
    }


async def _export_bridge_markdown_after_processing(db: Any) -> None:
    """Keep the fallback bridge file current after shipped-event state changes."""
    try:
        from bridge_db.tools.export import build_markdown, write_bridge_file

        content = await build_markdown(db)
        write_bridge_file(content)
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
        source_trust: Annotated[
            SourceTrust,
            Field(
                description="Provenance: 'operator' (operator-asserted), 'agent' "
                "(Claude-authored, default), or 'ingested' (external)"
            ),
        ] = "agent",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Log a session activity entry. Auto-prunes to the most recent 50 entries per source."""
        require_caller(ctx, caller, tool="log_activity")
        source_trust, source_trust_clamped = clamp_source_trust(
            source_trust, caller=caller, tool="log_activity"
        )
        db = get_db(ctx)
        ts = timestamp or str(date.today())
        resolution = resolve_project(project_name)
        await insert_activity_row(
            db,
            source=caller,
            timestamp=ts,
            project_name=project_name,
            summary=summary,
            branch=branch,
            tags=tags,
            retention_limit=config.ACTIVITY_RETENTION_PER_SOURCE,
            canonical_key=resolution.canonical_key,
            source_trust=source_trust,
        )
        await db.commit()

        log_audit("log_activity", caller, project_name, ok=True)
        if resolution.registry_present and not resolution.matched:
            # Drift: a real write with no canonical match. Surface it via the
            # existing audit log rather than silently recording the drifted name.
            log_audit(
                "log_activity.unmatched_project",
                caller,
                project_name,
                ok=True,
                detail="no canonical match in project-registry; flagged for triage",
            )
            logger.warning(
                "log_activity: unmatched project_name %r (no canonical key)", project_name
            )
        logger.info("logged activity: [%s] %s: %s", caller, project_name, summary)
        return {
            "ok": True,
            "source": caller,
            "project_name": project_name,
            "canonical_key": resolution.canonical_key,
            "source_trust": source_trust,
            "source_trust_clamped": source_trust_clamped,
            "timestamp": ts,
        }

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
            SELECT id, source, timestamp, project_name, summary, branch, tags, created_at, canonical_key
            FROM activity_log
            {where}
            ORDER BY timestamp DESC, created_at DESC, id DESC
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
                "canonical_key": r["canonical_key"],
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
                a.canonical_key,
                r.downstream_system,
                r.downstream_ref,
                r.synced_by,
                r.synced_at,
                r.notes AS sync_notes,
                d.disposition_type,
                d.policy_ref,
                d.reason AS disposition_reason,
                d.decided_by,
                d.decided_at,
                d.notes AS disposition_notes
            FROM activity_log AS a
            LEFT JOIN shipped_sync_receipts AS r ON r.activity_id = a.id
            LEFT JOIN shipped_event_dispositions AS d ON d.activity_id = a.id
            {where}
            ORDER BY a.timestamp DESC
            """,
            params,
        )
        rows = await cursor.fetchall()
        events: list[dict[str, Any]] = []
        for r in rows:
            resolution = resolve_project(r["project_name"])
            canonical_key = r["canonical_key"] or resolution.canonical_key
            meta_policy = _load_meta_shipped_event_policy(r["project_name"])
            notion_sync: dict[str, Any]
            if meta_policy is not None:
                notion_sync = {
                    "state": "meta_no_target",
                    "reason": meta_policy["reason"],
                    "canonical_key": canonical_key,
                    "notion_page_id": None,
                    "notion_title": None,
                    "record_outcome_in": meta_policy["record_outcome_in"],
                    "policy_ref": str(config.META_SHIPPED_EVENTS_PATH),
                }
            elif not resolution.registry_present:
                notion_sync = {
                    "state": "registry_unavailable",
                    "reason": "project registry is absent or unreadable",
                    "canonical_key": canonical_key,
                    "notion_page_id": None,
                    "notion_title": None,
                }
            elif not resolution.matched:
                notion_sync = {
                    "state": "unmatched",
                    "reason": "no canonical project match in project registry",
                    "canonical_key": None,
                    "notion_page_id": None,
                    "notion_title": None,
                }
            elif resolution.notion_page_id is None:
                notion_sync = {
                    "state": "no_notion_target",
                    "reason": "canonical project has no notion_local_page_id",
                    "canonical_key": canonical_key,
                    "notion_page_id": None,
                    "notion_title": resolution.notion_title,
                }
            else:
                notion_sync = {
                    "state": "ready",
                    "reason": "canonical project has explicit notion_local_page_id",
                    "canonical_key": canonical_key,
                    "notion_page_id": resolution.notion_page_id,
                    "notion_title": resolution.notion_title,
                }
            events.append(
                {
                    "id": r["id"],
                    "source": r["source"],
                    "timestamp": r["timestamp"],
                    "project_name": r["project_name"],
                    "canonical_key": canonical_key,
                    "notion_sync": notion_sync,
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
                    "policy_disposition": (
                        {
                            "disposition_type": r["disposition_type"],
                            "policy_ref": r["policy_ref"],
                            "reason": r["disposition_reason"],
                            "decided_by": r["decided_by"],
                            "decided_at": r["decided_at"],
                            "notes": r["disposition_notes"],
                        }
                        if r["disposition_type"] is not None
                        else None
                    ),
                }
            )
        return events

    @mcp.tool()
    async def mark_shipped_processed(
        activity_ids: Annotated[
            list[int], Field(description="IDs of activity entries to mark as PROCESSED")
        ],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Add 'PROCESSED' tag to activity events so they are not re-processed.

        LEGACY / receiptless path. It records NO downstream proof, so for genuine
        SHIPPED artifacts prefer ``confirm_shipped_sync`` (which writes a
        ``shipped_sync_receipts`` row). This tool is retained for non-shipped
        operational events (TASK_DONE / APPROVAL_SENT / PLANNING_APPLIED /
        REVIEW_CLOSED) that have no shipped-receipt lifecycle.

        Guard (F7): if any passed id IS tagged SHIPPED, processing it here bypasses
        the receipt and creates exactly the drift ``health.processed_shipped_without_receipt``
        detects. We still process it (non-breaking for existing callers) but flag it
        loudly via a warning + a non-ok audit record and return the offending ids in
        ``shipped_bypass_ids`` so the misuse is observable at the source, not only
        after the fact.
        """
        if not activity_ids:
            raise ToolError("activity_ids must not be empty")

        db = get_db(ctx)
        updated = 0
        updated_ids: list[int] = []
        missing_ids: list[int] = []
        shipped_bypass_ids: list[int] = []

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
            if "SHIPPED" in current_tags:
                shipped_bypass_ids.append(activity_id)
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

        if shipped_bypass_ids:
            logger.warning(
                "mark_shipped_processed: SHIPPED ids %s processed without a receipt — "
                "use confirm_shipped_sync for shipped artifacts (F7 drift vector)",
                shipped_bypass_ids,
            )
        log_audit(
            "mark_shipped_processed",
            None,
            None,
            ok=not shipped_bypass_ids,
            detail=(
                f"activity_ids={activity_ids} updated_ids={updated_ids} "
                f"missing_ids={missing_ids} updated={updated}/{len(activity_ids)}"
                + (f" shipped_bypass_ids={shipped_bypass_ids}" if shipped_bypass_ids else "")
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
            "shipped_bypass_ids": shipped_bypass_ids,
        }

    @mcp.tool()
    async def record_shipped_event_disposition(
        caller: Annotated[CallerID, Field(description="System recording the disposition")],
        activity_id: Annotated[int, Field(description="SHIPPED activity entry to classify")],
        disposition_type: Annotated[
            str,
            Field(
                description=(
                    "One of: unsynced_by_policy, no_durable_target, "
                    "superseded_without_receipt, declined_mapping"
                )
            ),
        ],
        reason: Annotated[str, Field(description="Why this event is not receipt-ready")],
        policy_ref: Annotated[
            str | None,
            Field(description="Optional durable policy or report reference"),
        ] = None,
        notes: Annotated[
            str | None,
            Field(description="Optional operator evidence or follow-up notes"),
        ] = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Record a non-receipt disposition for a SHIPPED event.

        This is not a downstream receipt and does not mark the event PROCESSED.
        It is for rows that should remain auditable but are not actionable for
        normal bridge-sync receipt processing.
        """
        require_caller(ctx, caller, tool="record_shipped_event_disposition")
        disposition = disposition_type.strip()
        if disposition not in _SHIPPED_EVENT_DISPOSITION_TYPES:
            allowed = ", ".join(sorted(_SHIPPED_EVENT_DISPOSITION_TYPES))
            raise ToolError(f"disposition_type must be one of: {allowed}")
        clean_reason = reason.strip()
        if not clean_reason:
            raise ToolError("reason must not be empty")
        clean_policy_ref = policy_ref.strip() if policy_ref is not None else None
        clean_notes = notes.strip() if notes is not None else None

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

        cursor = await db.execute(
            "SELECT downstream_ref FROM shipped_sync_receipts WHERE activity_id = ?",
            (activity_id,),
        )
        receipt = await cursor.fetchone()
        if receipt is not None:
            raise ToolError(
                f"Activity entry {activity_id} already has a shipped_sync_receipts row"
            )

        await db.execute(
            """
            INSERT OR REPLACE INTO shipped_event_dispositions (
                activity_id, disposition_type, policy_ref, reason, decided_by, notes
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (activity_id, disposition, clean_policy_ref, clean_reason, caller, clean_notes),
        )
        await db.commit()

        detail = f"activity_id={activity_id} disposition_type={disposition}"
        log_audit(
            "record_shipped_event_disposition",
            caller,
            row["project_name"],
            ok=True,
            detail=detail,
        )
        logger.info(
            "recorded shipped-event disposition: id=%d disposition=%s by=%s",
            activity_id,
            disposition,
            caller,
        )
        return {
            "ok": True,
            "activity_id": activity_id,
            "project_name": row["project_name"],
            "disposition_type": disposition,
            "policy_ref": clean_policy_ref,
            "decided_by": caller,
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
        require_caller(ctx, caller, tool="confirm_shipped_sync")
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
