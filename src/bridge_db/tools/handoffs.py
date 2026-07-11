"""Handoff queue tools: create_handoff, get_pending_handoffs, pick_up_handoff, clear_handoff."""

import logging
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from bridge_db.audit import log_audit
from bridge_db.auth import clamp_source_trust, get_principal, require_caller
from bridge_db.db import (
    fts_text_for_handoff,
    get_db,
    record_write_conflict,
    record_write_conflict_once,
    rollback_on_error,
    upsert_fts_entry,
)
from bridge_db.instruction_boundary import instruction_boundary
from bridge_db.invariants import always, always_tx, sometimes
from bridge_db.models import CallerID, SourceTrust
from bridge_db.project_resolver import resolve as resolve_project

logger = logging.getLogger("bridge_db.tools.handoffs")


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def create_handoff(
        caller: Annotated[
            CallerID,
            Field(
                description="Must be 'claude_ai' — only Claude.ai dispatches handoffs"
            ),
        ],
        project_name: Annotated[
            str, Field(description="Name of the project being handed off")
        ],
        project_path: Annotated[
            str | None, Field(description="Absolute path to the project directory")
        ] = None,
        roadmap_file: Annotated[
            str | None, Field(description="Relative path to the roadmap/plan file")
        ] = None,
        phase: Annotated[
            str | None, Field(description="Phase or step to start from, e.g. 'Phase 2'")
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
        """Create a project handoff for Claude Code or Codex to pick up. Only claude_ai may dispatch."""
        require_caller(ctx, caller, tool="create_handoff")
        if caller != "claude_ai":
            raise ToolError(
                f"Only 'claude_ai' may create handoffs; caller was '{caller}'"
            )
        source_trust, source_trust_clamped = clamp_source_trust(
            source_trust, caller=caller, tool="create_handoff"
        )

        db = get_db(ctx)
        resolution = resolve_project(project_name)
        cursor = await db.execute(
            """
            INSERT INTO pending_handoffs
                (project_name, project_path, roadmap_file, phase, dispatched_from,
                 canonical_key, source_trust)
            VALUES (?, ?, ?, ?, 'claude_ai', ?, ?)
            """,
            (
                project_name,
                project_path,
                roadmap_file,
                phase,
                resolution.canonical_key,
                source_trust,
            ),
        )
        handoff_id = cursor.lastrowid

        if handoff_id is not None:
            await upsert_fts_entry(
                db,
                "handoff",
                str(handoff_id),
                fts_text_for_handoff(project_name, project_path, roadmap_file, phase),
            )

        await db.commit()

        log_audit(
            "create_handoff",
            caller,
            project_name,
            ok=True,
            detail=f"source_trust={source_trust}",
        )
        if resolution.registry_present and not resolution.matched:
            # Drift: a real handoff with no canonical match. Surface it via the
            # audit log rather than silently recording an unresolvable name, so a
            # later clear_handoff alias can't expect a canonical match that the
            # registry never produced.
            log_audit(
                "create_handoff.unmatched_project",
                caller,
                project_name,
                ok=True,
                detail="no canonical match in project-registry; flagged for triage",
            )
            logger.warning(
                "create_handoff: unmatched project_name %r (no canonical key)",
                project_name,
            )

        logger.info("handoff created: id=%d project=%s", handoff_id, project_name)
        return {
            "ok": True,
            "handoff_id": handoff_id,
            "project_name": project_name,
            "canonical_key": resolution.canonical_key,
            "source_trust": source_trust,
            "source_trust_clamped": source_trust_clamped,
            "status": "pending",
        }

    @mcp.tool()
    async def get_pending_handoffs(
        status: Annotated[
            Literal["pending", "active", "all"],
            Field(
                description="Filter: 'pending' (default, unclaimed work), 'active' "
                "(claimed and in progress; shows who holds it), or 'all' (both). "
                "Cleared rows are history and stay excluded — recall covers them."
            ),
        ] = "pending",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> list[dict[str, Any]]:
        """Return open handoffs, newest first. Used by /start to surface priority work.

        The default status='pending' preserves the original contract exactly.
        'active' and 'all' expose live claims: v13 records claimed_by on pickup
        and the INV-13 clear gate keys on it, so the claim ledger deserves a
        read surface — who holds what, since when — without raw SQL.
        """
        db = get_db(ctx)
        statuses = ("pending", "active") if status == "all" else (status,)
        placeholders = ", ".join("?" for _ in statuses)
        cursor = await db.execute(
            f"""
            SELECT id, project_name, project_path, roadmap_file, phase,
                   dispatched_from, dispatched_at, picked_up_at, status,
                   canonical_key, source_trust, claimed_by
            FROM pending_handoffs
            WHERE status IN ({placeholders})
            ORDER BY dispatched_at DESC, id DESC
            """,  # noqa: S608 — placeholders count a closed literal tuple
            statuses,
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "project_name": r["project_name"],
                "project_path": r["project_path"],
                "roadmap_file": r["roadmap_file"],
                "phase": r["phase"],
                "dispatched_from": r["dispatched_from"],
                "dispatched_at": r["dispatched_at"],
                "picked_up_at": r["picked_up_at"],
                "status": r["status"],
                "canonical_key": r["canonical_key"],
                "source_trust": r["source_trust"],
                "claimed_by": r["claimed_by"],
                "instruction_boundary": instruction_boundary(r["source_trust"]),
            }
            for r in rows
        ]

    @mcp.tool()
    async def pick_up_handoff(
        caller: Annotated[
            CallerID,
            Field(description="The system picking up the handoff: 'cc' or 'codex'"),
        ],
        handoff_id: Annotated[int, Field(description="ID of the handoff to pick up")],
        confirm: Annotated[
            bool,
            Field(
                description="Operator confirmation to pick up a non-operator-trust handoff "
                "(cc only; ignored for codex, which is refused until promoted)"
            ),
        ] = False,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Mark a handoff as active (in progress). Only 'cc' or 'codex' may pick up.

        Provenance gate: a non-'operator' handoff is gated at this pending → active
        transition. 'cc' must re-invoke with confirm=True; 'codex' (danger-full-access)
        is refused until the handoff is promoted to operator trust. 'operator'-trust
        handoffs pick up in one call, as before.
        """
        require_caller(ctx, caller, tool="pick_up_handoff")
        if caller not in ("cc", "codex"):
            raise ToolError(
                f"Only 'cc' or 'codex' may pick up handoffs; caller was '{caller}'"
            )

        db = get_db(ctx)
        cursor = await db.execute(
            "SELECT id, project_name, status, source_trust, canonical_key, claimed_by "
            "FROM pending_handoffs WHERE id = ?",
            (handoff_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ToolError(f"No handoff found with id {handoff_id}")
        if row["status"] != "pending":
            # INV-2 recovery half: a refused re-attempt is the loser's only
            # durable trace when its raced_claim receipt died with a crashed
            # process — record it, idempotently, before raising. The distinct
            # reason keeps the raced_claim ledger phantom-free: a late or
            # retried arrival is not itself a race. The insert-if-absent is
            # atomic at statement level, so concurrent retries converge to
            # one row instead of both passing a separate find.
            async with rollback_on_error(db):
                receipt_id = await record_write_conflict_once(
                    db,
                    surface="handoff",
                    target_key=str(handoff_id),
                    operation="pick_up_handoff",
                    attempted_by=caller,
                    principal=get_principal(ctx),
                    reason="stale_claim",
                    current_source_trust=row["source_trust"],
                    detail={
                        "project_name": row["project_name"],
                        "current_status": row["status"],
                        "claimed_by": row["claimed_by"],
                    },
                )
                await db.commit()
            sometimes("stale_claim_receipt_written")
            log_audit(
                "pick_up_handoff",
                caller,
                row["project_name"],
                ok=False,
                detail=(
                    f"decision=stale_claim status={row['status']} "
                    f"receipt_id={receipt_id}"
                ),
            )
            raise ToolError(
                f"Handoff {handoff_id} is not pending (status: {row['status']}). "
                f"Conflict receipt: {receipt_id}."
            )

        trust = row["source_trust"]
        project = row["project_name"]

        # The provenance gate keys on the channel-bound principal when one is
        # bound, falling back to the claimed caller only when unbound (auth
        # 'off'/legacy). Without this, a principal-bound connection could dodge
        # the Codex refusal in 'warn' mode (where require_caller audits but does
        # not block a caller/principal mismatch) by claiming a different caller.
        gate_identity = get_principal(ctx) or caller

        # Provenance gate — the pending → active transition is the dangerous step.
        if trust != "operator":
            if gate_identity == "codex":
                log_audit(
                    "pick_up_handoff",
                    caller,
                    project,
                    ok=False,
                    detail=f"source_trust={trust} decision=refused principal={get_principal(ctx)}",
                )
                raise ToolError(
                    f"Codex cannot pick up a non-operator-trust handoff (source_trust='{trust}'). "
                    "Promote it to operator trust before picking up with Codex."
                )
            if not confirm:  # gate_identity is 'cc' (or an unbound caller)
                log_audit(
                    "pick_up_handoff",
                    caller,
                    project,
                    ok=False,
                    detail=f"source_trust={trust} decision=confirmation_required",
                )
                return {
                    "ok": False,
                    "requires_confirmation": True,
                    "handoff_id": handoff_id,
                    "project_name": project,
                    "source_trust": trust,
                    "status": "pending",
                    "reason": (
                        "non-operator-trust handoff; re-invoke pick_up_handoff with confirm=True"
                    ),
                }
            # cc + confirm=True → confirmed override; fall through to the transition.

        # INV-8: trust is a second TOCTOU coordinate the status guard alone
        # does not cover — the provenance gate above evaluated SELECT-time
        # trust, so the CAS re-verifies that same value at commit time. A
        # mid-window trust change (no tool writes one today; this is armor
        # for the day an ingest/sync/admin path does) makes the CAS miss and
        # the claim is refused instead of landing under stale trust.
        cursor = await db.execute(
            """
            UPDATE pending_handoffs
            SET status = 'active',
                picked_up_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                claimed_by = ?
            WHERE id = ? AND status = 'pending' AND source_trust = ?
            """,
            (gate_identity, handoff_id, trust),
        )
        await always_tx(
            db,
            cursor.rowcount <= 1,
            "INV-1: handoff claim CAS must commit at most one winner",
            handoff_id=handoff_id,
            rowcount=cursor.rowcount,
        )
        if cursor.rowcount == 0:
            # CAS guard: another 'cc'/'codex' caller transitioned this handoff out
            # of 'pending' between our SELECT above and this UPDATE (the TOCTOU
            # window). The status-guarded UPDATE — not the earlier SELECT — is the
            # real, single-winner claim.
            #
            # INV-2: the receipt stays in the claim attempt's own transaction.
            # The zero-change UPDATE already holds it open; the re-read and
            # receipt INSERT stage into it and the single commit below makes
            # the loss durable atomically. The old rollback→separate-receipt-
            # commit shape left a two-op crash window that silently dropped
            # the receipt (DST corpus: receipt-crash seed 11).
            async with rollback_on_error(db):
                status_cursor = await db.execute(
                    "SELECT status FROM pending_handoffs WHERE id = ?",
                    (handoff_id,),
                )
                current_status = await status_cursor.fetchone()
                receipt_id = await record_write_conflict(
                    db,
                    surface="handoff",
                    target_key=str(handoff_id),
                    operation="pick_up_handoff",
                    attempted_by=caller,
                    principal=get_principal(ctx),
                    reason="raced_claim",
                    current_source_trust=trust,
                    detail={
                        "project_name": project,
                        "current_status": current_status["status"]
                        if current_status is not None
                        else None,
                    },
                )
                await db.commit()
            sometimes("raced_claim_receipt_written")
            log_audit(
                "pick_up_handoff",
                caller,
                project,
                ok=False,
                detail=f"source_trust={trust} decision=raced receipt_id={receipt_id}",
            )
            raise ToolError(
                f"Handoff {handoff_id} was picked up by another caller before this "
                "pickup completed; re-check get_pending_handoffs and retry if still needed. "
                f"Conflict receipt: {receipt_id}."
            )
        await db.commit()
        decision = (
            "allowed confirm=true" if (trust != "operator" and confirm) else "allowed"
        )
        log_audit(
            "pick_up_handoff",
            caller,
            project,
            ok=True,
            detail=f"source_trust={trust} decision={decision}",
        )
        logger.info(
            "handoff picked up: id=%d by %s (source_trust=%s)",
            handoff_id,
            caller,
            trust,
        )
        return {
            "ok": True,
            "handoff_id": handoff_id,
            "project_name": project,
            "canonical_key": row["canonical_key"],
            "source_trust": trust,
            "status": "active",
            "claimed_by": gate_identity,
        }

    @mcp.tool()
    async def clear_handoff(
        caller: Annotated[
            CallerID,
            Field(description="Must be 'cc' or 'codex' — clears matched handoffs"),
        ],
        project_name: Annotated[
            str, Field(description="Project name to match and clear")
        ],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Clear a handoff by project name (mark as done). Called by /end after completing project work.

        Claimant gate (INV-13): 'pending' rows (never claimed) are always
        clearable — /finish and /bank clear opportunistically by project name
        from sessions that never claimed, and that contract is preserved.
        'active' rows are clearable only when claimed_by is NULL (legacy
        pre-v13 rows) or equals the gate identity (bound principal, falling
        back to the claimed caller). Refusals are reported, not raised:
        ok stays True and the response carries refused_ids/refused_count —
        deliberately asymmetric with pick_up_handoff's hard refusals, to match
        this tool's opportunistic no-op contract.

        Scope honesty: all cc windows share one principal, so this gate only
        protects cross-role clears (cc <-> codex); under live 'warn' auth it
        is accident-safety, not adversarial protection.
        """
        require_caller(ctx, caller, tool="clear_handoff")
        if caller not in ("cc", "codex"):
            raise ToolError(
                f"Only 'cc' or 'codex' may clear handoffs; caller was '{caller}'"
            )

        db = get_db(ctx)
        # Match by exact project_name OR — when the incoming name resolves through
        # the canonical registry — by shared canonical_key, so a handoff dispatched
        # as "IncidentMgmt" still clears when /end passes "IncidentManagement" (both
        # resolve to the same canonical key). The exact-name path is always present,
        # preserving today's behavior for rows with no canonical_key (F1).
        canonical = resolve_project(project_name).canonical_key
        if canonical is not None:
            match_sql = "(project_name = ? OR canonical_key = ?)"
            match_params: tuple[str, ...] = (project_name, canonical)
        else:
            match_sql = "project_name = ?"
            match_params = (project_name,)

        gate_identity = get_principal(ctx) or caller

        cursor = await db.execute(
            f"""
            SELECT id, status, claimed_by
            FROM pending_handoffs
            WHERE {match_sql} AND status != 'cleared'
            ORDER BY dispatched_at DESC, id DESC
            """,
            match_params,
        )
        rows = await cursor.fetchall()
        if not rows:
            # Not an error — handoff may not exist; /end calls this opportunistically
            return {
                "ok": True,
                "cleared": False,
                "reason": "No active handoff found for project",
            }

        clearable_ids: list[int] = []
        refused_ids: list[int] = []
        for row in rows:
            always(
                row["status"] in ("pending", "active"),
                "INV-3: only pending|active handoffs may move toward cleared",
                handoff_id=row["id"],
                status=row["status"],
            )
            claimant = row["claimed_by"]
            if (
                row["status"] == "active"
                and claimant is not None
                and claimant != gate_identity
            ):
                refused_ids.append(row["id"])
            else:
                clearable_ids.append(row["id"])

        if clearable_ids:
            id_placeholders = ", ".join("?" for _ in clearable_ids)
            await db.execute(
                f"""
                UPDATE pending_handoffs
                SET status = 'cleared', cleared_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                WHERE id IN ({id_placeholders})
                  AND (
                      status = 'pending'
                      OR (
                          status = 'active'
                          AND (claimed_by IS NULL OR claimed_by = ?)
                      )
                  )
                """,
                [*clearable_ids, gate_identity],
            )
            cursor = await db.execute(
                f"""
                SELECT id FROM pending_handoffs
                WHERE id IN ({id_placeholders}) AND status != 'cleared'
                """,
                clearable_ids,
            )
            race_refused_ids = [row["id"] for row in await cursor.fetchall()]
            # INV-3/INV-13 clear-race counter: the guarded UPDATE matched 0 rows
            # because a concurrent claim changed claimed_by between this call's
            # SELECT and its UPDATE — distinct from a static foreign refusal
            # decided in the loop above (both feed clear_refused_foreign_claim).
            sometimes("clear_refused_race", bool(race_refused_ids))
            if race_refused_ids:
                refused_ids.extend(race_refused_ids)
                clearable_ids = [
                    handoff_id
                    for handoff_id in clearable_ids
                    if handoff_id not in race_refused_ids
                ]
        await db.commit()
        sometimes("clear_refused_foreign_claim", bool(refused_ids))

        if refused_ids:
            log_audit(
                "clear_handoff.refused_foreign_claim",
                caller,
                project_name,
                ok=False,
                detail=f"refused_ids={refused_ids} gate_identity={gate_identity}",
            )

        if not clearable_ids:
            return {
                "ok": True,
                "cleared": False,
                "reason": "All matched handoffs are actively claimed by another identity",
                "refused_ids": refused_ids,
                "refused_count": len(refused_ids),
                "project_name": project_name,
                "canonical_key": canonical,
            }

        logger.info(
            "handoffs cleared: project=%s by %s count=%d",
            project_name,
            caller,
            len(clearable_ids),
        )
        return {
            "ok": True,
            "cleared": True,
            "handoff_id": clearable_ids[0],
            "handoff_ids": clearable_ids,
            "cleared_count": len(clearable_ids),
            "refused_ids": refused_ids,
            "refused_count": len(refused_ids),
            "project_name": project_name,
            "canonical_key": canonical,
        }
