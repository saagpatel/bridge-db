"""Health check tool: returns DB and bridge file status."""

import logging
from datetime import UTC, datetime
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from bridge_db import config
from bridge_db.db import SCHEMA_VERSION, get_db

logger = logging.getLogger("bridge_db.tools.health")


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def health(
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Return DB and bridge file health metrics. No caller required — read-only diagnostic."""
        db = get_db(ctx)

        cursor = await db.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        schema_version: int = row[0] if row else 0

        row_counts: dict[str, int] = {}
        for table in ("activity_log", "pending_handoffs", "cost_records"):
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

        bridge_path = config.BRIDGE_FILE_PATH
        bridge_file_exists = bridge_path.exists()
        bridge_file_age_seconds: float | None = None
        if bridge_file_exists:
            mtime = bridge_path.stat().st_mtime
            bridge_file_age_seconds = datetime.now(UTC).timestamp() - mtime

        ok = db_exists and schema_version == SCHEMA_VERSION

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
        }
