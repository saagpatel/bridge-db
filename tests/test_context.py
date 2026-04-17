"""Tests for context section tools."""

from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx
from mcp.server.fastmcp.exceptions import ToolError

import bridge_db.config as cfg
from bridge_db.tools import context as mod


@pytest.fixture
def fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    mod.register(cap)
    return cap.fns


async def test_update_section_owner_can_write(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    result = await fns["update_section"](
        caller="claude_ai", section_name="career", content="# Career\nSoftware engineer", ctx=ctx
    )
    assert result["ok"] is True
    assert result["owner"] == "claude_ai"


async def test_update_section_wrong_caller_raises(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    with pytest.raises(ToolError, match="Ownership violation"):
        await fns["update_section"](caller="cc", section_name="career", content="...", ctx=ctx)


async def test_update_section_unknown_section_raises(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    with pytest.raises(ToolError, match="Unknown section"):
        await fns["update_section"](caller="cc", section_name="nonexistent", content="...", ctx=ctx)


async def test_update_section_is_upsert(db: aiosqlite.Connection, fns: dict[str, Any]) -> None:
    ctx = make_ctx(db)
    await fns["update_section"](caller="claude_ai", section_name="career", content="v1", ctx=ctx)
    await fns["update_section"](caller="claude_ai", section_name="career", content="v2", ctx=ctx)

    cursor = await db.execute("SELECT COUNT(*) FROM context_sections WHERE section_name='career'")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 1  # only one row, not two

    section = await fns["get_section"](section_name="career", ctx=ctx)
    assert section["content"] == "v2"


async def test_get_section_not_found_raises(db: aiosqlite.Connection, fns: dict[str, Any]) -> None:
    ctx = make_ctx(db)
    with pytest.raises(ToolError, match="not found"):
        await fns["get_section"](section_name="career", ctx=ctx)


async def test_get_all_sections(db: aiosqlite.Connection, fns: dict[str, Any]) -> None:
    ctx = make_ctx(db)
    await fns["update_section"](caller="claude_ai", section_name="career", content="c1", ctx=ctx)
    await fns["update_section"](caller="claude_ai", section_name="speaking", content="c2", ctx=ctx)

    all_sections = await fns["get_all_sections"](ctx=ctx)
    assert "career" in all_sections
    assert "speaking" in all_sections
    assert all_sections["career"]["content"] == "c1"
    assert all_sections["speaking"]["owner"] == "claude_ai"


async def test_all_owned_sections_accept_correct_caller(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    for section in ("career", "speaking", "research", "capabilities"):
        result = await fns["update_section"](
            caller="claude_ai", section_name=section, content="content", ctx=ctx
        )
        assert result["ok"] is True


def test_parse_owned_sections_extracts_only_claude_ai_sections() -> None:
    markdown = """# Claude.ai <-> Claude Code <-> Codex Context Bridge
Last synced: 2026-04-15

## Career & Professional Target
Career body

## Speaking Engagements
Speaking body

## Active Research Themes
Research body

## Claude.ai Capabilities Summary
Capabilities body

## Pending Handoffs
- Ignore me

## Claude Code State Snapshot
_Do not import_

## Recent Codex Activity
_Do not import_
"""

    parsed = mod.parse_owned_sections(markdown)

    assert parsed == {
        "career": "Career body",
        "speaking": "Speaking body",
        "research": "Research body",
        "capabilities": "Capabilities body",
    }


async def test_sync_from_file_upserts_owned_sections(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    bridge_file = tmp_path / "claude_ai_context.md"
    bridge_file.write_text(
        """# Claude.ai <-> Claude Code <-> Codex Context Bridge

## Career & Professional Target
Current role details

## Speaking Engagements
Upcoming talk details

## Active Research Themes
Research notes

## Claude.ai Capabilities Summary
Capability notes

## Pending Handoffs
- Handoff that should be ignored
""",
        encoding="utf-8",
    )

    original = cfg.BRIDGE_FILE_PATH
    cfg.BRIDGE_FILE_PATH = bridge_file
    try:
        result = await fns["sync_from_file"](ctx=make_ctx(db))
    finally:
        cfg.BRIDGE_FILE_PATH = original

    assert result["ok"] is True
    assert result["count"] == 4
    assert result["sections_synced"] == ["career", "speaking", "research", "capabilities"]

    cursor = await db.execute(
        "SELECT section_name, owner, content FROM context_sections ORDER BY section_name"
    )
    rows = await cursor.fetchall()
    assert [(row["section_name"], row["owner"], row["content"]) for row in rows] == [
        ("capabilities", "claude_ai", "Capability notes"),
        ("career", "claude_ai", "Current role details"),
        ("research", "claude_ai", "Research notes"),
        ("speaking", "claude_ai", "Upcoming talk details"),
    ]


async def test_sync_from_file_is_idempotent(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    bridge_file = tmp_path / "claude_ai_context.md"
    bridge_file.write_text(
        """## Career & Professional Target
v1

## Speaking Engagements
v2

## Active Research Themes
v3

## Claude.ai Capabilities Summary
v4
""",
        encoding="utf-8",
    )

    first = await mod.sync_owned_sections_from_file(db=db, bridge_path=bridge_file)
    second = await mod.sync_owned_sections_from_file(db=db, bridge_path=bridge_file)

    assert first["count"] == 4
    assert second["count"] == 4

    cursor = await db.execute("SELECT COUNT(*) FROM context_sections")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 4


async def test_sync_from_file_skips_non_owned_sections(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    bridge_file = tmp_path / "claude_ai_context.md"
    bridge_file.write_text(
        """## Pending Handoffs
- should not sync

## Claude Code State Snapshot
cc data

## Recent Claude Code Activity
- activity

## Codex State Snapshot
codex data

## Recent Codex Activity
- activity
""",
        encoding="utf-8",
    )

    result = await mod.sync_owned_sections_from_file(db=db, bridge_path=bridge_file)

    assert result["sections_synced"] == []
    assert result["count"] == 0

    cursor = await db.execute("SELECT COUNT(*) FROM context_sections")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 0