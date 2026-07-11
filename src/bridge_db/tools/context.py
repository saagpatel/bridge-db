"""Context section tools: update_section, get_section, get_all_sections, sync_from_file."""

import logging
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from bridge_db import config
from bridge_db.audit import log_audit
from bridge_db.auth import auth_mode, clamp_source_trust, get_principal, require_caller
from bridge_db.db import (
    content_sha256,
    fts_text_for_section,
    get_db,
    record_write_conflict,
    rollback_on_error,
    upsert_fts_entry,
)
from bridge_db.instruction_boundary import instruction_boundary
from bridge_db.invariants import always_tx, sometimes
from bridge_db.models import SECTION_OWNERS, CallerID, SourceTrust

logger = logging.getLogger("bridge_db.tools.context")

_CAS_MODES = frozenset({"warn", "enforce"})

_SECTION_HEADING_MAP: dict[str, str] = {
    "Career & Professional Target": "career",
    "Speaking Engagements": "speaking",
    "Active Research Themes": "research",
    "Claude.ai Capabilities Summary": "capabilities",
}


def parse_owned_sections(markdown: str) -> dict[str, str]:
    """Extract only Claude.ai-owned section bodies from the bridge markdown file."""
    parsed: dict[str, list[str]] = {}
    current_section: str | None = None

    for line in markdown.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            current_section = _SECTION_HEADING_MAP.get(heading)
            if current_section is not None:
                parsed[current_section] = []
            continue

        if current_section is not None:
            parsed[current_section].append(line)

    return {
        section_name: "\n".join(lines).strip("\n")
        for section_name, lines in parsed.items()
    }


def _normalized_section_content(content: str) -> str:
    return content.strip("\n")


def _context_cas_mode() -> str:
    mode = config.CONTEXT_CAS_MODE.strip().lower()
    return mode if mode in _CAS_MODES else "enforce"


