"""Health and status tools: raw readiness plus compact operator summary."""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from bridge_db import config
from bridge_db.auth import auth_mode, load_principals
from bridge_db.db import SCHEMA_VERSION, collect_fts_index_metrics, get_db
from bridge_db.migration import BRIDGE_SECTION_HEADINGS, SECTION_MAP, extract_sections

logger = logging.getLogger("bridge_db.tools.health")

_ROW_COUNT_TABLES = (
    "context_sections",
    "activity_log",
    "pending_handoffs",
    "system_snapshots",
    "cost_records",
    "shipped_sync_receipts",
    "shipped_event_dispositions",
)
_ACTIVITY_SOURCES = ("cc", "codex", "claude_ai", "notion_os", "personal_ops")
_SNAPSHOT_SYSTEMS = ("cc", "codex")
_TRUST_LEVELS = ("operator", "agent", "ingested")
_TRUST_TABLES = ("context_sections", "activity_log", "system_snapshots", "pending_handoffs")
SNAPSHOT_STALE_AFTER_HOURS = 48.0
ACTIVITY_QUIET_AFTER_HOURS = 72.0
PENDING_HANDOFF_STALE_AFTER_HOURS = 168.0
ACTIVE_HANDOFF_STALE_AFTER_HOURS = 72.0


def _utc_now() -> datetime:
    return datetime.now(UTC)


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


def _read_bridge_claude_ai_sections() -> dict[str, str] | None:
    """Parse the exported bridge file's claude_ai-owned sections the same way
    ``sync_from_file`` reads them. Returns ``{section_name: body}`` (only non-empty
    sections), or ``None`` when the file is absent/unreadable."""
    path = config.BRIDGE_FILE_PATH
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    headings = extract_sections(content, allowed_headings=BRIDGE_SECTION_HEADINGS)
    sections: dict[str, str] = {}
    for heading, section_name in SECTION_MAP.items():
        body = headings.get(heading, "").strip()
        if body:
            sections[section_name] = body
    return sections


async def collect_claude_ai_section_drift(db: Any) -> dict[str, Any]:
    """Detect when the exported bridge file's claude_ai sections disagree with the
    DB projection (F8 clobber-race monitor).

    A mismatch means either unsynced inbound Claude.ai file edits at risk of being
    overwritten by the next ``export_bridge_markdown``, or a stale export — both
    actionable (run ``sync_from_file`` or re-export). Advisory only: this is a
    latent-risk signal and is intentionally NOT folded into ``ok`` so it can't flap
    during the normal window between a DB write and the next export.
    """
    file_sections = _read_bridge_claude_ai_sections()
    if file_sections is None:
        return {"checked": False, "in_sync": True, "drifted_sections": []}

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
    return {"checked": True, "in_sync": not drifted, "drifted_sections": drifted}


