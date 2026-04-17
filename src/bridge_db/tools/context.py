"""Context section tools: update_section, get_section, get_all_sections, sync_from_file."""

import logging
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from bridge_db import config
from bridge_db.db import get_db
from bridge_db.models import SECTION_OWNERS, CallerID, ownership_error

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

    return {
        section_name: "\n".join(lines).strip("\n")
        for section_name, lines in parsed.items()
    }


async def _upsert_section(db: Any, section_name: str, owner: str, content: str) -> None:
    await db.execute(
        """
        INSERT INTO context_sections (section_name, owner, content, updated_at)
        VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        ON CONFLICT(section_name) DO UPDATE SET
            content = excluded.content,
            updated_at = excluded.updated_at
        """,
        (section_name, owner, content),
    )


async def sync_owned_sections_from_file(db: Any, bridge_path: Path) -> dict[str, Any]:
    """Read the bridge file and upsert the Claude.ai-owned context sections."""
    if not bridge_path.exists():
        raise ToolError(f"Bridge file not found: {bridge_path}")

    parsed_sections = parse_owned_sections(bridge_path.read_text(encoding="utf-8"))
    synced_sections: list[str] = []

    for section_name in SECTION_OWNERS:
        if section_name not in parsed_sections:
            continue

        await _upsert_section(
            db=db,
            section_name=section_name,
            owner="claude_ai",
            content=parsed_sections[section_name],
        )
        synced_sections.append(section_name)

    await db.commit()
    logger.info("synced %d claude_ai section(s) from %s", len(synced_sections), bridge_path)
    return {
        "ok": True,
        "path": str(bridge_path),
        "sections_synced": synced_sections,
        "count": len(synced_sections),
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def update_section(
        caller: Annotated[CallerID, Field(description="The system updating this section")],
        section_name: Annotated[
            str,
            Field(description="Section key, e.g. 'career', 'speaking', 'research', 'capabilities'"),
        ],
        content: Annotated[str, Field(description="Full markdown content for this section")],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Upsert a context section. Caller must be the section owner (see SECTION_OWNERS)."""
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
        await _upsert_section(db=db, section_name=section_name, owner=owner, content=content)
        await db.commit()
        logger.info("section updated: %s by %s", section_name, caller)
        return {"ok": True, "section_name": section_name, "owner": owner}

    @mcp.tool()
    async def get_section(
        section_name: Annotated[str, Field(description="Section key, e.g. 'career', 'speaking'")],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Return a single context section's content and metadata."""
        db = get_db(ctx)
        cursor = await db.execute(
            "SELECT section_name, owner, content, updated_at FROM context_sections WHERE section_name = ?",
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
        }

    @mcp.tool()
    async def get_all_sections(
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Return all context sections as a dict keyed by section_name."""
        db = get_db(ctx)
        cursor = await db.execute(
            "SELECT section_name, owner, content, updated_at FROM context_sections ORDER BY section_name"
        )
        rows = await cursor.fetchall()
        return {
            r["section_name"]: {
                "owner": r["owner"],
                "content": r["content"],
                "updated_at": r["updated_at"],
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