async def _upsert_section(
    db: Any,
    section_name: str,
    owner: str,
    content: str,
    source_trust: SourceTrust | None = None,
    *,
    attempted_by: str,
    operation: str,
    principal: str | None = None,
    receipt_surface: str = "context_section",
    if_match_updated_at: str | None = None,
    if_match_version: int | None = None,
) -> dict[str, Any]:
    # source_trust is COALESCEd: a fresh row with no assertion takes 'agent'; a
    # content-only update (source_trust=None) preserves the row's existing label
    # rather than relabelling operator-authored sections on a routine re-sync.
    #
    # if_match_version is the real CAS token. if_match_updated_at remains a
    # compatibility guard for existing callers and is enforced when supplied.
    #
    # INV-5: conflict receipts are not caller-optional. Every rejection
    # (stale_cas, missing_cas) and every accepted blind overwrite of an
    # existing row stages its receipt HERE, in the same transaction as the
    # write decision, so the caller's single commit makes both durable
    # atomically — attempted_by/operation are required for that reason.
    if if_match_version is not None or if_match_updated_at is not None:
        conditions = ["section_name = ?"]
        params: list[Any] = [content, source_trust, section_name]
        if if_match_version is not None:
            conditions.append("version = ?")
            params.append(if_match_version)
        if if_match_updated_at is not None:
            conditions.append("updated_at = ?")
            params.append(if_match_updated_at)
        cursor = await db.execute(
            f"""
            UPDATE context_sections SET
                content = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                source_trust = COALESCE(?, source_trust),
                version = version + 1
            WHERE {" AND ".join(conditions)}
            """,  # noqa: S608 — conditions are assembled from fixed predicates only.
            params,
        )
        await always_tx(
            db,
            cursor.rowcount <= 1,
            "INV-4: section CAS update must match at most one row",
            section_name=section_name,
            rowcount=cursor.rowcount,
        )
        # INV-4's step-by-1 needs no re-read: the UPDATE sets
        # version = version + 1 in the same statement that matched
        # version = if_match_version, so rowcount == 1 IS the step proof.
        # A post-write SELECT would race legal concurrent writers on the
        # shared connection and fire on a false violation.
        if cursor.rowcount == 0:
            sometimes("stale_cas_rejection")
            # The zero-change UPDATE holds the transaction open; the receipt
            # (and its diagnostic re-read) stage into it — no separate
            # receipt transaction to crash out of.
            async with rollback_on_error(db):
                receipt_id, current = await _record_section_conflict(
                    db,
                    section_name=section_name,
                    caller=attempted_by,
                    operation=operation,
                    reason="stale_cas",
                    attempted_content=content,
                    attempted_source_trust=source_trust,
                    principal=principal,
                    stale_version=if_match_version,
                    stale_updated_at=if_match_updated_at,
                    surface=receipt_surface,
                )
            return {
                "written": False,
                "reason": "stale_cas",
                "receipt_id": receipt_id,
                "current": current,
            }
        await upsert_fts_entry(
            db, "section", section_name, fts_text_for_section(section_name, content)
        )
        return {"written": True, "legacy_blind_write": False, "receipt_id": None}

    cursor = await db.execute(
        "SELECT version FROM context_sections WHERE section_name = ?",
        (section_name,),
    )
    existing = await cursor.fetchone()
    legacy_blind_write = existing is not None
    if legacy_blind_write and _context_cas_mode() == "enforce":
        sometimes("missing_cas_rejection")
        async with rollback_on_error(db):
            receipt_id, current = await _record_section_conflict(
                db,
                section_name=section_name,
                caller=attempted_by,
                operation=operation,
                reason="missing_cas",
                attempted_content=content,
                attempted_source_trust=source_trust,
                principal=principal,
                surface=receipt_surface,
            )
        return {
            "written": False,
            "reason": "missing_cas",
            "receipt_id": receipt_id,
            "current": current,
        }

    receipt_id = None
    if existing is None:
        await db.execute(
            """
            INSERT INTO context_sections (section_name, owner, content, source_trust, updated_at)
            VALUES (?, ?, ?, COALESCE(?, 'agent'), strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            """,
            (section_name, owner, content, source_trust),
        )
    else:
        # Accepted blind overwrite (warn mode): the displacement stays legal
        # — that is INV-4's separate, config-level question — but it is
        # never trace-free. Staged BEFORE the UPDATE so the receipt's
        # current_* fields capture the displaced version and content sha.
        async with rollback_on_error(db):
            receipt_id, _ = await _record_section_conflict(
                db,
                section_name=section_name,
                caller=attempted_by,
                operation=operation,
                reason="legacy_blind_write",
                attempted_content=content,
                attempted_source_trust=source_trust,
                principal=principal,
                surface=receipt_surface,
            )
            await db.execute(
                """
                UPDATE context_sections SET
                    content = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                    source_trust = COALESCE(?, source_trust),
                    version = version + 1
                WHERE section_name = ?
                """,
                (content, source_trust, section_name),
            )
    await upsert_fts_entry(
        db, "section", section_name, fts_text_for_section(section_name, content)
    )
    sometimes("legacy_blind_write_accepted", legacy_blind_write)
    return {
        "written": True,
        "legacy_blind_write": legacy_blind_write,
        "receipt_id": receipt_id,
    }


async def _section_row(db: Any, section_name: str) -> Any | None:
    cursor = await db.execute(
        """
        SELECT section_name, owner, content, updated_at, source_trust, version
        FROM context_sections
        WHERE section_name = ?
        """,
        (section_name,),
    )
    return await cursor.fetchone()


async def _record_section_conflict(
    db: Any,
    *,
    section_name: str,
    caller: str,
    operation: str,
    reason: str,
    attempted_content: str,
    attempted_source_trust: str | None,
    principal: str | None = None,
    stale_version: int | None = None,
    stale_updated_at: str | None = None,
    surface: str = "context_section",
    detail: dict[str, Any] | None = None,
) -> tuple[int, Any | None]:
    current = await _section_row(db, section_name)
    receipt_id = await record_write_conflict(
        db,
        surface=surface,
        target_key=section_name,
        operation=operation,
        attempted_by=caller,
        principal=principal,
        stale_version=stale_version,
        current_version=current["version"] if current is not None else None,
        stale_updated_at=stale_updated_at,
        current_updated_at=current["updated_at"] if current is not None else None,
        attempted_source_trust=attempted_source_trust,
        current_source_trust=current["source_trust"] if current is not None else None,
        attempted_content_sha256=content_sha256(attempted_content),
        current_content_sha256=content_sha256(current["content"])
        if current is not None
        else None,
        reason=reason,
        detail=detail,
    )
    return receipt_id, current


