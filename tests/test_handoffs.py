"""Tests for handoff queue tools."""

from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx
from mcp.server.fastmcp.exceptions import ToolError

from bridge_db.tools import handoffs as mod


@pytest.fixture
def fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    mod.register(cap)
    return cap.fns


async def test_create_handoff_requires_claude_ai(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    with pytest.raises(ToolError, match="claude_ai"):
        await fns["create_handoff"](caller="cc", project_name="P", ctx=ctx)


async def test_create_handoff_inserts_pending(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    result = await fns["create_handoff"](
        caller="claude_ai",
        project_name="MyProject",
        project_path="/Users/d/Projects/MyProject",
        roadmap_file="ROADMAP.md",
        phase="Phase 2",
        ctx=ctx,
    )
    assert result["ok"] is True
    assert result["status"] == "pending"

    cursor = await db.execute("SELECT * FROM pending_handoffs")
    rows: list[aiosqlite.Row] = await cursor.fetchall()  # type: ignore[assignment]
    assert len(rows) == 1
    assert rows[0]["project_name"] == "MyProject"
    assert rows[0]["status"] == "pending"


async def test_get_pending_handoffs_returns_pending_only(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["create_handoff"](caller="claude_ai", project_name="A", ctx=ctx)
    await fns["create_handoff"](caller="claude_ai", project_name="B", ctx=ctx)
    # Mark one as cleared directly
    await db.execute("UPDATE pending_handoffs SET status='cleared' WHERE project_name='A'")
    await db.commit()

    pending = await fns["get_pending_handoffs"](ctx=ctx)
    assert len(pending) == 1
    assert pending[0]["project_name"] == "B"


async def test_pick_up_handoff(db: aiosqlite.Connection, fns: dict[str, Any]) -> None:
    ctx = make_ctx(db)
    created = await fns["create_handoff"](caller="claude_ai", project_name="P", ctx=ctx)
    handoff_id = created["handoff_id"]

    result = await fns["pick_up_handoff"](caller="cc", handoff_id=handoff_id, ctx=ctx)
    assert result["status"] == "active"

    cursor = await db.execute(
        "SELECT status, picked_up_at FROM pending_handoffs WHERE id = ?", (handoff_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == "active"
    assert row["picked_up_at"] is not None


async def test_pick_up_handoff_rejects_claude_ai(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    created = await fns["create_handoff"](caller="claude_ai", project_name="P", ctx=ctx)
    with pytest.raises(ToolError):
        await fns["pick_up_handoff"](caller="claude_ai", handoff_id=created["handoff_id"], ctx=ctx)


async def test_pick_up_nonexistent_handoff_raises(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    with pytest.raises(ToolError, match="No handoff found"):
        await fns["pick_up_handoff"](caller="cc", handoff_id=9999, ctx=ctx)


async def test_clear_handoff_by_project_name(db: aiosqlite.Connection, fns: dict[str, Any]) -> None:
    ctx = make_ctx(db)
    await fns["create_handoff"](caller="claude_ai", project_name="MyProject", ctx=ctx)

    result = await fns["clear_handoff"](caller="cc", project_name="MyProject", ctx=ctx)
    assert result["ok"] is True
    assert result["cleared"] is True

    cursor = await db.execute("SELECT status FROM pending_handoffs WHERE project_name='MyProject'")
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == "cleared"


async def test_clear_handoff_missing_project_returns_ok(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    result = await fns["clear_handoff"](caller="cc", project_name="DoesNotExist", ctx=ctx)
    assert result["ok"] is True
    assert result["cleared"] is False


async def test_clear_handoff_rejects_claude_ai(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    with pytest.raises(ToolError):
        await fns["clear_handoff"](caller="claude_ai", project_name="P", ctx=ctx)
