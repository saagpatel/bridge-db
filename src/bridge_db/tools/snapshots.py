"""Snapshot tools: save_snapshot, get_latest_snapshot."""

import json
import logging
from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from bridge_db import clock
from bridge_db.audit import log_audit
from bridge_db.auth import clamp_source_trust, require_bound_caller, require_caller
from bridge_db.db import get_db
from bridge_db.instruction_boundary import instruction_boundary
from bridge_db.models import (
    SNAPSHOT_SYSTEM_MAP,
    CallerID,
    SourceTrust,
    snapshot_ownership_error,
)
from bridge_db.snapshot_service import (
    SnapshotRefusalDecision,
    SnapshotRetentionPolicy,
    acknowledge_snapshot_refusal_record,
    save_snapshot_record,
    snapshot_capacity,
    snapshot_family,
)

logger = logging.getLogger("bridge_db.tools.snapshots")

def _utc_snapshot_date() -> str:
    return clock.now().date().isoformat()


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

        db = get_db(ctx)
        snap_date = snapshot_date or _utc_snapshot_date()
        try:
            result = await save_snapshot_record(
                db,
                caller=caller,
                system=system,
                data=data,
                snapshot_date=snap_date,
                source_trust=source_trust,
                retention_policy=retention_policy,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

        result["source_trust_clamped"] = source_trust_clamped
        if not result["ok"]:
            log_audit(
                "save_snapshot.refused",
                caller,
                None,
                ok=False,
                detail=(
                    f"reason={result['reason_code']} refusal_id={result['refusal_id']} "
                    f"system={system} family={result['snapshot_family']} "
                    f"retained={result['retained_count']} "
                    f"limit={result['retention_limit']} date={snap_date} "
                    f"next_state={result['next_state']}"
                ),
            )
            logger.warning(
                "snapshot refused: system=%s family=%s refusal_id=%s next_state=%s",
                system,
                result["snapshot_family"],
                result["refusal_id"],
                result["next_state"],
            )
            return result

        if result["pruned_count"]:
            log_audit(
                "save_snapshot.prune",
                caller,
                None,
                ok=True,
                detail=(
                    f"pruned={result['pruned_count']} ids={result['pruned_ids']} "
                    f"families={result['pruned_families']} system={system}"
                ),
            )
        logger.info(
            "snapshot saved: system=%s id=%s date=%s",
            system,
            result["snapshot_id"],
            snap_date,
        )
        return result

    @mcp.tool()
    async def get_snapshot_capacity(
        caller: Annotated[CallerID, Field(description="Must be 'cc' or 'codex'")],
        data: Annotated[
            dict[str, Any],
            Field(description="Prospective snapshot object used to select its family"),
        ],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Return owner/family capacity before a snapshot write is attempted."""
        require_bound_caller(ctx, caller, tool="get_snapshot_capacity")
        system = SNAPSHOT_SYSTEM_MAP.get(caller)
        if system is None:
            raise ToolError(snapshot_ownership_error(caller))
        family = snapshot_family(system, data)
        capacity = await snapshot_capacity(get_db(ctx), system=system, family=family)
        return {
            "ok": True,
            "caller": caller,
            **capacity.to_dict(),
            "mutation_performed": False,
        }

    @mcp.tool()
    async def acknowledge_snapshot_refusal(
        caller: Annotated[
            CallerID,
            Field(
                description=(
                    "Must own the refusal or hold an active exact-resource "
                    "operator delegation"
                )
            ),
        ],
        refusal_id: Annotated[int, Field(ge=1, description="Exact durable refusal ID")],
        decision: Annotated[
            SnapshotRefusalDecision,
            Field(
                description=(
                    "Owner decision: preserve_history, retry_after_owner_action, "
                    "or superseded. No decision grants deletion authority."
                )
            ),
        ],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Acknowledge an exact refusal and publish its next state.

        An append-only exact-resource delegation may authorize a different
        bound caller without changing the refusal's stored original owner.
        """
        require_caller(ctx, caller, tool="acknowledge_snapshot_refusal")
        require_bound_caller(ctx, caller, tool="acknowledge_snapshot_refusal")
        try:
            result = await acknowledge_snapshot_refusal_record(
                get_db(ctx),
                caller=caller,
                refusal_id=refusal_id,
                decision=decision,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        log_audit(
            "acknowledge_snapshot_refusal",
            caller,
            None,
            ok=bool(result["ok"]),
            detail=(
                f"refusal_id={refusal_id} decision={decision} "
                f"reason={result.get('reason_code')} next_state={result.get('next_state')}"
            ),
        )
        return result

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
