"""Activity and shipped-event tools."""

import json
import logging
from typing import Annotated, Any, cast

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from bridge_db import clock, config
from bridge_db.audit import log_audit
from bridge_db.auth import (
    clamp_source_trust,
    get_principal,
    require_bound_caller,
    require_caller,
)
from bridge_db.db import (
    get_db,
    insert_activity_row,
    protected_tags_predicate,
    reindex_activity_fts,
)
from bridge_db.instruction_boundary import instruction_boundary
from bridge_db.invariants import sometimes
from bridge_db.models import (
    ACTIVITY_SOURCES,
    CallerID,
    SourceTrust,
    invalid_source_error,
)
from bridge_db.project_resolver import resolve as resolve_project

logger = logging.getLogger("bridge_db.tools.activity")

# The receipt-backed proof disposition: claims a durable downstream sync and
# requires downstream_system + downstream_ref (the old confirm_shipped_sync path).
_SYNCED_DISPOSITION = "synced"
# Non-receipt policy dispositions: record why an event is not receipt-backed
# without claiming sync (the old record_shipped_event_disposition path).
_POLICY_DISPOSITION_TYPES = {
    "unsynced_by_policy",
    "no_durable_target",
    "superseded_without_receipt",
    "declined_mapping",
}
_ALL_DISPOSITIONS = {_SYNCED_DISPOSITION, *_POLICY_DISPOSITION_TYPES}
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
    return (f"{prefix}created_at >= ?", [_created_at_since_threshold(since)])


def _activity_signal_sort_key(entry: dict[str, Any]) -> tuple[str, int]:
    created_at = entry["created_at"]
    activity_id = entry.get("latest_activity_id", entry.get("id", 0))
    return (created_at, int(activity_id or 0))


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


def _delivery_state(
    *,
    notion_sync_state: str,
    sync_disposition: str | None,
    downstream_system: str | None,
    downstream_ref: str | None,
) -> dict[str, Any]:
    """Expose receipt-backed delivery facts without inferring Git or deploy state."""
    dimensions = {
        "local_complete": "unknown",
        "committed": "unknown",
        "pushed": "unknown",
        "merged": "unknown",
        "default_branch_contains": "unknown",
        "deployed": "unknown",
        "production_readback_verified": "unknown",
        "downstream_sync_pending": notion_sync_state == "ready" and sync_disposition is None,
        "downstream_synced": sync_disposition == _SYNCED_DISPOSITION,
        "policy_dispositioned": sync_disposition in _POLICY_DISPOSITION_TYPES,
    }
    if sync_disposition == _SYNCED_DISPOSITION:
        state = "downstream_synced"
        evidence = {
            "downstream_system": downstream_system,
            "downstream_ref": downstream_ref,
        }
    elif sync_disposition in _POLICY_DISPOSITION_TYPES:
        state = "policy_dispositioned"
        evidence = {"disposition": sync_disposition}
    elif notion_sync_state == "ready":
        state = "downstream_sync_pending"
        evidence = None
    else:
        state = "unknown"
        evidence = None
    return {"state": state, "dimensions": dimensions, "evidence": evidence}


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
            else "bridge-db activity sync disposition"
        ),
    }


