"""Health and status tools: raw readiness plus compact operator summary."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from bridge_db import clock, config
from bridge_db.auth import auth_mode, load_principals
from bridge_db.db import (
    SCHEMA_VERSION,
    collect_fts_index_metrics,
    get_db,
    protected_tags_predicate,
)
from bridge_db.evidence import (
    evidence_file_inventory,
    legacy_raw_query_inventory,
    migration_backup_inventory,
)
from bridge_db.tools.context import parse_owned_sections

logger = logging.getLogger("bridge_db.tools.health")

_ROW_COUNT_TABLES = (
    "context_sections",
    "activity_log",
    "pending_handoffs",
    "system_snapshots",
    "cost_records",
)
_ACTIVITY_SOURCES = ("cc", "codex", "claude_ai", "notion_os", "personal_ops")
_SNAPSHOT_SYSTEMS = ("cc", "codex")
_TRUST_LEVELS = ("operator", "agent", "ingested")
_TRUST_TABLES = (
    "context_sections",
    "activity_log",
    "system_snapshots",
    "pending_handoffs",
)
SNAPSHOT_STALE_AFTER_HOURS = 48.0
ACTIVITY_QUIET_AFTER_HOURS = 72.0
PENDING_HANDOFF_STALE_AFTER_HOURS = 168.0
ACTIVE_HANDOFF_STALE_AFTER_HOURS = 72.0


def _utc_now() -> datetime:
    return clock.now()


def _parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_hours(value: str | None, now: datetime) -> float | None:
    parsed = _parse_utc_timestamp(value)
    if parsed is None:
        return None
    fixed_now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    age = (fixed_now.astimezone(UTC) - parsed).total_seconds() / 3600
    return round(max(age, 0.0), 1)


async def _source_trust_breakdown(db: Any) -> dict[str, dict[str, int]]:
    """Per-table provenance distribution: {table: {operator, agent, ingested}}.

    Default-zero-filled so every level is present even when a table has no rows
    at that trust. Reads source rows directly; content_index is not involved.
    """
    breakdown: dict[str, dict[str, int]] = {}
    for table in _TRUST_TABLES:
        cursor = await db.execute(
            f"SELECT source_trust, COUNT(*) AS n FROM {table} GROUP BY source_trust"  # noqa: S608
        )
        counts = {level: 0 for level in _TRUST_LEVELS}
        for row in await cursor.fetchall():
            if row["source_trust"] in counts:
                counts[row["source_trust"]] = row["n"]
        breakdown[table] = counts
    return breakdown


def _read_bridge_claude_ai_sections() -> tuple[str, dict[str, str] | None]:
    """Parse the exported bridge file's claude_ai-owned sections the same way
    ``sync_from_file`` reads them. Returns ``{section_name: body}`` (only non-empty
    sections) together with ``readable``, ``missing``, or ``unreadable``."""
    path = config.BRIDGE_FILE_PATH
    if not path.exists():
        return "missing", None
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return "unreadable", None
    try:
        sections = parse_owned_sections(content)
    except (ToolError, ValueError):
        return "unreadable", None
    return "readable", {name: body for name, body in sections.items() if body}


async def collect_claude_ai_section_drift(db: Any) -> dict[str, Any]:
    """Detect when the exported bridge file's claude_ai sections disagree with the
    DB projection (F8 clobber-race monitor).

    A mismatch means either unsynced inbound Claude.ai file edits or a stale
    export. Missing and unreadable states remain distinct and never claim sync.
    """
    read_state, file_sections = _read_bridge_claude_ai_sections()
    if read_state != "readable" or file_sections is None:
        return {
            "checked": False,
            "in_sync": None,
            "state": read_state,
            "reason": "not_found" if read_state == "missing" else "read_error",
            "drifted_sections": [],
        }

    cursor = await db.execute(
        "SELECT section_name, content FROM context_sections WHERE owner = 'claude_ai'"
    )
    rows = await cursor.fetchall()
    db_sections = {r["section_name"]: (r["content"] or "").strip() for r in rows}

    drifted = sorted(
        name
        for name in set(db_sections) | set(file_sections)
        if db_sections.get(name, "") != file_sections.get(name, "")
    )
    return {
        "checked": True,
        "in_sync": not drifted,
        "state": "drift" if drifted else "current",
        "reason": None,
        "drifted_sections": drifted,
    }


async def collect_health_metrics(db: Any) -> dict[str, Any]:
    """Collect raw bridge health metrics from the current DB plus filesystem state.

    Note (v14 boundary): `receipt_orphan_count` and `disposition_orphan_count`
    keep their names but changed meaning. Pre-v14 they counted FK-orphaned
    shipped_sync_receipts / shipped_event_dispositions rows; those child tables
    were collapsed into activity_log `sync_*` columns, so the metrics now measure
    disposition MALFORMATION on the row (a 'synced' row missing downstream proof;
    a disposition on a non-SHIPPED row or a policy disposition missing its
    reason). Both must always read 0 and are the compensating detection control
    for the field requirements the old NOT NULL columns enforced.
    """
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    schema_version: int = row[0] if row else 0

    row_counts: dict[str, int] = {}
    for table in _ROW_COUNT_TABLES:
        cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
        count_row = await cursor.fetchone()
        row_counts[table] = count_row[0] if count_row else 0

    cursor = await db.execute(
        "SELECT COUNT(*) FROM activity_log "
        "WHERE EXISTS (SELECT 1 FROM json_each(tags) WHERE value = 'SHIPPED') "
        "AND NOT EXISTS (SELECT 1 FROM json_each(tags) WHERE value = 'PROCESSED')"
    )
    unprocessed_row = await cursor.fetchone()
    unprocessed_shipped_count: int = unprocessed_row[0] if unprocessed_row else 0

    # Actionable = SHIPPED, not PROCESSED, and no terminal sync disposition yet
    # (sync_disposition IS NULL). A policy-dispositioned row is resolved and
    # drops out here just as it did under the old dispositions subquery.
    cursor = await db.execute(
        "SELECT COUNT(*) FROM activity_log AS activity "
        "WHERE EXISTS (SELECT 1 FROM json_each(activity.tags) WHERE value = 'SHIPPED') "
        "AND NOT EXISTS (SELECT 1 FROM json_each(activity.tags) WHERE value = 'PROCESSED') "
        "AND activity.sync_disposition IS NULL"
    )
    actionable_row = await cursor.fetchone()
    actionable_unprocessed_shipped_count: int = (
        actionable_row[0] if actionable_row else 0
    )

    # Receiptless-processed = SHIPPED, PROCESSED-tagged, but no 'synced' proof
    # disposition. 'synced' is the column-model receipt; anything else (NULL or a
    # policy value) counts as lacking downstream proof.
    cursor = await db.execute(
        "SELECT COUNT(*) FROM activity_log AS activity "
        "WHERE EXISTS (SELECT 1 FROM json_each(activity.tags) WHERE value = 'SHIPPED') "
        "AND EXISTS (SELECT 1 FROM json_each(activity.tags) WHERE value = 'PROCESSED') "
        "AND (activity.sync_disposition IS NULL OR activity.sync_disposition <> 'synced')"
    )
    receiptless_row = await cursor.fetchone()
    processed_shipped_without_receipt_count: int = (
        receiptless_row[0] if receiptless_row else 0
    )

    protected_sql, protected_params = protected_tags_predicate()
    cursor = await db.execute(
        f"SELECT COUNT(*) FROM activity_log WHERE {protected_sql}",  # noqa: S608
        protected_params,
    )
    ledger_row = await cursor.fetchone()
    ledger_protected_count: int = ledger_row[0] if ledger_row else 0

    # Rows carrying a 'synced' proof disposition — the column-model analog of the
    # old shipped_sync_receipts row count (surfaced in --status).
    cursor = await db.execute(
        "SELECT COUNT(*) FROM activity_log WHERE sync_disposition = 'synced'"
    )
    synced_row = await cursor.fetchone()
    synced_shipped_count: int = synced_row[0] if synced_row else 0

    # BD-INV-1 integrity checks, reframed for the v14 column model. Pre-v14 these
    # counted FK-orphans (a receipt/disposition whose activity row was pruned);
    # that is structurally impossible now — the state IS the row — so the same
    # two metric names now measure disposition MALFORMATION and MUST stay zero.
    # They are the compensating detection control for the field requirements the
    # old NOT NULL child-table columns enforced but the nullable sync_* columns
    # cannot (see db.py _V14_SYNC_COLUMNS):
    #   receipt_orphan_count      — a 'synced' row missing downstream_system/ref
    #                               (a receipt without its proof).
    #   disposition_orphan_count  — a disposition on a non-SHIPPED row, OR a
    #                               policy disposition missing its reason.
    cursor = await db.execute(
        "SELECT COUNT(*) FROM activity_log "
        "WHERE sync_disposition = 'synced' "
        "AND (sync_downstream_system IS NULL OR sync_downstream_ref IS NULL)"
    )
    receipt_orphan_row = await cursor.fetchone()
    receipt_orphan_count: int = receipt_orphan_row[0] if receipt_orphan_row else 0

    cursor = await db.execute(
        "SELECT COUNT(*) FROM activity_log AS activity "
        "WHERE ("
        "  activity.sync_disposition IS NOT NULL "
        "  AND NOT EXISTS (SELECT 1 FROM json_each(activity.tags) WHERE value = 'SHIPPED')"
        ") OR ("
        "  activity.sync_disposition IN ("
        "    'unsynced_by_policy', 'no_durable_target', "
        "    'superseded_without_receipt', 'declined_mapping'"
        "  ) AND (activity.sync_reason IS NULL OR trim(activity.sync_reason) = '')"
        ")"
    )
    disposition_orphan_row = await cursor.fetchone()
    disposition_orphan_count: int = (
        disposition_orphan_row[0] if disposition_orphan_row else 0
    )

    # Open write-conflict receipts. Soft signal like WAL size — a receipt is
    # evidence the conflict machinery worked, not that the bridge is broken,
    # so it must not fold into `ok`. Surfacing the count here is what turns
    # the receipts ledger into a feedback loop instead of a table only
    # deliberate get_write_conflicts callers ever see.
    cursor = await db.execute(
        "SELECT COUNT(*), MIN(created_at) FROM write_conflicts WHERE status = 'open'"
    )
    open_conflict_row = await cursor.fetchone()
    open_write_conflicts: int = open_conflict_row[0] if open_conflict_row else 0
    oldest_open_conflict_age_hours: float | None = _age_hours(
        open_conflict_row[1] if open_conflict_row else None, _utc_now()
    )

    db_path = config.DB_PATH
    db_exists = db_path.exists()

    wal_path = db_path.with_name(db_path.name + "-wal")
    wal_size_bytes = wal_path.stat().st_size if wal_path.exists() else 0
    wal_warning = wal_size_bytes > config.WAL_SIZE_WARN_BYTES

    bridge_path = config.BRIDGE_FILE_PATH
    bridge_file_exists = bridge_path.exists()
    bridge_file_age_seconds: float | None = None
    if bridge_file_exists:
        mtime = bridge_path.stat().st_mtime
        bridge_file_age_seconds = _utc_now().timestamp() - mtime

    fts_index = await collect_fts_index_metrics(db)
    claude_ai_section_drift = await collect_claude_ai_section_drift(db)
    source_trust_breakdown = await _source_trust_breakdown(db)
    cursor = await db.execute(
        "SELECT 1 FROM bridge_file_export_state WHERE singleton = 1"
    )
    bridge_file_export_tracked = await cursor.fetchone() is not None

    from bridge_db.tools.recall import RECALL_LOG_PATH

    audit_inventory = evidence_file_inventory(
        config.AUDIT_LOG_PATH, rotate_bytes=config.AUDIT_LOG_ROTATE_BYTES
    )
    recall_inventory = evidence_file_inventory(
        RECALL_LOG_PATH, rotate_bytes=config.RECALL_LOG_ROTATE_BYTES
    )
    failure_inventory = evidence_file_inventory(
        config.AUDIT_FAILURE_LOG_PATH,
        rotate_bytes=config.AUDIT_LOG_ROTATE_BYTES,
    )
    audit_degraded = failure_inventory["total_bytes"] > 0
    database_list_cursor = await db.execute("PRAGMA database_list")
    database_rows = await database_list_cursor.fetchall()
    open_main_path = next(
        (
            row[2]
            for row in database_rows
            if row[1] == "main" and isinstance(row[2], str) and row[2]
        ),
        str(config.DB_PATH),
    )
    backup_inventory = migration_backup_inventory(Path(open_main_path))
    backup_integrity_ok = (
        backup_inventory["count"] == backup_inventory["verified_count"]
    )
    evidence_lifecycle = {
        "audit": audit_inventory,
        "recall": {
            **recall_inventory,
            "historical_raw_queries": legacy_raw_query_inventory(RECALL_LOG_PATH),
        },
        "audit_failures": {
            **failure_inventory,
            "state": "degraded" if audit_degraded else "clear",
        },
        "migration_backups": backup_inventory,
        "audit_degraded": audit_degraded,
        "backup_integrity_ok": backup_integrity_ok,
        "destructive_actions": "approval_required",
    }

    # Keep structural storage health separate from cross-store projection
    # integrity. Generic readiness is green only when both are proven current.
    storage_ok = (
        db_exists
        and schema_version == SCHEMA_VERSION
        and bridge_file_exists
        and fts_index["ok"]
        and not audit_degraded
        and backup_integrity_ok
    )
    projection_health = claude_ai_section_drift["state"]
    if projection_health == "current" and not bridge_file_export_tracked:
        projection_health = "untracked"
    ok = storage_ok and projection_health == "current"

    return {
        "ok": ok,
        "storage_ok": storage_ok,
        "projection_health": projection_health,
        "db_path": str(db_path),
        "db_exists": db_exists,
        "schema_version": schema_version,
        "row_counts": row_counts,
        "bridge_file_path": str(bridge_path),
        "bridge_file_exists": bridge_file_exists,
        "bridge_file_age_seconds": bridge_file_age_seconds,
        "bridge_file_export_tracked": bridge_file_export_tracked,
        "unprocessed_shipped_count": unprocessed_shipped_count,
        "actionable_unprocessed_shipped_count": actionable_unprocessed_shipped_count,
        "processed_shipped_without_receipt_count": processed_shipped_without_receipt_count,
        "ledger_protected_count": ledger_protected_count,
        "synced_shipped_count": synced_shipped_count,
        "receipt_orphan_count": receipt_orphan_count,
        "disposition_orphan_count": disposition_orphan_count,
        "open_write_conflicts": open_write_conflicts,
        "oldest_open_conflict_age_hours": oldest_open_conflict_age_hours,
        "wal_size_bytes": wal_size_bytes,
        "wal_warning": wal_warning,
        "fts_index": fts_index,
        "claude_ai_section_drift": claude_ai_section_drift,
        "source_trust_breakdown": source_trust_breakdown,
        "evidence_lifecycle": evidence_lifecycle,
        "auth": {
            "mode": auth_mode(),
            "principals_file_exists": config.PRINCIPALS_PATH.exists(),
            "principals_enrolled": len(load_principals(config.PRINCIPALS_PATH)),
        },
    }


def _snapshot_next_action(owner: str, state: str) -> str:
    if state in {"stale", "superseded", "missing", "unknown"}:
        return f"{owner}_refresh_snapshot"
    return "none"


async def _snapshot_freshness(db: Any, now: datetime) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for system in _SNAPSHOT_SYSTEMS:
        cursor = await db.execute(
            "SELECT snapshot_date, created_at FROM system_snapshots "
            "WHERE system = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (system,),
        )
        row = await cursor.fetchone()
        latest_snapshot_date = row["snapshot_date"] if row else "none"
        latest_created_at = row["created_at"] if row else "none"
        age = _age_hours(row["created_at"], now) if row else None
        latest_activity_cursor = await db.execute(
            "SELECT id, created_at FROM activity_log WHERE source = ? "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM json_each(activity_log.tags) "
            "  WHERE lower(value) = 'session-boundary'"
            ") "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (system,),
        )
        latest_activity = await latest_activity_cursor.fetchone()
        superseding_activity_id = None
        activity_created_at = (
            _parse_utc_timestamp(latest_activity["created_at"])
            if latest_activity is not None
            else None
        )
        snapshot_created_at = (
            _parse_utc_timestamp(row["created_at"]) if row is not None else None
        )
        if row is None:
            state = "missing"
        elif age is None:
            state = "unknown"
        elif (
            latest_activity is not None
            and activity_created_at is not None
            and snapshot_created_at is not None
            and activity_created_at > snapshot_created_at
        ):
            state = "superseded"
            superseding_activity_id = latest_activity["id"]
        elif age > SNAPSHOT_STALE_AFTER_HOURS:
            state = "stale"
        else:
            state = "fresh"
        snapshots[system] = {
            "state": state,
            "owner": system,
            "latest_snapshot_date": latest_snapshot_date,
            "latest_created_at": latest_created_at,
            "age_hours": age,
            "superseding_activity_id": superseding_activity_id,
            "next_action": _snapshot_next_action(system, state),
        }
    return snapshots


async def _activity_source_freshness(
    db: Any, now: datetime
) -> dict[str, dict[str, Any]]:
    activity_sources: dict[str, dict[str, Any]] = {}
    for source in _ACTIVITY_SOURCES:
        cursor = await db.execute(
            "SELECT created_at FROM activity_log "
            "WHERE source = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (source,),
        )
        row = await cursor.fetchone()
        latest = row["created_at"] if row else "none"
        age = _age_hours(row["created_at"], now) if row else None
        if row is None:
            state = "missing"
        elif age is None:
            state = "unknown"
        elif age > ACTIVITY_QUIET_AFTER_HOURS:
            state = "quiet"
        else:
            state = "fresh"
        activity_sources[source] = {
            "state": state,
            "latest": latest,
            "age_hours": age,
        }
    return activity_sources


async def _handoff_freshness(db: Any, now: datetime) -> dict[str, Any]:
    cursor = await db.execute(
        "SELECT status, dispatched_at, picked_up_at FROM pending_handoffs "
        "WHERE status IN ('pending', 'active')"
    )
    pending_ages: list[float] = []
    active_ages: list[float] = []
    pending_count = 0
    active_count = 0
    stale_pending_count = 0
    stale_active_count = 0
    unknown_pending_count = 0
    unknown_active_count = 0
    for row in await cursor.fetchall():
        status = row["status"]
        if status == "pending":
            pending_count += 1
            age = _age_hours(row["dispatched_at"], now)
            if age is None:
                unknown_pending_count += 1
            else:
                pending_ages.append(age)
                if age > PENDING_HANDOFF_STALE_AFTER_HOURS:
                    stale_pending_count += 1
        elif status == "active":
            active_count += 1
            age = _age_hours(row["picked_up_at"], now)
            if age is None:
                unknown_active_count += 1
            else:
                active_ages.append(age)
                if age > ACTIVE_HANDOFF_STALE_AFTER_HOURS:
                    stale_active_count += 1
    return {
        "pending_count": pending_count,
        "stale_pending_count": stale_pending_count,
        "active_count": active_count,
        "stale_active_count": stale_active_count,
        "oldest_pending_age_hours": max(pending_ages) if pending_ages else None,
        "oldest_active_age_hours": max(active_ages) if active_ages else None,
        "unknown_pending_count": unknown_pending_count,
        "unknown_active_count": unknown_active_count,
    }


def _shipped_event_freshness(health: dict[str, Any]) -> dict[str, Any]:
    unprocessed = int(health["unprocessed_shipped_count"])
    actionable_unprocessed = int(health["actionable_unprocessed_shipped_count"])
    dispositioned_unprocessed = max(unprocessed - actionable_unprocessed, 0)
    processed_without_receipt = int(health["processed_shipped_without_receipt_count"])
    if processed_without_receipt > 0:
        next_action = "inspect_receiptless_processed"
    elif actionable_unprocessed > 0:
        next_action = "record_disposition"
    else:
        next_action = "none"
    return {
        "unprocessed": unprocessed,
        "actionable_unprocessed": actionable_unprocessed,
        "dispositioned_unprocessed": dispositioned_unprocessed,
        "processed_without_receipt": processed_without_receipt,
        "next_action": next_action,
    }


def _freshness_next_actions(
    health: dict[str, Any],
    snapshots: dict[str, dict[str, Any]],
    handoffs: dict[str, Any],
    shipped_events: dict[str, Any],
) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    if not health["ok"]:
        fts_index = health["fts_index"]
        if fts_index["missing"] or fts_index["orphaned"]:
            actions.append(
                {
                    "action": "repair_fts_index",
                    "owner": "operator",
                    "reason": "FTS index drift is degrading bridge health.",
                }
            )
        else:
            actions.append(
                {
                    "action": "inspect_bridge_health",
                    "owner": "operator",
                    "reason": "Bridge health is degraded.",
                }
            )
    if shipped_events["processed_without_receipt"] > 0:
        actions.append(
            {
                "action": "inspect_receiptless_processed",
                "owner": "operator",
                "reason": "Processed SHIPPED rows lack receipt proof.",
            }
        )
    if shipped_events["actionable_unprocessed"] > 0:
        actions.append(
            {
                "action": "record_disposition",
                "owner": "operator",
                "reason": "Actionable SHIPPED rows need receipt-backed sync or disposition.",
            }
        )
    for owner in _SNAPSHOT_SYSTEMS:
        snapshot = snapshots[owner]
        if snapshot["state"] in {"stale", "superseded", "missing", "unknown"}:
            actions.append(
                {
                    "action": snapshot["next_action"],
                    "owner": owner,
                    "reason": f"{owner} snapshot freshness is {snapshot['state']}.",
                }
            )
    if (
        handoffs["stale_pending_count"]
        or handoffs["stale_active_count"]
        or handoffs["unknown_pending_count"]
        or handoffs["unknown_active_count"]
    ):
        actions.append(
            {
                "action": "review_stale_handoff",
                "owner": "operator",
                "reason": "Pending or active handoffs exceeded freshness thresholds or have unknown age.",
            }
        )
    return actions[:5]


def _freshness_overall(
    health: dict[str, Any],
    snapshots: dict[str, dict[str, Any]],
    activity_sources: dict[str, dict[str, Any]],
    handoffs: dict[str, Any],
    shipped_events: dict[str, Any],
) -> str:
    if any(
        snapshot["state"] in {"stale", "superseded"}
        for snapshot in snapshots.values()
    ):
        return "stale"
    if handoffs["stale_pending_count"] or handoffs["stale_active_count"]:
        return "stale"
    if not health["ok"]:
        return "attention"
    if shipped_events["next_action"] != "none":
        return "attention"
    if any(snapshot["state"] == "unknown" for snapshot in snapshots.values()):
        return "unknown"
    if any(source["state"] == "unknown" for source in activity_sources.values()):
        return "unknown"
    if handoffs["unknown_pending_count"] or handoffs["unknown_active_count"]:
        return "unknown"
    if any(snapshot["state"] == "missing" for snapshot in snapshots.values()):
        return "attention"
    return "fresh"


async def _collect_freshness_block(
    db: Any, health: dict[str, Any], now: datetime
) -> dict[str, Any]:
    snapshots = await _snapshot_freshness(db, now)
    activity_sources = await _activity_source_freshness(db, now)
    handoffs = await _handoff_freshness(db, now)
    shipped_events = _shipped_event_freshness(health)
    next_actions = _freshness_next_actions(health, snapshots, handoffs, shipped_events)
    return {
        "thresholds_hours": {
            "snapshot_stale_after": SNAPSHOT_STALE_AFTER_HOURS,
            "activity_quiet_after": ACTIVITY_QUIET_AFTER_HOURS,
            "pending_handoff_stale_after": PENDING_HANDOFF_STALE_AFTER_HOURS,
            "active_handoff_stale_after": ACTIVE_HANDOFF_STALE_AFTER_HOURS,
        },
        "snapshots": snapshots,
        "activity_sources": activity_sources,
        "handoffs": handoffs,
        "shipped_events": shipped_events,
        "overall": _freshness_overall(
            health, snapshots, activity_sources, handoffs, shipped_events
        ),
        "next_actions": next_actions,
    }


async def collect_status_summary(
    db: Any, *, now: datetime | None = None
) -> dict[str, Any]:
    """Collect a compact operator-facing status summary."""
    health = await collect_health_metrics(db)
    fixed_now = now or _utc_now()

    cursor = await db.execute(
        "SELECT COUNT(*) FROM pending_handoffs WHERE status = 'pending'"
    )
    pending_handoffs_row = await cursor.fetchone()
    pending_handoffs = pending_handoffs_row[0] if pending_handoffs_row else 0

    cursor = await db.execute(
        "SELECT source_trust, COUNT(*) AS n FROM pending_handoffs "
        "WHERE status = 'pending' GROUP BY source_trust"
    )
    pending_handoffs_by_trust = {level: 0 for level in _TRUST_LEVELS}
    for trust_row in await cursor.fetchall():
        if trust_row["source_trust"] in pending_handoffs_by_trust:
            pending_handoffs_by_trust[trust_row["source_trust"]] = trust_row["n"]

    latest_snapshots: dict[str, str] = {}
    for system in _SNAPSHOT_SYSTEMS:
        cursor = await db.execute(
            "SELECT snapshot_date FROM system_snapshots "
            "WHERE system = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (system,),
        )
        snapshot_row = await cursor.fetchone()
        latest_snapshots[system] = snapshot_row[0] if snapshot_row else "none"

    latest_activity: dict[str, str] = {}
    for source in _ACTIVITY_SOURCES:
        cursor = await db.execute(
            "SELECT timestamp, project_name FROM activity_log "
            "WHERE source = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (source,),
        )
        activity_row = await cursor.fetchone()
        if activity_row:
            latest_activity[source] = (
                f"{activity_row['timestamp']} ({activity_row['project_name']})"
            )
        else:
            latest_activity[source] = "none"

    bridge_age_seconds = health["bridge_file_age_seconds"]
    bridge_age_human = "missing"
    if bridge_age_seconds is not None:
        bridge_age_human = f"{bridge_age_seconds / 3600:.1f}h old"

    freshness = await _collect_freshness_block(db, health, fixed_now)
    storage_health = "healthy" if health["storage_ok"] else "degraded"
    projection_health = health["projection_health"]
    overall = "healthy" if health["ok"] else "degraded"
    operating_state = freshness["overall"]
    return {
        "ok": health["ok"],
        "overall": overall,
        "storage_health": storage_health,
        "projection_health": projection_health,
        "operating_state": operating_state,
        "db": {
            "path": health["db_path"],
            "exists": health["db_exists"],
            "schema_version": health["schema_version"],
            "expected_schema_version": SCHEMA_VERSION,
        },
        "bridge_file": {
            "path": health["bridge_file_path"],
            "exists": health["bridge_file_exists"],
            "age_seconds": bridge_age_seconds,
            "age_human": bridge_age_human,
        },
        "row_counts": health["row_counts"],
        "source_trust_breakdown": health["source_trust_breakdown"],
        "pending_handoffs_by_trust": pending_handoffs_by_trust,
        "signals": {
            "pending_handoffs": pending_handoffs,
            "unprocessed_shipped": health["unprocessed_shipped_count"],
            "actionable_unprocessed_shipped": health[
                "actionable_unprocessed_shipped_count"
            ],
            "dispositioned_unprocessed_shipped": max(
                health["unprocessed_shipped_count"]
                - health["actionable_unprocessed_shipped_count"],
                0,
            ),
            "processed_shipped_without_receipt": health[
                "processed_shipped_without_receipt_count"
            ],
            "synced_shipped": health["synced_shipped_count"],
            "fts_missing": health["fts_index"]["missing"],
            "fts_orphaned": health["fts_index"]["orphaned"],
            "claude_ai_unsynced_sections": len(
                health["claude_ai_section_drift"]["drifted_sections"]
            ),
            "open_write_conflicts": health["open_write_conflicts"],
            "audit_degraded": health["evidence_lifecycle"]["audit_degraded"],
            "migration_backup_integrity_ok": health["evidence_lifecycle"][
                "backup_integrity_ok"
            ],
        },
        "evidence_lifecycle": health["evidence_lifecycle"],
        "fts_index": health["fts_index"],
        "latest_snapshots": latest_snapshots,
        "latest_activity": latest_activity,
        "latest_activity_json": json.dumps(latest_activity, sort_keys=True),
        "freshness": freshness,
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def health(
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Return DB and bridge file health metrics. No caller required — read-only diagnostic."""
        db = get_db(ctx)
        return await collect_health_metrics(db)

    @mcp.tool()
    async def status(
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Return a compact operator-facing bridge summary."""
        db = get_db(ctx)
        return await collect_status_summary(db)