async def collect_health_metrics(db: Any) -> dict[str, Any]:
    """Collect raw bridge health metrics from the current DB plus filesystem state."""
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

    cursor = await db.execute(
        "SELECT COUNT(*) FROM activity_log AS activity "
        "WHERE EXISTS (SELECT 1 FROM json_each(activity.tags) WHERE value = 'SHIPPED') "
        "AND NOT EXISTS (SELECT 1 FROM json_each(activity.tags) WHERE value = 'PROCESSED') "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM shipped_event_dispositions AS disposition "
        "  WHERE disposition.activity_id = activity.id"
        ")"
    )
    actionable_row = await cursor.fetchone()
    actionable_unprocessed_shipped_count: int = actionable_row[0] if actionable_row else 0

    cursor = await db.execute(
        "SELECT COUNT(*) FROM activity_log AS activity "
        "WHERE EXISTS (SELECT 1 FROM json_each(activity.tags) WHERE value = 'SHIPPED') "
        "AND EXISTS (SELECT 1 FROM json_each(activity.tags) WHERE value = 'PROCESSED') "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM shipped_sync_receipts AS receipt "
        "  WHERE receipt.activity_id = activity.id"
        ")"
    )
    receiptless_row = await cursor.fetchone()
    processed_shipped_without_receipt_count: int = receiptless_row[0] if receiptless_row else 0

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
        bridge_file_age_seconds = datetime.now(UTC).timestamp() - mtime

    fts_index = await collect_fts_index_metrics(db)
    claude_ai_section_drift = await collect_claude_ai_section_drift(db)
    source_trust_breakdown = await _source_trust_breakdown(db)

    # WAL size and claude_ai section drift are soft signals — do not fold into `ok`.
    ok = db_exists and schema_version == SCHEMA_VERSION and bridge_file_exists and fts_index["ok"]

    return {
        "ok": ok,
        "db_path": str(db_path),
        "db_exists": db_exists,
        "schema_version": schema_version,
        "row_counts": row_counts,
        "bridge_file_path": str(bridge_path),
        "bridge_file_exists": bridge_file_exists,
        "bridge_file_age_seconds": bridge_file_age_seconds,
        "unprocessed_shipped_count": unprocessed_shipped_count,
        "actionable_unprocessed_shipped_count": actionable_unprocessed_shipped_count,
        "processed_shipped_without_receipt_count": processed_shipped_without_receipt_count,
        "wal_size_bytes": wal_size_bytes,
        "wal_warning": wal_warning,
        "fts_index": fts_index,
        "claude_ai_section_drift": claude_ai_section_drift,
        "source_trust_breakdown": source_trust_breakdown,
        "auth": {
            "mode": auth_mode(),
            "principals_file_exists": config.PRINCIPALS_PATH.exists(),
            "principals_enrolled": len(load_principals(config.PRINCIPALS_PATH)),
        },
    }


def _snapshot_next_action(owner: str, state: str) -> str:
    if state in {"stale", "missing", "unknown"}:
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
        if row is None:
            state = "missing"
        elif age is None:
            state = "unknown"
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
            "next_action": _snapshot_next_action(system, state),
        }
    return snapshots


async def _activity_source_freshness(db: Any, now: datetime) -> dict[str, dict[str, Any]]:
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
        next_action = "confirm_shipped_sync_or_record_disposition"
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
                "action": "confirm_shipped_sync_or_record_disposition",
                "owner": "operator",
                "reason": "Actionable SHIPPED rows need receipt-backed sync or disposition.",
            }
        )
    for owner in _SNAPSHOT_SYSTEMS:
        snapshot = snapshots[owner]
        if snapshot["state"] in {"stale", "missing", "unknown"}:
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
    if any(snapshot["state"] == "stale" for snapshot in snapshots.values()):
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


async def collect_status_summary(db: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Collect a compact operator-facing status summary."""
    health = await collect_health_metrics(db)
    fixed_now = now or _utc_now()

    cursor = await db.execute("SELECT COUNT(*) FROM pending_handoffs WHERE status = 'pending'")
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
            "WHERE source = ? ORDER BY timestamp DESC, created_at DESC, id DESC LIMIT 1",
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

    return {
        "ok": health["ok"],
        "overall": "healthy" if health["ok"] else "degraded",
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
            "actionable_unprocessed_shipped": health["actionable_unprocessed_shipped_count"],
            "dispositioned_unprocessed_shipped": max(
                health["unprocessed_shipped_count"]
                - health["actionable_unprocessed_shipped_count"],
                0,
            ),
            "processed_shipped_without_receipt": health["processed_shipped_without_receipt_count"],
            "fts_missing": health["fts_index"]["missing"],
            "fts_orphaned": health["fts_index"]["orphaned"],
            "claude_ai_unsynced_sections": len(
                health["claude_ai_section_drift"]["drifted_sections"]
            ),
        },
        "fts_index": health["fts_index"],
        "latest_snapshots": latest_snapshots,
        "latest_activity": latest_activity,
        "latest_activity_json": json.dumps(latest_activity, sort_keys=True),
        "freshness": await _collect_freshness_block(db, health, fixed_now),
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