async def _export_bridge_markdown_after_processing(db: Any) -> None:
    """Keep the fallback bridge file current after shipped-event state changes."""
    try:
        from bridge_db.tools.export import (
            ContextExportSnapshot,
            build_markdown,
            export_bridge_file,
        )

        context_snapshot: list[ContextExportSnapshot] = []
        content = await build_markdown(db, context_snapshot=context_snapshot)
        await export_bridge_file(db, content, context_snapshot)
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
          eventual record_disposition (disposition='synced' for a downstream
          receipt, or a policy disposition) — unsynced SHIPPED rows nag in health
          until terminally resolved.
        - LEDGER: a durable operator-facing record for the next agent's catch-up.
          Attach when the operator says "log this to BridgeDB" or the entry should
          outlive the rolling window.
        - Anything else: free-form, searchable, prunable.
        """
        require_caller(ctx, caller, tool="log_activity")
        # INV-9 reachability: a row whose claimed source diverges from the
        # channel-bound principal is legal in auth 'warn' mode but must be
        # observable — attribution is the forensic record every other
        # invariant's diagnosis depends on.
        principal = get_principal(ctx)
        sometimes(
            "attribution_divergence", principal is not None and principal != caller
        )
        # Activity authorship is a durable cross-principal trust boundary, not
        # a rollout-compatible write. The channel identity must match even when
        # lower-risk tools still run with auth off or warn.
        require_bound_caller(ctx, caller, tool="log_activity")
        source_trust, source_trust_clamped = clamp_source_trust(
            source_trust, caller=caller, tool="log_activity", strict=True
        )
        db = get_db(ctx)
        # UTC via the clock seam: the old local-time default here both leaked
        # real time into DST runs and disagreed with snapshots' UTC date near
        # midnight.
        ts = timestamp or clock.now().date().isoformat()
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
            ORDER BY created_at DESC, id DESC
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

            if (row["created_at"], row["id"]) > (
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
            ORDER BY created_at DESC, id DESC
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
            ORDER BY created_at DESC, id DESC
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
            # "Unprocessed" excludes both PROCESSED-tagged rows and any row that
            # has reached a terminal sync disposition (synced or a policy value).
            # The PROCESSED-tag clause additionally holds a legacy receiptless-
            # processed row (PROCESSED tag, no disposition) out of the feed.
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM json_each(tags) WHERE value = 'PROCESSED')"
            )
            conditions.append("a.sync_disposition IS NULL")

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
                a.sync_disposition,
                a.sync_disposition_by,
                a.synced_at,
                a.sync_downstream_system,
                a.sync_downstream_ref,
                a.sync_policy_ref,
                a.sync_reason,
                a.sync_note
            FROM activity_log AS a
            {where}
            ORDER BY a.created_at DESC, a.id DESC
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
                    "delivery_state": _delivery_state(
                        notion_sync_state=str(notion_sync["state"]),
                        sync_disposition=r["sync_disposition"],
                        downstream_system=r["sync_downstream_system"],
                        downstream_ref=r["sync_downstream_ref"],
                    ),
                    "summary": r["summary"],
                    "branch": r["branch"],
                    "tags": json.loads(r["tags"]),
                    "created_at": r["created_at"],
                    "source_trust": r["source_trust"],
                    "instruction_boundary": instruction_boundary(r["source_trust"]),
                    "sync_receipt": (
                        {
                            "downstream_system": r["sync_downstream_system"],
                            "downstream_ref": r["sync_downstream_ref"],
                            "synced_by": r["sync_disposition_by"],
                            "synced_at": r["synced_at"],
                            "notes": r["sync_note"],
                        }
                        if r["sync_disposition"] == _SYNCED_DISPOSITION
                        else None
                    ),
                    "policy_disposition": (
                        {
                            "disposition_type": r["sync_disposition"],
                            "policy_ref": r["sync_policy_ref"],
                            "reason": r["sync_reason"],
                            "decided_by": r["sync_disposition_by"],
                            "decided_at": r["synced_at"],
                            "notes": r["sync_note"],
                        }
                        if r["sync_disposition"] in _POLICY_DISPOSITION_TYPES
                        else None
                    ),
                }
            )
        return events

    @mcp.tool()
    async def record_disposition(
        caller: Annotated[
            CallerID, Field(description="System recording the disposition")
        ],
        activity_id: Annotated[
            int, Field(description="SHIPPED activity entry to dispose")
        ],
        disposition: Annotated[
            str,
            Field(
                description=(
                    "Terminal sync disposition. 'synced' = receipt-backed "
                    "downstream proof (requires downstream_system + "
                    "downstream_ref, a bound caller matching the event source, "
                    "and adds PROCESSED). A policy value also requires the bound "
                    "event owner. "
                    "(unsynced_by_policy, no_durable_target, "
                    "superseded_without_receipt, declined_mapping) = non-receipt "
                    "decision (requires reason, does not claim sync)."
                )
            ),
        ],
        downstream_system: Annotated[
            str | None,
            Field(
                description="External system updated, e.g. 'notion' or 'github' (synced only)"
            ),
        ] = None,
        downstream_ref: Annotated[
            str | None,
            Field(
                description="Durable downstream reference, e.g. Notion page ID or URL (synced only)"
            ),
        ] = None,
        reason: Annotated[
            str | None,
            Field(
                description="Why this event is not receipt-ready (policy dispositions only)"
            ),
        ] = None,
        policy_ref: Annotated[
            str | None,
            Field(
                description="Optional durable policy or report reference (policy dispositions only)"
            ),
        ] = None,
        notes: Annotated[
            str | None,
            Field(description="Optional short sync note or operator evidence"),
        ] = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Record the terminal sync disposition for one SHIPPED activity event.

        Single verb replacing the former confirm_shipped_sync /
        record_shipped_event_disposition / mark_shipped_processed trio. A SHIPPED
        row reaches exactly one disposition, stored on the row itself (the v14
        ``sync_*`` columns):

        - ``disposition='synced'`` is the receipt-backed proof path: it REQUIRES
          ``downstream_system`` + ``downstream_ref`` and a channel-bound caller
          matching the event source, adds the ``PROCESSED`` tag, and records the
          downstream reference. Only this path claims a durable downstream sync.
        - A policy ``disposition`` (unsynced_by_policy / no_durable_target /
          superseded_without_receipt / declined_mapping) is a non-receipt
          decision: it REQUIRES a channel-bound caller matching the event source
          and a ``reason``, does NOT add ``PROCESSED``, and does not claim sync —
          it records why the event is not receipt-backed.

        Guarantees carried over from the trio: a SHIPPED row can never be marked
        resolved without either downstream proof or an explicit reasoned policy
        decision, and a row that already carries downstream proof ('synced')
        cannot be downgraded or replaced. An exact replay of its canonical
        receipt is a no-op. The legacy non-shipped
        PROCESSED-marking path (mark_shipped_processed) is retired; this tool is
        SHIPPED-only.
        """
        require_caller(ctx, caller, tool="record_disposition")
        choice = disposition.strip()
        if choice not in _ALL_DISPOSITIONS:
            allowed = ", ".join(sorted(_ALL_DISPOSITIONS))
            raise ToolError(f"disposition must be one of: {allowed}")

        is_synced = choice == _SYNCED_DISPOSITION
        clean_system = (
            downstream_system.strip() if downstream_system is not None else None
        )
        clean_ref = downstream_ref.strip() if downstream_ref is not None else None
        clean_reason = reason.strip() if reason is not None else None
        clean_policy_ref = policy_ref.strip() if policy_ref is not None else None
        clean_notes = notes.strip() if notes is not None else None

        def same_synced_receipt(receipt: Any) -> bool:
            return (
                receipt["sync_disposition"] == _SYNCED_DISPOSITION
                and receipt["sync_disposition_by"] == caller
                and receipt["sync_downstream_system"] == clean_system
                and receipt["sync_downstream_ref"] == clean_ref
                and receipt["sync_note"] == clean_notes
            )

        def idempotent_synced_result(project_name: str) -> dict[str, Any]:
            log_audit(
                "record_disposition",
                caller,
                project_name,
                ok=True,
                detail=(
                    f"activity_id={activity_id} disposition=synced "
                    "decision=idempotent_noop"
                ),
            )
            return {
                "ok": True,
                "activity_id": activity_id,
                "project_name": project_name,
                "disposition": choice,
                "decided_by": caller,
                "processed_added": False,
                "downstream_system": clean_system,
                "downstream_ref": clean_ref,
                "policy_ref": None,
            }

        if is_synced:
            if not clean_system:
                raise ToolError(
                    "downstream_system must not be empty for a 'synced' disposition"
                )
            if not clean_ref:
                raise ToolError(
                    "downstream_ref must not be empty for a 'synced' disposition"
                )
        elif not clean_reason:
            raise ToolError("reason must not be empty for a policy disposition")

        db = get_db(ctx)
        cursor = await db.execute(
            "SELECT source, tags, project_name, sync_disposition, "
            "sync_disposition_by, sync_downstream_system, sync_downstream_ref, "
            "sync_reason, sync_policy_ref, sync_note "
            "FROM activity_log WHERE id = ?",
            (activity_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ToolError(f"No activity entry found with id {activity_id}")

        current_tags: list[str] = json.loads(row["tags"])
        if "SHIPPED" not in current_tags:
            raise ToolError(f"Activity entry {activity_id} is not tagged SHIPPED")

        # Every disposition terminalizes the source owner's downstream
        # obligation. Bind both receipt proof and policy waivers to that owner,
        # even while lower-risk writes retain compatibility auth modes.
        require_bound_caller(ctx, caller, tool="record_disposition")
        if row["source"] != caller:
            log_audit(
                "record_disposition",
                caller,
                row["project_name"],
                ok=False,
                detail=(
                    f"activity_id={activity_id} disposition={choice} "
                    f"decision=refused reason=source_owner_mismatch "
                    f"event_source={row['source']}"
                ),
            )
            action = "synced receipt" if is_synced else "policy disposition"
            raise ToolError(
                f"Activity entry {activity_id} is owned by '{row['source']}'; "
                f"caller '{caller}' cannot record its {action}"
            )

        current_disposition = row["sync_disposition"]
        if is_synced and current_disposition == _SYNCED_DISPOSITION:
            if not same_synced_receipt(row):
                log_audit(
                    "record_disposition",
                    caller,
                    row["project_name"],
                    ok=False,
                    detail=(
                        f"activity_id={activity_id} disposition=synced "
                        "decision=refused reason=immutable_synced_receipt"
                    ),
                )
                raise ToolError(
                    f"Activity entry {activity_id} already has immutable downstream "
                    "sync proof; a different synced receipt cannot replace it"
                )

            return idempotent_synced_result(row["project_name"])

        if not is_synced and current_disposition == _SYNCED_DISPOSITION:
            raise ToolError(
                f"Activity entry {activity_id} already has downstream sync proof "
                "('synced'); it cannot be downgraded to a policy disposition"
            )

        # Transitioning a policy disposition to 'synced' would otherwise null the
        # prior policy reason/ref. Fold that reasoning into the note so the
        # audit trail (visible on get_shipped_events' sync_receipt.notes) is not
        # silently erased — the old two-table model kept both facts.
        note_value = clean_notes
        superseded: str | None = None
        if is_synced and current_disposition in _POLICY_DISPOSITION_TYPES:
            superseded = f"superseded prior disposition '{current_disposition}'"
            if row["sync_reason"]:
                superseded += f": {row['sync_reason']}"
            if row["sync_policy_ref"]:
                superseded += f" (policy_ref={row['sync_policy_ref']})"
            note_value = f"{clean_notes}; {superseded}" if clean_notes else superseded

        # Tags ARE indexed in content_index (see fts_text_for_activity), so the
        # PROCESSED add re-indexes its row via reindex_activity_fts.
        processed_added = False
        if is_synced and "PROCESSED" not in current_tags:
            current_tags.append("PROCESSED")
            await db.execute(
                "UPDATE activity_log SET tags = ? WHERE id = ?",
                (json.dumps(current_tags), activity_id),
            )
            await reindex_activity_fts(db, activity_id)
            processed_added = True

        # synced_at holds the receipt sync time for 'synced' and the decision
        # time for a policy disposition. SQL strftime is the seam-approved clock
        # source for DB-side timestamps (clock.py docstring, Phase 1).
        disposition_cursor = await db.execute(
            """
            UPDATE activity_log SET
                sync_disposition = ?,
                sync_disposition_by = ?,
                synced_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                sync_downstream_system = ?,
                sync_downstream_ref = ?,
                sync_policy_ref = ?,
                sync_reason = ?,
                sync_note = ?
            WHERE id = ?
              AND (? = 0 OR sync_disposition IS NULL OR sync_disposition != 'synced')
            """,
            (
                choice,
                caller,
                clean_system if is_synced else None,
                clean_ref if is_synced else None,
                None if is_synced else clean_policy_ref,
                None if is_synced else clean_reason,
                note_value,
                activity_id,
                int(is_synced),
            ),
        )
        if is_synced and disposition_cursor.rowcount != 1:
            # A competing first receipt won after this call's SELECT. Roll back
            # the staged tag/FTS work, then accept only an exact replay of that
            # now-canonical proof.
            await db.rollback()
            cursor = await db.execute(
                "SELECT sync_disposition, sync_disposition_by, "
                "sync_downstream_system, sync_downstream_ref, sync_note "
                "FROM activity_log WHERE id = ?",
                (activity_id,),
            )
            latest = await cursor.fetchone()
            if latest is not None and same_synced_receipt(latest):
                return idempotent_synced_result(row["project_name"])
            log_audit(
                "record_disposition",
                caller,
                row["project_name"],
                ok=False,
                detail=(
                    f"activity_id={activity_id} disposition=synced "
                    "decision=refused reason=immutable_synced_receipt_race"
                ),
            )
            raise ToolError(
                f"Activity entry {activity_id} already has immutable downstream "
                "sync proof; a different synced receipt cannot replace it"
            )
        await db.commit()

        if is_synced:
            await _export_bridge_markdown_after_processing(db)

        detail = f"activity_id={activity_id} disposition={choice}"
        if is_synced:
            detail += f" downstream={clean_system}:{clean_ref}"
        if superseded is not None:
            detail += f" {superseded}"
        log_audit(
            "record_disposition", caller, row["project_name"], ok=True, detail=detail
        )
        logger.info(
            "recorded disposition: id=%d disposition=%s by=%s",
            activity_id,
            choice,
            caller,
        )
        return {
            "ok": True,
            "activity_id": activity_id,
            "project_name": row["project_name"],
            "disposition": choice,
            "decided_by": caller,
            "processed_added": processed_added,
            "downstream_system": clean_system if is_synced else None,
            "downstream_ref": clean_ref if is_synced else None,
            "policy_ref": None if is_synced else clean_policy_ref,
        }
