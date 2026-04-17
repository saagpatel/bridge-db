"""Health and status tools: raw readiness plus compact operator summary."""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from bridge_db import config
from bridge_db.db import SCHEMA_VERSION, get_db

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

    # WAL size is a soft signal — do not fold it into `ok`.
    ok = db_exists and schema_version == SCHEMA_VERSION and bridge_file_exists

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
        "wal_size_bytes": wal_size_bytes,
        "wal_warning": wal_warning,
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
            "WHERE system = ? ORDER BY created_at DESC LIMIT 1",
            (system,),
        )
        snapshot_row = await cursor.fetchone()
        latest_snapshots[system] = snapshot_row[0] if snapshot_row else "none"

    latest_activity: dict[str, str] = {}
    for source in _ACTIVITY_SOURCES:
        cursor = await db.execute(
            "SELECT timestamp, project_name FROM activity_log "
            "WHERE source = ? ORDER BY timestamp DESC, created_at DESC LIMIT 1",
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
        },
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
