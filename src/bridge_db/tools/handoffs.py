"""Handoff queue tools: create_handoff, get_pending_handoffs, pick_up_handoff, clear_handoff."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import hashlib
import logging
import secrets
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from bridge_db import clock, config
from bridge_db.audit import log_audit
from bridge_db.capacity import require_combined_bytes, require_utf8_bytes
from bridge_db.auth import (
    clamp_source_trust,
    get_principal,
    require_bound_caller,
    require_caller,
)
from bridge_db.db import (
    fts_text_for_handoff,
    get_db,
    record_write_conflict,
    record_write_conflict_once,
    rollback_on_error,
    upsert_fts_entry,
)
from bridge_db.instruction_boundary import instruction_boundary
from bridge_db.invariants import always_tx, sometimes
from bridge_db.models import CallerID, SourceTrust
from bridge_db.project_resolver import resolve as resolve_project

logger = logging.getLogger("bridge_db.tools.handoffs")

_CAPABILITY_VERSION = "hcap1"
_CAPABILITY_TRANSITION = "clear"
_capability_token_provider: Callable[[int], str] = secrets.token_urlsafe


def _install_capability_token_provider(provider: Callable[[int], str]) -> None:
    """Install a deterministic capability source for the DST harness only."""
    global _capability_token_provider
    _capability_token_provider = provider


def _reset_capability_token_provider() -> None:
    global _capability_token_provider
    _capability_token_provider = secrets.token_urlsafe


def _utc_text(value: datetime) -> str:
    """Serialize a UTC-aware timestamp in the repository's canonical Z form."""
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc_text(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _mint_completion_capability() -> tuple[str, str, str]:
    """Mint an opaque bearer capability and return session id, token, hash."""
    session_id = _capability_token_provider(18)
    secret = _capability_token_provider(32)
    token = f"{_CAPABILITY_VERSION}.{session_id}.{secret}"
    return session_id, token, hashlib.sha256(token.encode("utf-8")).hexdigest()


def _completion_capability_hash(value: str) -> str | None:
    """Validate the public envelope before hashing; never log the bearer value."""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if (
        not value.startswith(f"{_CAPABILITY_VERSION}.")
        or value != value.strip()
        or len(encoded) > config.HANDOFF_CAPABILITY_MAX_BYTES
        or value.count(".") != 2
    ):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _validate_handoff_payload(
    *,
    project_name: str,
    project_path: str | None,
    roadmap_file: str | None,
    phase: str | None,
) -> None:
    sizes = [
        require_utf8_bytes(
            project_name,
            config.HANDOFF_PROJECT_NAME_MAX_BYTES,
            "handoff.project_name_utf8_bytes_exceeded",
        ),
        require_utf8_bytes(
            project_path,
            config.HANDOFF_PROJECT_PATH_MAX_BYTES,
            "handoff.project_path_utf8_bytes_exceeded",
        ),
        require_utf8_bytes(
            roadmap_file,
            config.HANDOFF_ROADMAP_FILE_MAX_BYTES,
            "handoff.roadmap_file_utf8_bytes_exceeded",
        ),
        require_utf8_bytes(
            phase,
            config.HANDOFF_PHASE_MAX_BYTES,
            "handoff.phase_utf8_bytes_exceeded",
        ),
    ]
    require_combined_bytes(
        sizes,
        config.HANDOFF_COMBINED_MAX_BYTES,
        "handoff.combined_utf8_bytes_exceeded",
    )


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
                description="Requested provenance. 'operator' is always clamped to "
                "'agent' at this MCP boundary; use the TTY-gated promotion CLI "
                "after reviewing the stored pending handoff."
            ),
        ] = "agent",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Create an agent-trust handoff from a channel-bound Claude.ai client."""
        # Handoff creation is an instruction-bearing dispatch boundary. It must
        # not inherit the global auth rollout bypass or mint operator trust.
        require_bound_caller(ctx, caller, tool="create_handoff")
        if caller != "claude_ai":
            raise ToolError(
                f"Only 'claude_ai' may create handoffs; caller was '{caller}'"
            )
        source_trust, source_trust_clamped = clamp_source_trust(
            source_trust, caller=caller, tool="create_handoff", strict=True
        )
        _validate_handoff_payload(
            project_name=project_name,
            project_path=project_path,
            roadmap_file=roadmap_file,
            phase=phase,
        )

        db = get_db(ctx)
        resolution = resolve_project(project_name)
        cursor = await db.execute(
            """
            INSERT INTO pending_handoffs
                (project_name, project_path, roadmap_file, phase, dispatched_from,
                 canonical_key, source_trust)
            SELECT ?, ?, ?, ?, 'claude_ai', ?, ?
            WHERE (
                SELECT COUNT(*) FROM pending_handoffs
                WHERE status IN ('pending', 'active')
            ) < ?
            AND (
                SELECT COUNT(*) FROM pending_handoffs
            ) < ?
            RETURNING id
            """,
            (
                project_name,
                project_path,
                roadmap_file,
                phase,
                resolution.canonical_key,
                source_trust,
                config.HANDOFF_OPEN_QUOTA,
                config.HANDOFF_TOTAL_ROWS_QUOTA,
            ),
        )
        inserted = await cursor.fetchone()
        if inserted is None:
            await db.rollback()
            total_row = await (
                await db.execute("SELECT COUNT(*) FROM pending_handoffs")
            ).fetchone()
            if total_row is not None and int(total_row[0]) >= (
                config.HANDOFF_TOTAL_ROWS_QUOTA
            ):
                raise ToolError(
                    "handoff.total_row_quota_exceeded: "
                    f"maximum={config.HANDOFF_TOTAL_ROWS_QUOTA}"
                )
            raise ToolError(
                "handoff.open_queue_quota_exceeded: "
                f"maximum={config.HANDOFF_OPEN_QUOTA}"
            )
        handoff_id = int(inserted["id"])

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
        limit: Annotated[
            int,
            Field(
                description="Page size; use before_id to fetch older open handoffs",
                ge=1,
                le=config.HANDOFF_PAGE_MAX,
            ),
        ] = config.HANDOFF_PAGE_DEFAULT,
        before_id: Annotated[
            int | None,
            Field(description="Return rows with id lower than this page cursor", ge=1),
        ] = None,
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
        before_sql = "AND h.id < ?" if before_id is not None else ""
        params: list[Any] = [*statuses]
        if before_id is not None:
            params.append(before_id)
        params.append(limit)
        cursor = await db.execute(
            f"""
            SELECT h.id, h.project_name, h.project_path, h.roadmap_file, h.phase,
                   h.dispatched_from, h.dispatched_at, h.picked_up_at, h.status,
                   h.canonical_key, h.source_trust, h.claimed_by,
                   c.session_id AS claim_session_id,
                   c.expires_at AS capability_expires_at
            FROM pending_handoffs AS h
            LEFT JOIN handoff_session_capabilities AS c ON c.handoff_id = h.id
            WHERE h.status IN ({placeholders})
            {before_sql}
            ORDER BY h.id DESC
            LIMIT ?
            """,  # noqa: S608 — placeholders count a closed literal tuple
            params,
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
                "claim_session_id": r["claim_session_id"],
                "capability_expires_at": r["capability_expires_at"],
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
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Mark a handoff as active (in progress). Only 'cc' or 'codex' may pick up.

        Provenance gate: a non-'operator' handoff cannot cross this pending → active
        transition. An independent operator must first review and promote the exact
        pending row through the TTY-only promotion ceremony. 'operator'-trust
        handoffs pick up in one call.
        """
        require_caller(ctx, caller, tool="pick_up_handoff")
        require_bound_caller(ctx, caller, tool="pick_up_handoff")
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

        # Strict binding above makes the verified principal and claimed caller
        # identical for every reachable mutation.
        gate_identity = caller

        # Provenance gate — the pending → active transition is the dangerous step.
        # The consuming MCP principal cannot supply its own approval evidence;
        # only the exact-row TTY ceremony can establish operator trust.
        if trust != "operator":
            log_audit(
                "pick_up_handoff",
                caller,
                project,
                ok=False,
                detail=(
                    f"source_trust={trust} decision=refused "
                    f"principal={get_principal(ctx)}"
                ),
            )
            raise ToolError(
                f"Handoff {handoff_id} has non-operator source trust '{trust}'. "
                "Review and promote the exact pending handoff to operator trust with "
                f"`python -m bridge_db --promote-handoff {handoff_id}` before pickup."
            )

        # INV-8: trust is a second TOCTOU coordinate the status guard alone
        # does not cover — the provenance gate above evaluated SELECT-time
        # trust, so the CAS re-verifies that same value at commit time. A
        # mid-window trust change (no tool writes one today; this is armor
        # for the day an ingest/sync/admin path does) makes the CAS miss and
        # the claim is refused instead of landing under stale trust.
        claim_session_id, completion_capability, capability_sha256 = (
            _mint_completion_capability()
        )
        issued_at = clock.now()
        expires_at = issued_at + timedelta(hours=config.HANDOFF_CAPABILITY_TTL_HOURS)
        issued_at_text = _utc_text(issued_at)
        expires_at_text = _utc_text(expires_at)

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
        try:
            await db.execute(
                """
                INSERT INTO handoff_session_capabilities (
                    handoff_id, session_id, token_sha256, claimed_caller,
                    allowed_transition, issued_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handoff_id,
                    claim_session_id,
                    capability_sha256,
                    caller,
                    _CAPABILITY_TRANSITION,
                    issued_at_text,
                    expires_at_text,
                ),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        log_audit(
            "pick_up_handoff",
            caller,
            project,
            ok=True,
            detail=f"source_trust={trust} decision=allowed",
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
            "claim_session_id": claim_session_id,
            "completion_capability": completion_capability,
            "capability_expires_at": expires_at_text,
            "allowed_transition": _CAPABILITY_TRANSITION,
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
        handoff_id: Annotated[
            int | None,
            Field(description="Exact handoff ID returned by pick_up_handoff", ge=1),
        ] = None,
        completion_capability: Annotated[
            str | None,
            Field(
                description="Short-lived completion capability returned only to "
                "the session that claimed this handoff"
            ),
        ] = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Complete one exact handoff using its claiming-session capability.

        Role authorization remains mandatory, but role identity is not treated
        as session identity. ``pick_up_handoff`` returns the only copy of a
        short-lived bearer secret; this transition binds its hash to the exact
        handoff, claimant role, and ``clear`` action and consumes it atomically.
        The optional parameter defaults preserve old client schemas, but a live
        claimed handoff fails closed until both new fields are supplied.
        """
        require_caller(ctx, caller, tool="clear_handoff")
        require_bound_caller(ctx, caller, tool="clear_handoff")
        if caller not in ("cc", "codex"):
            raise ToolError(
                f"Only 'cc' or 'codex' may clear handoffs; caller was '{caller}'"
            )

        db = get_db(ctx)
        canonical = resolve_project(project_name).canonical_key
        if canonical is not None:
            match_sql = "(project_name = ? OR canonical_key = ?)"
            match_params: tuple[str, ...] = (project_name, canonical)
        else:
            match_sql = "project_name = ?"
            match_params = (project_name,)

        def refused(
            reason_code: str,
            reason: str,
            *,
            refused_handoff_id: int | None = handoff_id,
            claim_session_id: str | None = None,
            foreign_claim: bool = False,
        ) -> dict[str, Any]:
            sometimes("clear_refused_session_capability")
            if foreign_claim:
                sometimes("clear_refused_foreign_claim")
            log_audit(
                "clear_handoff.refused_session_capability",
                caller,
                project_name,
                ok=False,
                detail=(
                    f"reason_code={reason_code} handoff_id={refused_handoff_id} "
                    f"claim_session_id={claim_session_id or 'unknown'}"
                ),
            )
            return {
                "ok": True,
                "cleared": False,
                "reason": reason,
                "reason_code": reason_code,
                "refused_ids": [refused_handoff_id]
                if refused_handoff_id is not None
                else [],
                "refused_count": 1 if refused_handoff_id is not None else 0,
                "project_name": project_name,
                "canonical_key": canonical,
            }

        if handoff_id is None or completion_capability is None:
            cursor = await db.execute(
                f"""
                SELECT id FROM pending_handoffs
                WHERE {match_sql} AND status != 'cleared'
                ORDER BY dispatched_at DESC, id DESC
                """,
                match_params,
            )
            open_ids = [int(row["id"]) for row in await cursor.fetchall()]
            if not open_ids:
                return {
                    "ok": True,
                    "cleared": False,
                    "reason": "No active handoff found for project",
                }
            return refused(
                "session_capability_required",
                "Claiming-session handoff ID and completion capability are required",
                refused_handoff_id=handoff_id or open_ids[0],
            )

        capability_sha256 = _completion_capability_hash(completion_capability)
        if capability_sha256 is None:
            return refused(
                "invalid_session_capability",
                "Completion capability is malformed or exceeds the size limit",
            )

        cursor = await db.execute(
            """
            SELECT c.handoff_id, c.session_id, c.claimed_caller,
                   c.allowed_transition, c.expires_at, c.consumed_at, c.recovered_at,
                   h.project_name, h.canonical_key, h.status, h.claimed_by
            FROM handoff_session_capabilities AS c
            JOIN pending_handoffs AS h ON h.id = c.handoff_id
            WHERE c.token_sha256 = ?
            """,
            (capability_sha256,),
        )
        capability = await cursor.fetchone()
        if capability is None:
            return refused(
                "invalid_session_capability",
                "Completion capability is not recognized",
            )

        claim_session_id = str(capability["session_id"])
        capability_handoff_id = int(capability["handoff_id"])
        if capability_handoff_id != handoff_id:
            return refused(
                "wrong_handoff",
                "Completion capability is bound to a different handoff",
                claim_session_id=claim_session_id,
            )
        project_matches = capability["project_name"] == project_name or (
            canonical is not None and capability["canonical_key"] == canonical
        )
        if not project_matches:
            return refused(
                "wrong_project",
                "Completion capability is bound to a different project",
                claim_session_id=claim_session_id,
            )
        if capability["claimed_caller"] != caller:
            return refused(
                "wrong_role",
                "Completion capability belongs to a different authorized role",
                claim_session_id=claim_session_id,
                foreign_claim=True,
            )
        if capability["allowed_transition"] != _CAPABILITY_TRANSITION:
            return refused(
                "wrong_transition",
                "Completion capability does not authorize this transition",
                claim_session_id=claim_session_id,
            )
        if capability["recovered_at"] is not None:
            return refused(
                "recovered_session_capability",
                "Completion capability was retired by audited orphan recovery",
                claim_session_id=claim_session_id,
            )
        if capability["consumed_at"] is not None:
            return refused(
                "replayed_session_capability",
                "Completion capability has already been consumed",
                claim_session_id=claim_session_id,
            )
        expires_at = _parse_utc_text(capability["expires_at"])
        if expires_at is None:
            return refused(
                "invalid_capability_state",
                "Stored completion capability has an invalid expiry",
                claim_session_id=claim_session_id,
            )
        if clock.now() >= expires_at:
            return refused(
                "expired_session_capability",
                "Completion capability has expired; use audited orphan recovery",
                claim_session_id=claim_session_id,
            )
        if capability["status"] != "active" or capability["claimed_by"] != caller:
            return refused(
                "claim_state_changed",
                "Handoff is no longer an active claim owned by this role",
                claim_session_id=claim_session_id,
                foreign_claim=capability["claimed_by"] not in (None, caller),
            )

        consumed_at = _utc_text(clock.now())
        async with rollback_on_error(db):
            await db.execute("BEGIN IMMEDIATE")
            current = await (
                await db.execute(
                    """
                    SELECT c.expires_at, c.consumed_at, c.recovered_at,
                           h.status, h.claimed_by
                    FROM handoff_session_capabilities AS c
                    JOIN pending_handoffs AS h ON h.id = c.handoff_id
                    WHERE c.handoff_id = ? AND c.token_sha256 = ?
                    """,
                    (handoff_id, capability_sha256),
                )
            ).fetchone()
            current_expiry = _parse_utc_text(current["expires_at"]) if current else None
            if (
                current is None
                or current["consumed_at"] is not None
                or current["recovered_at"] is not None
                or current["status"] != "active"
                or current["claimed_by"] != caller
                or current_expiry is None
                or clock.now() >= current_expiry
            ):
                await db.rollback()
                sometimes("clear_refused_race")
                return refused(
                    "capability_state_changed",
                    "Handoff capability state changed before completion",
                    claim_session_id=claim_session_id,
                )

            cursor = await db.execute(
                """
                UPDATE pending_handoffs
                SET status = 'cleared',
                    cleared_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                WHERE id = ? AND status = 'active' AND claimed_by = ?
                """,
                (handoff_id, caller),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                sometimes("clear_refused_race")
                return refused(
                    "capability_state_changed",
                    "Handoff claim changed before completion",
                    claim_session_id=claim_session_id,
                )
            cursor = await db.execute(
                """
                UPDATE handoff_session_capabilities
                SET consumed_at = ?
                WHERE handoff_id = ? AND token_sha256 = ?
                  AND consumed_at IS NULL AND recovered_at IS NULL
                """,
                (consumed_at, handoff_id, capability_sha256),
            )
            await always_tx(
                db,
                cursor.rowcount == 1,
                "INV-14: one completion consumes exactly one session capability",
                handoff_id=handoff_id,
                claim_session_id=claim_session_id,
                rowcount=cursor.rowcount,
            )
            await db.execute(
                """
                INSERT INTO handoff_lifecycle_receipts (
                    handoff_id, event_type, principal, claimed_caller,
                    requested_project_name, canonical_key, match_basis,
                    previous_status, previous_claimant
                ) VALUES (?, 'cleared', ?, ?, ?, ?, ?, 'active', ?)
                """,
                (
                    handoff_id,
                    caller,
                    caller,
                    project_name,
                    capability["canonical_key"],
                    "exact"
                    if capability["project_name"] == project_name
                    else "canonical_alias",
                    caller,
                ),
            )
            await db.commit()

        cursor = await db.execute(
            f"""
            SELECT id FROM pending_handoffs
            WHERE {match_sql} AND status != 'cleared' AND id != ?
            ORDER BY dispatched_at DESC, id DESC
            """,
            (*match_params, handoff_id),
        )
        refused_ids = [int(row["id"]) for row in await cursor.fetchall()]
        if refused_ids:
            sometimes("clear_refused_foreign_claim")

        log_audit(
            "clear_handoff",
            caller,
            project_name,
            ok=True,
            detail=(
                f"handoff_id={handoff_id} claim_session_id={claim_session_id} "
                f"decision=allowed"
            ),
        )
        logger.info(
            "handoff cleared: project=%s id=%d by %s session=%s",
            project_name,
            handoff_id,
            caller,
            claim_session_id,
        )
        return {
            "ok": True,
            "cleared": True,
            "handoff_id": handoff_id,
            "handoff_ids": [handoff_id],
            "cleared_count": 1,
            "refused_ids": refused_ids,
            "refused_count": len(refused_ids),
            "project_name": project_name,
            "canonical_key": canonical,
            "claim_session_id": claim_session_id,
        }
