"""Snapshot tools: save_snapshot, get_latest_snapshot."""

import json
import logging
from datetime import date
from typing import Annotated, Any, cast

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from bridge_db import config
from bridge_db.db import (
    fts_text_for_snapshot,
    gc_fts_orphans,
    get_db,
    upsert_fts_entry,
)
from bridge_db.models import SNAPSHOT_SYSTEM_MAP, CallerID, SourceTrust, snapshot_ownership_error

logger = logging.getLogger("bridge_db.tools.snapshots")


def _snapshot_family(system: str, data: dict[str, Any]) -> str:
    if system != "codex":
        return "default"
    if {"infrastructure", "automation_digest", "active_projects"}.issubset(data):
        return "operating"
    if "consulted_node" in data:
        return "consulted_node"
    return "other"


async def _prune_snapshots(db: Any, *, system: str) -> None:
    cursor = await db.execute(
        """
        SELECT id, data
        FROM system_snapshots
        WHERE system = ?
        ORDER BY created_at DESC, id DESC
        """,
        (system,),
    )
    rows = await cursor.fetchall()

    seen_by_family: dict[str, int] = {}
    delete_ids: list[int] = []
    for row in rows:
        try:
            parsed_data = cast(object, json.loads(row["data"]))
        except json.JSONDecodeError:
            parsed_data = {}
        data: dict[str, Any] = {}
        if isinstance(parsed_data, dict):
            data = {str(key): value for key, value in cast(dict[object, Any], parsed_data).items()}
        family = _snapshot_family(system, data)
        seen_by_family[family] = seen_by_family.get(family, 0) + 1
        if seen_by_family[family] > config.SNAPSHOT_RETENTION_PER_SYSTEM:
            delete_ids.append(row["id"])

    if delete_ids:
        placeholders = ",".join("?" for _ in delete_ids)
        await db.execute(
            f"DELETE FROM system_snapshots WHERE id IN ({placeholders})",
            delete_ids,
        )


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def save_snapshot(
        caller: Annotated[CallerID, Field(description="Must be 'cc' or 'codex'")],
        data: Annotated[
            dict[str, Any],
            Field(
                description="JSON object with sub-section keys (active_projects, lessons, patterns, etc.)"
            ),
        ],
        snapshot_date: Annotated[
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
        """Save a system state snapshot. Auto-prunes to the 10 most recent per system."""
        system = SNAPSHOT_SYSTEM_MAP.get(caller)
        if system is None:
            logger.warning("snapshot ownership violation: caller=%s", caller)
            raise ToolError(snapshot_ownership_error(caller))

        db = get_db(ctx)
        snap_date = snapshot_date or str(date.today())

        snapshot_json = json.dumps(data)
        cursor = await db.execute(
            """
            INSERT INTO system_snapshots (system, snapshot_date, data, source_trust)
            VALUES (?, ?, ?, ?)
            """,
            (system, snap_date, snapshot_json, source_trust),
        )
        snapshot_id = cursor.lastrowid

        if snapshot_id is not None:
            await upsert_fts_entry(
                db, "snapshot", str(snapshot_id), fts_text_for_snapshot(snapshot_json)
            )

        await _prune_snapshots(db, system=system)
        await gc_fts_orphans(db, "snapshot")
        await db.commit()

        logger.info("snapshot saved: system=%s id=%d date=%s", system, snapshot_id, snap_date)
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "system": system,
            "snapshot_date": snap_date,
            "source_trust": source_trust,
        }

    @mcp.tool()
    async def get_latest_snapshot(
        system: Annotated[
            str, Field(description="Which system's snapshot to fetch: 'cc' or 'codex'")
        ],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Return the most recent snapshot for a system."""
        if system not in ("cc", "codex"):
            raise ToolError(f"Invalid system '{system}'. Must be 'cc' or 'codex'.")

        db = get_db(ctx)
        cursor = await db.execute(
            """
            SELECT id, system, snapshot_date, data, created_at
            FROM system_snapshots
            WHERE system = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (system,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ToolError(f"No snapshot found for system '{system}'")

        return {
            "id": row["id"],
            "system": row["system"],
            "snapshot_date": row["snapshot_date"],
            "data": json.loads(row["data"]),
            "created_at": row["created_at"],
        }
