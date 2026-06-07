"""Health and status tools: raw readiness plus compact operator summary."""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from bridge_db import config
from bridge_db.db import SCHEMA_VERSION, collect_fts_index_metrics, get_db
from bridge_db.migration import SECTION_MAP, extract_sections

logger = logging.getLogger("bridge_db.tools.health")

_ROW_COUNT_TABLES = (
    "context_sections",
    "activity_log",
    "pending_handoffs",
    "system_snapshots",
    "cost_records",
    "shipped_sync_receipts",
)
_ACTIVITY_SOURCES = ("cc", "codex", "claude_ai", "notion_os", "personal_ops")
_SNAPSHOT_SYSTEMS = ("cc", "codex")


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
    headings = extract_sections(content)
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
        "processed_shipped_without_receipt_count": processed_shipped_without_receipt_count,
        "wal_size_bytes": wal_size_bytes,
        "wal_warning": wal_warning,
        "fts_index": fts_index,
        "claude_ai_section_drift": claude_ai_section_drift,
    }


async def collect_status_summary(db: Any) -> dict[str, Any]:
    """Collect a compact operator-facing status summary."""
    health = await collect_health_metrics(db)

    cursor = await db.execute("SELECT COUNT(*) FROM pending_handoffs WHERE status = 'pending'")
    pending_handoffs_row = await cursor.fetchone()
    pending_handoffs = pending_handoffs_row[0] if pending_handoffs_row else 0

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
        "signals": {
            "pending_handoffs": pending_handoffs,
            "unprocessed_shipped": health["unprocessed_shipped_count"],
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
