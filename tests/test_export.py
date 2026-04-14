"""Tests for the export_bridge_markdown tool (semantic fidelity, not byte-perfect)."""

from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx

from bridge_db.tools import activity as act_mod
from bridge_db.tools import context as ctx_mod
from bridge_db.tools import export as exp_mod
from bridge_db.tools import handoffs as hnd_mod
from bridge_db.tools import snapshots as snap_mod
from bridge_db.tools.export import build_markdown as _build_markdown


@pytest.fixture
def all_fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    act_mod.register(cap)
    ctx_mod.register(cap)
    exp_mod.register(cap)
    hnd_mod.register(cap)
    snap_mod.register(cap)
    return cap.fns


async def test_export_contains_all_section_headings(
    db: aiosqlite.Connection, all_fns: dict[str, Any]
) -> None:
    md = await _build_markdown(db)
    for heading in [
        "## Career & Professional Target",
        "## Speaking Engagements",
        "## Active Research Themes",
        "## Claude.ai Capabilities Summary",
        "## Pending Handoffs",
        "## Claude Code State Snapshot",
        "## Recent Claude Code Activity",
        "## Codex State Snapshot",
        "## Recent Codex Activity",
    ]:
        assert heading in md, f"Missing heading: {heading}"


async def test_export_includes_activity_entries(
    db: aiosqlite.Connection, all_fns: dict[str, Any]
) -> None:
    mctx = make_ctx(db)
    await all_fns["log_activity"](
        caller="cc",
        project_name="bridge-db",
        summary="Phase 0 complete",
        branch="feat/scaffold",
        tags=["SHIPPED"],
        timestamp="2026-04-14",
        ctx=mctx,
    )
    md = await _build_markdown(db)
    assert "bridge-db" in md
    assert "Phase 0 complete" in md
    assert "[SHIPPED]" in md


async def test_export_includes_pending_handoffs(
    db: aiosqlite.Connection, all_fns: dict[str, Any]
) -> None:
    mctx = make_ctx(db)
    await all_fns["create_handoff"](
        caller="claude_ai",
        project_name="MyProject",
        project_path="/Users/d/Projects/MyProject",
        phase="Phase 3",
        ctx=mctx,
    )
    md = await _build_markdown(db)
    assert "MyProject" in md
    assert "Phase 3" in md


async def test_export_includes_section_content(
    db: aiosqlite.Connection, all_fns: dict[str, Any]
) -> None:
    mctx = make_ctx(db)
    await all_fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="**Target:** Staff Engineer at a top AI lab",
        ctx=mctx,
    )
    md = await _build_markdown(db)
    assert "Staff Engineer" in md


async def test_export_writes_to_file(
    db: aiosqlite.Connection, all_fns: dict[str, Any], tmp_path: Path
) -> None:
    import bridge_db.config as cfg

    original = cfg.BRIDGE_FILE_PATH
    cfg.BRIDGE_FILE_PATH = tmp_path / "bridge.md"

    mctx = make_ctx(db)
    result = await all_fns["export_bridge_markdown"](ctx=mctx)
    assert result["ok"] is True
    assert cfg.BRIDGE_FILE_PATH.exists()
    content = cfg.BRIDGE_FILE_PATH.read_text()
    assert "Claude Code State Snapshot" in content

    cfg.BRIDGE_FILE_PATH = original  # restore


async def test_export_frontmatter_present(db: aiosqlite.Connection) -> None:
    md = await _build_markdown(db)
    assert "---" in md
    assert "name: claude_ai_context" in md
    assert "type: reference" in md
