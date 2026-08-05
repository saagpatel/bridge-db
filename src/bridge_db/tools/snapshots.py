"""Snapshot tools: save_snapshot, get_latest_snapshot."""

import json
import logging
from typing import Annotated, Any, Literal, cast

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from bridge_db import clock, config
from bridge_db.audit import log_audit
from bridge_db.auth import clamp_source_trust, require_bound_caller, require_caller
from bridge_db.capacity import encode_bounded_json
from bridge_db.db import (
    fts_text_for_snapshot,
    gc_fts_orphans,
    get_db,
    upsert_fts_entry,
)
from bridge_db.instruction_boundary import instruction_boundary
from bridge_db.models import (
    SNAPSHOT_SYSTEM_MAP,
    CallerID,
    SourceTrust,
    snapshot_ownership_error,
)

logger = logging.getLogger("bridge_db.tools.snapshots")

SnapshotRetentionPolicy = Literal["preserve_existing", "prune_oldest"]


def _utc_snapshot_date() -> str:
    return clock.now().date().isoformat()


def _snapshot_family(system: str, data: dict[str, Any]) -> str:
    if system != "codex":
        return "default"
    if {"infrastructure", "automation_digest", "active_projects"}.issubset(data):
        return "operating"
    if "consulted_node" in data:
        return "consulted_node"
    return "other"


def _decode_snapshot_data(raw_data: str) -> dict[str, Any]:
    try:
        parsed_data = cast(object, json.loads(raw_data))
    except json.JSONDecodeError:
        parsed_data = {}
    if not isinstance(parsed_data, dict):
        return {}
    return {
        str(key): value for key, value in cast(dict[object, Any], parsed_data).items()
    }


async def _snapshot_family_count(db: Any, *, system: str, family: str) -> int:
    cursor = await db.execute(
        "SELECT data FROM system_snapshots WHERE system = ?",
        (system,),
    )
    rows = await cursor.fetchall()
    return sum(
        1
        for row in rows
        if _snapshot_family(system, _decode_snapshot_data(row["data"])) == family
    )


