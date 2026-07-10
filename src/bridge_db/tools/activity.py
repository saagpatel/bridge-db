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
    protected_tags_predicate,
    reindex_activity_fts,
)
from bridge_db.instruction_boundary import instruction_boundary
from bridge_db.models import (
    ACTIVITY_SOURCES,
    CallerID,
    SourceTrust,
    invalid_source_error,
)
from bridge_db.project_resolver import resolve as resolve_project

logger = logging.getLogger("bridge_db.tools.activity")

_SHIPPED_EVENT_DISPOSITION_TYPES = {
    "unsynced_by_policy",
    "no_durable_target",
    "superseded_without_receipt",
    "declined_mapping",
}
_SESSION_BOUNDARY_TAG = "session-boundary"
_LIFECYCLE_ACTIVITY_SQL = """
(
    source = 'cc'
    AND (
        summary LIKE 'CC session ended%'
        OR EXISTS (SELECT 1 FROM json_each(tags) WHERE value = 'session-boundary')
    )
)
"""


def _decode_tags(raw: str) -> list[str]:
    parsed: object = json.loads(raw)
    return (
        [str(tag) for tag in cast(list[object], parsed)]
        if isinstance(parsed, list)
        else []
    )


def _activity_payload(row: Any, *, kind: str | None = None) -> dict[str, Any]:
    payload = {
        "id": row["id"],
        "source": row["source"],
        "timestamp": row["timestamp"],
        "project_name": row["project_name"],
        "summary": row["summary"],
        "branch": row["branch"],
        "tags": _decode_tags(row["tags"]),
        "created_at": row["created_at"],
        "canonical_key": row["canonical_key"],
        "source_trust": row["source_trust"],
        "instruction_boundary": instruction_boundary(row["source_trust"]),
    }
    if kind is not None:
        payload["kind"] = kind
    return payload


def _activity_time_bucket(timestamp: str) -> str:
    if len(timestamp) >= 13 and timestamp[10:11] == "T":
        return timestamp[:13]
    return timestamp[:10]


def _created_at_since_threshold(since: str) -> str:
    if len(since) == 10 and since[4] == "-" and since[7] == "-":
        return f"{since}T00:00:00Z"
    return since


def _activity_since_condition(
    since: str, *, table_alias: str | None = None
) -> tuple[str, list[str]]:
    prefix = f"{table_alias}." if table_alias else ""
    return (
        f"({prefix}timestamp >= ? OR {prefix}created_at >= ?)",
        [since, _created_at_since_threshold(since)],
    )


def _activity_signal_sort_key(entry: dict[str, Any]) -> tuple[str, str, int]:
    timestamp = (
        entry["last_ts"]
        if entry["kind"] == "lifecycle_aggregate"
        else entry["timestamp"]
    )
    created_at = entry["created_at"]
    activity_id = entry.get("latest_activity_id", entry.get("id", 0))
    return (timestamp, created_at, int(activity_id or 0))


def _summarize_trust(counts: dict[str, int]) -> str:
    nonzero = [trust for trust, count in counts.items() if count > 0]
    return nonzero[0] if len(nonzero) == 1 else "mixed"


