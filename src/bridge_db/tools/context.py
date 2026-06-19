"""Context section tools: update_section, get_section, get_all_sections, sync_from_file."""

import logging
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from bridge_db import config
from bridge_db.audit import log_audit
from bridge_db.auth import auth_mode, clamp_source_trust, require_caller
from bridge_db.db import (
    fts_text_for_section,
    get_db,
    upsert_fts_entry,
)
from bridge_db.instruction_boundary import instruction_boundary
from bridge_db.models import SECTION_OWNERS, CallerID, SourceTrust, ownership_error

logger = logging.getLogger("bridge_db.tools.context")

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

    return {section_name: "\n".join(lines).strip("\n") for section_name, lines in parsed.items()}


async def _upsert_section(
    db: Any,
    section_name: str,
    owner: str,
    content: str,
    source_trust: SourceTrust | None = None,
    *,
    if_match: str | None = None,
) -> bool:
    # source_trust is COALESCEd: a fresh row with no assertion takes 'agent'; a
    # content-only update (source_trust=None) preserves the row's existing label
    # rather than relabelling operator-authored sections on a routine re-sync.
    #
    # if_match enables optimistic concurrency. When given, the write is a
    # conditional UPDATE that applies only if the stored updated_at still equals
    # if_match — i.e. the section has not changed since the caller read it. A
    # mismatch (or missing row) returns False without writing, so a stale
    # read-modify-write cannot silently clobber a concurrent update. When if_match
    # is None the historical blind upsert runs unchanged (backward compatible).
    # Returns True iff a row was written.
    if if_match is not None:
        cursor = await db.execute(
            """
            UPDATE context_sections SET
                content = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                source_trust = COALESCE(?, source_trust)
            WHERE section_name = ? AND updated_at = ?
            """,
            (content, source_trust, section_name, if_match),
        )
        if cursor.rowcount == 0:
            return False
    else:
        await db.execute(
            """
            INSERT INTO context_sections (section_name, owner, content, source_trust, updated_at)
            VALUES (?, ?, ?, COALESCE(?, 'agent'), strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            ON CONFLICT(section_name) DO UPDATE SET
                content = excluded.content,
                updated_at = excluded.updated_at,
                source_trust = COALESCE(?, context_sections.source_trust)
            """,
            (section_name, owner, content, source_trust, source_trust),
        )
    await upsert_fts_entry(db, "section", section_name, fts_text_for_section(section_name, content))
    return True


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

    for section_name in SECTION_OWNERS:
        if section_name not in parsed_sections:
            continue
        content = parsed_sections[section_name]

        if auth_active:
            cursor = await db.execute(
                "SELECT content FROM context_sections WHERE section_name = ?",
                (section_name,),
            )
            row = await cursor.fetchone()
            # Normalize the same way parse_owned_sections does (strip outer
            # newlines) so whitespace variance between the update_section write
            # path and the file parse path can't spuriously demote a section.
            if row is not None and str(row["content"]).strip("\n") == content.strip("\n"):
                unchanged.append(section_name)
                continue
            await _upsert_section(
                db=db,
                section_name=section_name,
                owner="claude_ai",
                content=content,
                source_trust="ingested",
            )
            demoted.append(section_name)
        else:
            await _upsert_section(
                db=db, section_name=section_name, owner="claude_ai", content=content
            )

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
        "count": len(synced_sections),
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def update_section(
        caller: Annotated[CallerID, Field(description="The system updating this section")],
        section_name: Annotated[
            str,
            Field(
                description="Section key, e.g. 'career', 'speaking', 'research', 'capabilities', 'portfolio'"
            ),
        ],
        content: Annotated[str, Field(description="Full markdown content for this section")],
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
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Upsert a context section. Caller must be the section owner (see SECTION_OWNERS)."""
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
        if caller != owner:
            logger.warning(
                "ownership violation: caller=%s section=%s owner=%s", caller, section_name, owner
            )
            raise ToolError(ownership_error(caller, section_name, owner))

        db = get_db(ctx)
        written = await _upsert_section(
            db=db,
            section_name=section_name,
            owner=owner,
            content=content,
            source_trust=source_trust,
            if_match=if_match_updated_at,
        )
        if not written:
            # Optimistic-concurrency conflict: the section changed since the caller
            # read it. The conditional UPDATE matched 0 rows but still opened an
            # implicit write transaction — roll it back to release the lock before
            # the diagnostic read below. This tool performs no DB write above
            # _upsert_section, so the rollback discards nothing else (keep that
            # invariant if this function is ever extended).
            await db.rollback()
            cursor = await db.execute(
                "SELECT updated_at, source_trust FROM context_sections WHERE section_name = ?",
                (section_name,),
            )
            current = await cursor.fetchone()
            logger.info(
                "section update conflict: %s by %s (stale if_match=%s)",
                section_name,
                caller,
                if_match_updated_at,
            )
            return {
                "ok": False,
                "conflict": True,
                "section_name": section_name,
                "owner": owner,
                "current_updated_at": current["updated_at"] if current is not None else None,
                "current_source_trust": current["source_trust"] if current is not None else None,
                "reason": (
                    "Section changed since you read it (optimistic-concurrency conflict) "
                    "or was removed. Re-read it with get_section and retry update_section "
                    "with the new updated_at as if_match_updated_at."
                ),
            }
        await db.commit()
        # Echo the label actually stored (not the param): on a preserve-update the
        # stored value is the pre-existing label, not the None that was passed in.
        cursor = await db.execute(
            "SELECT source_trust FROM context_sections WHERE section_name = ?", (section_name,)
        )
        row = await cursor.fetchone()
        stored_trust = row["source_trust"] if row is not None else source_trust
        logger.info("section updated: %s by %s", section_name, caller)
        return {
            "ok": True,
            "section_name": section_name,
            "owner": owner,
            "source_trust": stored_trust,
            "source_trust_clamped": source_trust_clamped,
        }

    @mcp.tool()
    async def get_section(
        section_name: Annotated[str, Field(description="Section key, e.g. 'career', 'speaking'")],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Return a single context section's content and metadata."""
        db = get_db(ctx)
        cursor = await db.execute(
            "SELECT section_name, owner, content, updated_at, source_trust FROM context_sections WHERE section_name = ?",
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
            "SELECT section_name, owner, content, updated_at, source_trust FROM context_sections ORDER BY section_name"
        )
        rows = await cursor.fetchall()
        return {
            r["section_name"]: {
                "owner": r["owner"],
                "content": r["content"],
                "updated_at": r["updated_at"],
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
        return await sync_owned_sections_from_file(db=db, bridge_path=config.BRIDGE_FILE_PATH)