async def _prune_snapshots(db: Any, *, system: str) -> list[tuple[int, str]]:
    """Delete over-retention snapshots; return (id, family) of each pruned row.

    Returned so save_snapshot can emit a prune audit line — snapshots are
    instruction-bearing rows, and BD-INV-1's philosophy is that no prune is
    silent (activity prunes have audited since the durable-ledger work).
    """
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
    pruned: list[tuple[int, str]] = []
    for row in rows:
        data = _decode_snapshot_data(row["data"])
        family = _snapshot_family(system, data)
        seen_by_family[family] = seen_by_family.get(family, 0) + 1
        if seen_by_family[family] > config.SNAPSHOT_RETENTION_PER_SYSTEM:
            pruned.append((row["id"], family))

    if pruned:
        placeholders = ",".join("?" for _ in pruned)
        await db.execute(
            f"DELETE FROM system_snapshots WHERE id IN ({placeholders})",
            [row_id for row_id, _ in pruned],
        )
    return pruned


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
            str | None,
            Field(
                description="Date in YYYY-MM-DD format; defaults to the UTC calendar date"
            ),
        ] = None,
        source_trust: Annotated[
            SourceTrust,
            Field(
                description="Provenance: 'operator' (operator-asserted), 'agent' "
                "(Claude-authored, default), or 'ingested' (external)"
            ),
        ] = "agent",
        retention_policy: Annotated[
            SnapshotRetentionPolicy,
            Field(
                description=(
                    "'preserve_existing' (default) atomically refuses with "
                    "reason_code='snapshot.retention_would_prune' when the target "
                    "family is full and never performs retention deletion. "
                    "'prune_oldest' explicitly enables the legacy auto-prune path."
                )
            ),
        ] = "preserve_existing",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Save a system state snapshot.

        The default preserve_existing policy runs the family capacity check and
        insert under one SQLite writer transaction. A full family returns a
        stable no-write result; an accepted write never prunes snapshots or FTS
        rows. Callers must explicitly select prune_oldest to enable the legacy
        10-per-family auto-prune behavior.
        """
        require_caller(ctx, caller, tool="save_snapshot")
        require_bound_caller(ctx, caller, tool="save_snapshot")
        source_trust, source_trust_clamped = clamp_source_trust(
            source_trust, caller=caller, tool="save_snapshot", strict=True
        )
        system = SNAPSHOT_SYSTEM_MAP.get(caller)
        if system is None:
            logger.warning("snapshot ownership violation: caller=%s", caller)
            raise ToolError(snapshot_ownership_error(caller))
        if retention_policy not in ("preserve_existing", "prune_oldest"):
            raise ToolError(
                "snapshot.invalid_retention_policy: expected "
                "'preserve_existing' or 'prune_oldest'"
            )

        snapshot_json = encode_bounded_json(
            data,
            maximum_bytes=config.SNAPSHOT_JSON_MAX_BYTES,
            maximum_depth=config.SNAPSHOT_JSON_MAX_DEPTH,
            maximum_nodes=config.SNAPSHOT_JSON_MAX_NODES,
            code_prefix="snapshot",
        )
        db = get_db(ctx)
        snap_date = snapshot_date or _utc_snapshot_date()
        family = _snapshot_family(system, data)
        retention_limit = config.SNAPSHOT_RETENTION_PER_SYSTEM
        preserve_existing = retention_policy == "preserve_existing"

        try:
            if preserve_existing:
                await db.execute("BEGIN IMMEDIATE")
                family_count = await _snapshot_family_count(
                    db, system=system, family=family
                )
                if family_count >= retention_limit:
                    await db.rollback()
                    # BD-INV-1's philosophy is that no prune is silent. A refusal
                    # to write is the same class of event: it changes what the
                    # ledger will contain. Without this line a saturated family
                    # is invisible in the audit log, so a caller that does not
                    # inspect `ok` cannot be distinguished after the fact from a
                    # caller that never wrote at all.
                    log_audit(
                        "save_snapshot.refused",
                        caller,
                        None,
                        ok=False,
                        detail=(
                            f"reason=snapshot.retention_would_prune system={system} "
                            f"family={family} retained={family_count} "
                            f"limit={retention_limit} date={snap_date}"
                        ),
                    )
                    logger.warning(
                        "snapshot refused: system=%s family=%s retained=%d limit=%d "
                        "(no write performed; caller must check ok)",
                        system,
                        family,
                        family_count,
                        retention_limit,
                    )
                    return {
                        "ok": False,
                        "reason_code": "snapshot.retention_would_prune",
                        "mutation_performed": False,
                        "snapshot_id": None,
                        "system": system,
                        "snapshot_family": family,
                        "snapshot_date": snap_date,
                        "source_trust": source_trust,
                        "source_trust_clamped": source_trust_clamped,
                        "retention_policy": retention_policy,
                        "retention_limit": retention_limit,
                        "retained_count": family_count,
                        "would_prune_count": family_count + 1 - retention_limit,
                        "pruned_count": 0,
                    }

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
                    db,
                    "snapshot",
                    str(snapshot_id),
                    fts_text_for_snapshot(snapshot_json),
                )

            if preserve_existing:
                pruned: list[tuple[int, str]] = []
            else:
                pruned = await _prune_snapshots(db, system=system)
                await gc_fts_orphans(db, "snapshot")
            await db.commit()
        except Exception:
            await db.rollback()
            raise

        if pruned:
            pruned_ids = [row_id for row_id, _ in pruned]
            pruned_families = sorted({family for _, family in pruned})
            log_audit(
                "save_snapshot.prune",
                caller,
                None,
                ok=True,
                detail=(
                    f"pruned={len(pruned)} ids={pruned_ids} "
                    f"families={pruned_families} system={system}"
                ),
            )
        logger.info(
            "snapshot saved: system=%s id=%d date=%s", system, snapshot_id, snap_date
        )
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "system": system,
            "snapshot_date": snap_date,
            "source_trust": source_trust,
            "source_trust_clamped": source_trust_clamped,
            "snapshot_family": family,
            "mutation_performed": True,
            "retention_policy": retention_policy,
            "retention_limit": retention_limit,
            "pruned_count": len(pruned),
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
            SELECT id, system, snapshot_date, data, created_at, source_trust
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
            "source_trust": row["source_trust"],
            "instruction_boundary": instruction_boundary(row["source_trust"]),
        }