def _select_activity_signal_entries(
    entries: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    entries.sort(key=_activity_signal_sort_key, reverse=True)
    selected = entries[:limit]
    if not selected or any(entry["kind"] == "activity" for entry in selected):
        return selected

    newest_substantive = next(
        (entry for entry in entries if entry["kind"] == "activity"), None
    )
    if newest_substantive is None:
        return selected

    selected = [*selected[: limit - 1], newest_substantive]
    selected.sort(key=_activity_signal_sort_key, reverse=True)
    return selected[:limit]


def _normalize_policy_key(value: str) -> str:
    return value.strip().lower()


_META_POLICY_CACHE: tuple[float, dict[str, Any]] | None = None


def _load_meta_policy_root() -> dict[str, Any]:
    global _META_POLICY_CACHE
    try:
        mtime = config.META_SHIPPED_EVENTS_PATH.stat().st_mtime
    except OSError:
        return {}
    if _META_POLICY_CACHE is not None and _META_POLICY_CACHE[0] == mtime:
        return _META_POLICY_CACHE[1]
    try:
        raw = json.loads(config.META_SHIPPED_EVENTS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    root = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
    _META_POLICY_CACHE = (mtime, root)  # pyright: ignore[reportConstantRedefinition]
    return root


def _load_meta_shipped_event_policy(project_name: str) -> dict[str, Any] | None:
    """Return meta-event policy for SHIPPED rows that intentionally skip Notion."""
    policy_root = _load_meta_policy_root()
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
        from bridge_db.tools.export import (
            build_markdown,
            record_context_export_state,
            write_bridge_file,
        )

        content = await build_markdown(db)
        write_bridge_file(content)
        await record_context_export_state(db)
        await db.commit()
        logger.info("auto-export triggered after shipped-event processing")
    except Exception:
        logger.warning(
            "auto-export after shipped-event processing failed", exc_info=True
        )


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
        project_name: Annotated[
            str, Field(description="Project name, e.g. 'bridge-db'")
        ],
        summary: Annotated[
            str, Field(description="One-line description of what was done")
        ],
        branch: Annotated[
            str | None, Field(description="Git branch name, if applicable")
        ] = None,
        tags: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Optional tags (indexed for recall). SHIPPED = durable ship "
                    "event, syncs to the Notion Build Log and is retention-"
                    "protected. LEDGER = durable catch-up entry ('what happened "
                    "/ what it does / what it points to'), retention-protected, "
                    "pinned by get_activity_signal. Both match case-"
                    "insensitively. Other tags are free-form and searchable but "
                    "NOT retention-protected."
                )
            ),
        ] = None,
        timestamp: Annotated[
            str | None, Field(description="Logical activity date or timestamp")
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
        """Log a session activity entry.

        Retention (BD-INV-1): unprotected entries auto-prune to the most recent
        50 per source; entries tagged SHIPPED or LEDGER (case-insensitive) are
        NEVER pruned. Case-insensitivity applies only to retention protection;
        the shipped-sync feed and health nags match exact-case SHIPPED. Every
        prune emits a `log_activity.prune` audit line.

        Tag conventions (tags are indexed in content_index, so they are recall-able):
        - SHIPPED: a feature/artifact reached a durable, usable state. Requires an
          eventual confirm_shipped_sync receipt or record_shipped_event_disposition —
          unsynced SHIPPED rows nag in health until terminally resolved.
        - LEDGER: a durable operator-facing record for the next agent's catch-up.
          Attach when the operator says "log this to BridgeDB" or the entry should
          outlive the rolling window.
        - Anything else: free-form, searchable, prunable.
        """
        require_caller(ctx, caller, tool="log_activity")
        source_trust, source_trust_clamped = clamp_source_trust(
            source_trust, caller=caller, tool="log_activity"
        )
        db = get_db(ctx)
        ts = timestamp or str(date.today())
        resolution = resolve_project(project_name)
        insert_result = await insert_activity_row(
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
        if insert_result.pruned_rows:
            pruned_ids = [row_id for row_id, _ in insert_result.pruned_rows]
            pruned_tag_set: set[str] = set()
            for _, raw in insert_result.pruned_rows:
                try:
                    pruned_tag_set.update(json.loads(raw))
                except json.JSONDecodeError:
                    logger.warning("could not decode tags for pruned activity row")
            pruned_tags = sorted(pruned_tag_set)
            log_audit(
                "log_activity.prune",
                caller,
                project_name,
                ok=True,
                detail=(
                    f"pruned={len(pruned_ids)} ids_head={pruned_ids[:20]} "
                    f"tags={pruned_tags} source={caller}"
                ),
            )
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
                "log_activity: unmatched project_name %r (no canonical key)",
                project_name,
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
        limit: Annotated[
            int, Field(description="Max entries to return", ge=1, le=200)
        ] = 20,
        since: Annotated[
            str | None,
            Field(description="Only entries on or after this YYYY-MM-DD date"),
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
            condition, since_params = _activity_since_condition(since)
            conditions.append(condition)
            params.extend(since_params)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)

        cursor = await db.execute(
            f"""
            SELECT id, source, timestamp, project_name, summary, branch, tags, created_at, canonical_key, source_trust
            FROM activity_log
            {where}
            ORDER BY timestamp DESC, created_at DESC, id DESC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()
        return [_activity_payload(r) for r in rows]

    @mcp.tool()
    async def get_activity_signal(
        source: Annotated[
            str | None,
            Field(
                description=(
                    "Filter by source: 'cc', 'codex', 'claude_ai', "
                    "'notion_os', or 'personal_ops'. Omit for all."
                )
            ),
        ] = None,
        limit: Annotated[
            int, Field(description="Max signal entries to return", ge=1, le=200)
        ] = 20,
        since: Annotated[
            str | None,
            Field(description="Only entries on or after this YYYY-MM-DD date"),
        ] = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> list[dict[str, Any]]:
        """Return operator-facing activity with lifecycle session-boundary rows compressed."""
        db = get_db(ctx)

        conditions: list[str] = []
        params: list[Any] = []

        if source is not None:
            if source not in ACTIVITY_SOURCES:
                raise ToolError(invalid_source_error(source))
            conditions.append("source = ?")
            params.append(source)
        if since is not None:
            condition, since_params = _activity_since_condition(since)
            conditions.append(condition)
            params.extend(since_params)

        lifecycle_where = (
            "WHERE " + " AND ".join([*conditions, _LIFECYCLE_ACTIVITY_SQL])
            if conditions
            else f"WHERE {_LIFECYCLE_ACTIVITY_SQL}"
        )
        substantive_where = (
            "WHERE " + " AND ".join([*conditions, f"NOT {_LIFECYCLE_ACTIVITY_SQL}"])
            if conditions
            else f"WHERE NOT {_LIFECYCLE_ACTIVITY_SQL}"
        )

        lifecycle_cursor = await db.execute(
            f"""
            SELECT
                source,
                project_name,
                CASE
                    WHEN summary LIKE 'CC session ended%' THEN 'CC session ended'
                    ELSE summary
                END AS summary_family,
                timestamp,
                created_at,
                id,
                canonical_key,
                source_trust
            FROM activity_log
            {lifecycle_where}
            """,
            params,
        )
        lifecycle_rows = await lifecycle_cursor.fetchall()

        aggregates: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for row in lifecycle_rows:
            summary_family = row["summary_family"]
            time_bucket = _activity_time_bucket(row["timestamp"])
            key = (row["source"], row["project_name"], summary_family, time_bucket)
            current = aggregates.get(key)
            if current is None:
                aggregates[key] = {
                    "kind": "lifecycle_aggregate",
                    "source": row["source"],
                    "project_name": row["project_name"],
                    "summary": summary_family,
                    "summary_family": summary_family,
                    "time_bucket": time_bucket,
                    "count": 1,
                    "first_ts": row["timestamp"],
                    "last_ts": row["timestamp"],
                    "latest_activity_id": row["id"],
                    "created_at": row["created_at"],
                    "canonical_key": row["canonical_key"],
                    "tags": [_SESSION_BOUNDARY_TAG],
                    "source_trust_summary": {row["source_trust"]: 1},
                }
                continue

            if (row["timestamp"], row["created_at"], row["id"]) > (
                current["last_ts"],
                current["created_at"],
                current["latest_activity_id"],
            ):
                current["latest_activity_id"] = row["id"]
                current["created_at"] = row["created_at"]
                current["canonical_key"] = row["canonical_key"]
            current["count"] += 1
            current["first_ts"] = min(current["first_ts"], row["timestamp"])
            current["last_ts"] = max(current["last_ts"], row["timestamp"])
            summary = current["source_trust_summary"]
            summary[row["source_trust"]] = summary.get(row["source_trust"], 0) + 1

        for aggregate in aggregates.values():
            trust = _summarize_trust(aggregate["source_trust_summary"])
            aggregate["source_trust"] = trust
            aggregate["instruction_boundary"] = instruction_boundary(trust)

        # Over-fetch compensates for ledger-id dedupe so protected rows in the
        # recency window can't shrink the substantive result below limit.
        substantive_params = [*params, limit + config.LEDGER_SIGNAL_LIMIT]
        substantive_cursor = await db.execute(
            f"""
            SELECT id, source, timestamp, project_name, summary, branch, tags, created_at, canonical_key, source_trust
            FROM activity_log
            {substantive_where}
            ORDER BY timestamp DESC, created_at DESC, id DESC
            LIMIT ?
            """,
            substantive_params,
        )
        substantive_rows = await substantive_cursor.fetchall()

        protected_sql, protected_params = protected_tags_predicate()
        ledger_conditions = [
            *conditions,
            protected_sql,
            f"NOT {_LIFECYCLE_ACTIVITY_SQL}",
        ]
        ledger_where = "WHERE " + " AND ".join(ledger_conditions)
        ledger_cursor = await db.execute(
            f"""
            SELECT id, source, timestamp, project_name, summary, branch, tags, created_at, canonical_key, source_trust
            FROM activity_log
            {ledger_where}
            ORDER BY timestamp DESC, created_at DESC, id DESC
            LIMIT ?
            """,
            [*params, *protected_params, config.LEDGER_SIGNAL_LIMIT],
        )
        ledger_rows = await ledger_cursor.fetchall()
        ledger_ids = {r["id"] for r in ledger_rows}
        ledger_entries = [_activity_payload(r, kind="ledger") for r in ledger_rows]

        entries = [
            *aggregates.values(),
            *[
                _activity_payload(r, kind="activity")
                for r in substantive_rows
                if r["id"] not in ledger_ids
            ],
        ]
        return [*ledger_entries, *_select_activity_signal_entries(entries, limit)]

    @mcp.tool()
    async def get_shipped_events(
        since: Annotated[
            str | None, Field(description="Only shipped events on or after YYYY-MM-DD")
        ] = None,
        unprocessed_only: Annotated[
            bool, Field(description="If true, exclude events already marked PROCESSED")
        ] = False,
        limit: Annotated[
            int,
            Field(
                description="Max shipped events to return, newest first", ge=1, le=1000
            ),
        ] = 200,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> list[dict[str, Any]]:
        """Return activity entries tagged SHIPPED, for Codex bridge-sync to sync to Notion."""
        db = get_db(ctx)

        conditions = ["EXISTS (SELECT 1 FROM json_each(tags) WHERE value = 'SHIPPED')"]
        params: list[Any] = []

        if since:
            condition, since_params = _activity_since_condition(since, table_alias="a")
            conditions.append(condition)
            params.extend(since_params)
        if unprocessed_only:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM json_each(tags) WHERE value = 'PROCESSED')"
            )
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM shipped_event_dispositions AS d2 "
                "WHERE d2.activity_id = a.id)"
            )

        where = "WHERE " + " AND ".join(conditions)
        params.append(limit)
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
                a.source_trust,
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
            LIMIT ?
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
                    "source_trust": r["source_trust"],
                    "instruction_boundary": instruction_boundary(r["source_trust"]),
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

        Guard (F7): if any passed id is tagged SHIPPED, this tool refuses the
        whole batch before updating rows. SHIPPED rows must either get
        receipt-backed processing through ``confirm_shipped_sync`` or an explicit
        non-receipt policy decision through ``record_shipped_event_disposition``.
        """
        if not activity_ids:
            raise ToolError("activity_ids must not be empty")

        db = get_db(ctx)
        updated = 0
        updated_ids: list[int] = []
        missing_ids: list[int] = []
        blocked_shipped_ids: list[int] = []
        tags_by_activity_id: dict[int, list[str]] = {}

        # Tags ARE indexed in content_index (see fts_text_for_activity), so each
        # tag mutation below re-indexes its row via reindex_activity_fts.
        for activity_id in activity_ids:
            cursor = await db.execute(
                "SELECT tags FROM activity_log WHERE id = ?", (activity_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                logger.warning(
                    "mark_shipped_processed: id %d not found, skipping", activity_id
                )
                missing_ids.append(activity_id)
                continue
            current_tags: list[str] = json.loads(row["tags"])
            tags_by_activity_id[activity_id] = current_tags
            if "SHIPPED" in current_tags:
                blocked_shipped_ids.append(activity_id)

        if blocked_shipped_ids:
            logger.warning(
                "mark_shipped_processed: refused SHIPPED ids %s; use confirm_shipped_sync "
                "for receipt-backed shipped artifacts or record_shipped_event_disposition "
                "for explicit non-receipt decisions",
                blocked_shipped_ids,
            )
            log_audit(
                "mark_shipped_processed",
                None,
                None,
                ok=False,
                detail=(
                    f"activity_ids={activity_ids} missing_ids={missing_ids} "
                    f"blocked_shipped_ids={blocked_shipped_ids}"
                ),
            )
            raise ToolError(
                "mark_shipped_processed refuses SHIPPED activity ids "
                f"{blocked_shipped_ids}; use confirm_shipped_sync with downstream proof "
                "or record_shipped_event_disposition for an explicit non-receipt decision"
            )

        for activity_id, current_tags in tags_by_activity_id.items():
            if "PROCESSED" not in current_tags:
                current_tags.append("PROCESSED")
                await db.execute(
                    "UPDATE activity_log SET tags = ? WHERE id = ?",
                    (json.dumps(current_tags), activity_id),
                )
                await reindex_activity_fts(db, activity_id)
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
        logger.info(
            "mark_shipped_processed: updated %d/%d entries", updated, len(activity_ids)
        )
        return {
            "ok": True,
            "updated": updated,
            "total": len(activity_ids),
            "activity_ids": activity_ids,
            "updated_ids": updated_ids,
            "missing_ids": missing_ids,
            "shipped_bypass_ids": [],
        }

    @mcp.tool()
    async def record_shipped_event_disposition(
        caller: Annotated[
            CallerID, Field(description="System recording the disposition")
        ],
        activity_id: Annotated[
            int, Field(description="SHIPPED activity entry to classify")
        ],
        disposition_type: Annotated[
            str,
            Field(
                description=(
                    "One of: unsynced_by_policy, no_durable_target, "
                    "superseded_without_receipt, declined_mapping"
                )
            ),
        ],
        reason: Annotated[
            str, Field(description="Why this event is not receipt-ready")
        ],
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
            (
                activity_id,
                disposition,
                clean_policy_ref,
                clean_reason,
                caller,
                clean_notes,
            ),
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
        caller: Annotated[
            CallerID, Field(description="System confirming downstream sync")
        ],
        activity_id: Annotated[
            int, Field(description="SHIPPED activity entry that was synced")
        ],
        downstream_system: Annotated[
            str,
            Field(description="External system updated, e.g. 'notion' or 'github'"),
        ],
        downstream_ref: Annotated[
            str,
            Field(
                description="Durable downstream reference, e.g. Notion page ID or URL"
            ),
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
            await reindex_activity_fts(db, activity_id)
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
        log_audit(
            "confirm_shipped_sync", caller, row["project_name"], ok=True, detail=detail
        )
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
