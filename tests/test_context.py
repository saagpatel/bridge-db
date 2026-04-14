"""Tests for context section tools."""

from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx
from mcp.server.fastmcp.exceptions import ToolError

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