async def sync_owned_sections_from_file(db: Any, bridge_path: Path) -> dict[str, Any]:
    """Read the bridge file and upsert the Claude.ai-owned context sections.

    With auth active (mode != 'off'), the file is an unauthenticated channel:
    unchanged sections are skipped (label preserved), changed or new sections
    are imported as source_trust='ingested' and reported in `demoted` so the
    operator can review and promote via `--promote-section`. In 'off' mode the
    legacy preserve-label upsert runs unchanged (rollback lever).
    """
    if not bridge_path.exists():
        raise ToolError(f"Bridge file not found: {bridge_path}")

    auth_active = auth_mode() != "off"
    parsed_sections = parse_owned_sections(bridge_path.read_text(encoding="utf-8"))
    synced_sections: list[str] = []
    unchanged: list[str] = []
    demoted: list[str] = []
    conflicts: list[dict[str, Any]] = []
    legacy_imports: list[str] = []

    for section_name in SECTION_OWNERS:
        if section_name not in parsed_sections:
            continue
        content = parsed_sections[section_name]
        current = await _section_row(db, section_name)

        if current is not None and _normalized_section_content(
            str(current["content"])
        ) == (_normalized_section_content(content)):
            unchanged.append(section_name)
            continue

        if current is not None:
            exported_cursor = await db.execute(
                """
                SELECT exported_version, exported_content_sha256
                FROM context_section_export_state
                WHERE section_name = ?
                """,
                (section_name,),
            )
            exported = await exported_cursor.fetchone()
            if exported is None:
                legacy_imports.append(section_name)
            else:
                current_hash = content_sha256(
                    _normalized_section_content(str(current["content"]))
                )
                if (
                    int(current["version"]) != int(exported["exported_version"])
                    or current_hash != exported["exported_content_sha256"]
                ):
                    receipt_id, refreshed = await _record_section_conflict(
                        db,
                        section_name=section_name,
                        caller="sync_from_file",
                        operation="sync_from_file",
                        reason="stale_export_base",
                        attempted_content=content,
                        attempted_source_trust="ingested" if auth_active else None,
                        principal=None,
                        stale_version=int(exported["exported_version"]),
                        surface="markdown_sync",
                        detail={
                            "exported_content_sha256": exported[
                                "exported_content_sha256"
                            ]
                        },
                    )
                    conflicts.append(
                        {
                            "section_name": section_name,
                            "receipt_id": receipt_id,
                            "reason": "stale_export_base",
                            "current_version": refreshed["version"]
                            if refreshed is not None
                            else None,
                            "current_updated_at": refreshed["updated_at"]
                            if refreshed is not None
                            else None,
                            "current_source_trust": refreshed["source_trust"]
                            if refreshed is not None
                            else None,
                        }
                    )
                    continue

        # INV-5: on rejection the receipt is staged by _upsert_section in
        # the same transaction as the refused write; the batch commit below
        # makes both durable together.
        result = await _upsert_section(
            db=db,
            section_name=section_name,
            owner="claude_ai",
            content=content,
            source_trust="ingested" if auth_active else None,
            attempted_by="sync_from_file",
            operation="sync_from_file",
            receipt_surface="markdown_sync",
            if_match_version=current["version"] if current is not None else None,
        )
        if not result["written"]:
            refreshed = result["current"]
            conflicts.append(
                {
                    "section_name": section_name,
                    "receipt_id": result["receipt_id"],
                    "reason": result["reason"],
                    "current_version": refreshed["version"]
                    if refreshed is not None
                    else None,
                }
            )
            continue
        if auth_active:
            demoted.append(section_name)

        synced_sections.append(section_name)

    await db.commit()
    if demoted:
        log_audit(
            "sync_from_file.demoted",
            None,
            None,
            ok=True,
            detail=f"sections={','.join(demoted)} label=ingested (file channel)",
        )
    logger.info(
        "synced %d claude_ai section(s) from %s (unchanged=%d, demoted=%d)",
        len(synced_sections),
        bridge_path,
        len(unchanged),
        len(demoted),
    )
    return {
        "ok": True,
        "path": str(bridge_path),
        "sections_synced": synced_sections,
        "unchanged": unchanged,
        "demoted": demoted,
        "conflicts": conflicts,
        "conflict_count": len(conflicts),
        "legacy_imports": legacy_imports,
        "count": len(synced_sections),
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def update_section(
        caller: Annotated[
            CallerID, Field(description="The system updating this section")
        ],
        section_name: Annotated[
            str,
            Field(
                description="Section key, e.g. 'career', 'speaking', 'research', 'capabilities', 'portfolio'"
            ),
        ],
        content: Annotated[
            str, Field(description="Full markdown content for this section")
        ],
        source_trust: Annotated[
            SourceTrust | None,
            Field(
                description="Provenance to set: 'operator', 'agent', or 'ingested' — an "
                "explicit value always wins (it can relabel an existing section). Omit to "
                "preserve the section's current label ('agent' on first write)."
            ),
        ] = None,
        if_match_updated_at: Annotated[
            str | None,
            Field(
                description="Optional optimistic-concurrency guard. Pass the `updated_at` "
                "value you got from get_section; the write applies only if the section has "
                "not changed since then. On a mismatch the call returns ok=False with "
                "conflict=True instead of clobbering a concurrent update. Note: updated_at "
                "has 1-second resolution, so two writes within the same wall-clock second "
                "cannot be distinguished by this guard. Omit for a blind upsert (legacy "
                "behavior)."
            ),
        ] = None,
        if_match_version: Annotated[
            int | None,
            Field(
                description="Preferred optimistic-concurrency guard. Pass the `version` "
                "from get_section; the write applies only if the section has not changed. "
                "On mismatch the call returns ok=False with conflict=True and a durable "
                "receipt_id. Omit only for legacy blind-write compatibility."
            ),
        ] = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Upsert a context section. Open to every caller; SECTION_OWNERS records the
        section's steward (returned as `owner` for display). Clobbering is guarded by
        the optimistic-concurrency check (if_match_version) + write-conflict receipts."""
        require_caller(ctx, caller, tool="update_section")
        # source_trust may be None here; None passes through the clamp and
        # preserves the stored label via COALESCE in _upsert_section.
        source_trust, source_trust_clamped = clamp_source_trust(
            source_trust, caller=caller, tool="update_section"
        )
        owner = SECTION_OWNERS.get(section_name)
        if owner is None:
            raise ToolError(
                f"Unknown section '{section_name}'. Known sections: {sorted(SECTION_OWNERS.keys())}"
            )
        # Context sections are open to every caller — no per-caller ownership gate.
        # `owner` stays the registered steward (for display); the optimistic-
        # concurrency guard + write-conflict receipts below prevent clobbering.

        db = get_db(ctx)
        result = await _upsert_section(
            db=db,
            section_name=section_name,
            owner=owner,
            content=content,
            source_trust=source_trust,
            attempted_by=caller,
            operation="update_section",
            principal=get_principal(ctx),
            if_match_updated_at=if_match_updated_at,
            if_match_version=if_match_version,
        )
        if not result["written"]:
            # Optimistic-concurrency conflict. The receipt was staged by
            # _upsert_section in the same transaction as the rejected write
            # (INV-5) — this commit makes both durable atomically.
            await db.commit()
            receipt_id, current = result["receipt_id"], result["current"]
            logger.info(
                "section update conflict: %s by %s (reason=%s receipt=%s)",
                section_name,
                caller,
                result["reason"],
                receipt_id,
            )
            return {
                "ok": False,
                "conflict": True,
                "receipt_id": receipt_id,
                "section_name": section_name,
                "owner": owner,
                "current_updated_at": current["updated_at"]
                if current is not None
                else None,
                "current_version": current["version"] if current is not None else None,
                "current_source_trust": current["source_trust"]
                if current is not None
                else None,
                "current_content_sha256": content_sha256(current["content"])
                if current is not None
                else None,
                "reason_code": result["reason"],
                "reason": (
                    "Section changed since you read it, the required CAS token was missing, "
                    "or the row was removed. Re-read it with get_section and retry "
                    "update_section with the current version as if_match_version."
                ),
            }
        await db.commit()
        # Echo the label actually stored (not the param): on a preserve-update the
        # stored value is the pre-existing label, not the None that was passed in.
        #
        # Echo semantics: this post-commit re-read reports CURRENT row state at
        # response time, not necessarily this write's image — on the shared
        # connection a concurrent writer can land between the commit above and
        # this SELECT, so `version` may be newer than if_match_version + 1.
        # Callers already treat the response as "state to CAS against next",
        # which stays correct. Rewriting the UPDATE as `... RETURNING` would
        # pin the echo to this write, but rowcount is unreliable on RETURNING
        # statements in sqlite3 and INV-4's always_tx keys on rowcount, and the
        # DST fault points key on these statement fingerprints — a cosmetic
        # echo is not worth destabilizing either (P6 review, 2026-07-10).
        cursor = await db.execute(
            "SELECT content, updated_at, source_trust, version FROM context_sections WHERE section_name = ?",
            (section_name,),
        )
        row = await cursor.fetchone()
        stored_trust = row["source_trust"] if row is not None else source_trust
        legacy_blind_write = bool(result.get("legacy_blind_write"))
        if legacy_blind_write:
            log_audit(
                "update_section.legacy_blind_write",
                caller,
                section_name,
                ok=True,
                detail=(
                    f"mode={_context_cas_mode()} receipt_id={result.get('receipt_id')}"
                ),
            )
        logger.info("section updated: %s by %s", section_name, caller)
        return {
            "ok": True,
            "section_name": section_name,
            "owner": owner,
            "updated_at": row["updated_at"] if row is not None else None,
            "version": row["version"] if row is not None else None,
            "content_sha256": content_sha256(row["content"])
            if row is not None
            else None,
            "source_trust": stored_trust,
            "source_trust_clamped": source_trust_clamped,
            "legacy_blind_write": legacy_blind_write,
        }

    @mcp.tool()
    async def get_section(
        section_name: Annotated[
            str, Field(description="Section key, e.g. 'career', 'speaking'")
        ],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Return a single context section's content and metadata."""
        db = get_db(ctx)
        cursor = await db.execute(
            """
            SELECT section_name, owner, content, updated_at, source_trust, version
            FROM context_sections
            WHERE section_name = ?
            """,
            (section_name,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ToolError(f"Section '{section_name}' not found")
        return {
            "section_name": row["section_name"],
            "owner": row["owner"],
            "content": row["content"],
            "updated_at": row["updated_at"],
            "version": row["version"],
            "content_sha256": content_sha256(row["content"]),
            "source_trust": row["source_trust"],
            "instruction_boundary": instruction_boundary(row["source_trust"]),
        }

    @mcp.tool()
    async def get_all_sections(
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Return all context sections as a dict keyed by section_name."""
        db = get_db(ctx)
        cursor = await db.execute(
            """
            SELECT section_name, owner, content, updated_at, source_trust, version
            FROM context_sections
            ORDER BY section_name
            """
        )
        rows = await cursor.fetchall()
        return {
            r["section_name"]: {
                "owner": r["owner"],
                "content": r["content"],
                "updated_at": r["updated_at"],
                "version": r["version"],
                "content_sha256": content_sha256(r["content"]),
                "source_trust": r["source_trust"],
                "instruction_boundary": instruction_boundary(r["source_trust"]),
            }
            for r in rows
        }

    @mcp.tool()
    async def sync_from_file(
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Sync Claude.ai-owned context sections from the bridge markdown file into SQLite."""
        db = get_db(ctx)
        return await sync_owned_sections_from_file(
            db=db, bridge_path=config.BRIDGE_FILE_PATH
        )
