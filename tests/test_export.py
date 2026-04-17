"""Tests for the export_bridge_markdown tool (semantic fidelity, not byte-perfect)."""

from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx

from bridge_db.tools import activity as act_mod
from bridge_db.tools import context as ctx_mod
from bridge_db.tools import cost as cost_mod
from bridge_db.tools import export as exp_mod
from bridge_db.tools import handoffs as hnd_mod
from bridge_db.tools import snapshots as snap_mod
from bridge_db.tools.export import build_markdown as _build_markdown


@pytest.fixture
def all_fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    act_mod.register(cap)
    ctx_mod.register(cap)
    cost_mod.register(cap)
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


async def test_export_includes_additional_source_activity_sections(
    db: aiosqlite.Connection, all_fns: dict[str, Any]
) -> None:
    mctx = make_ctx(db)
    await all_fns["log_activity"](
        caller="notion_os",
        project_name="Notion Sync",
        summary="synced portfolio updates",
        timestamp="2026-04-15",
        ctx=mctx,
    )
    await all_fns["log_activity"](
        caller="personal_ops",
        project_name="personal-ops",
        summary="processed inbox triage",
        timestamp="2026-04-15",
        ctx=mctx,
    )

    md = await _build_markdown(db)
    assert "## Recent Notion OS Activity" in md
    assert "Notion Sync" in md
    assert "## Recent Personal Ops Activity" in md
    assert "processed inbox triage" in md


async def test_export_workflow_reflects_multi_tool_bridge_state(
    db: aiosqlite.Connection, all_fns: dict[str, Any], tmp_path: Path
) -> None:
    import bridge_db.config as cfg

    bridge_path = tmp_path / "bridge.md"
    original_bridge_path = cfg.BRIDGE_FILE_PATH
    cfg.BRIDGE_FILE_PATH = bridge_path

    mctx = make_ctx(db)
    try:
        await all_fns["update_section"](
            caller="claude_ai",
            section_name="capabilities",
            content="Capability baseline\n- direct MCP path confirmed",
            ctx=mctx,
        )
        await all_fns["create_handoff"](
            caller="claude_ai",
            project_name="bridge-db",
            project_path="/Users/d/Projects/bridge-db",
            roadmap_file="ROADMAP.md",
            phase="Phase 4 hardening",
            ctx=mctx,
        )
        await all_fns["save_snapshot"](
            caller="cc",
            data={
                "active_projects": "- bridge-db",
                "lessons": "- keep docs aligned",
                "infrastructure": "- bridge-db MCP live",
            },
            snapshot_date="2026-04-15",
            ctx=mctx,
        )
        await all_fns["save_snapshot"](
            caller="codex",
            data={
                "infrastructure": "- Skills: 35 active",
                "automation_digest": "- bridge health: healthy",
                "active_projects": "- bridge-db",
            },
            snapshot_date="2026-04-15",
            ctx=mctx,
        )
        await all_fns["record_cost"](
            caller="cc",
            month="2026-04",
            amount=125.0,
            ctx=mctx,
        )
        await all_fns["log_activity"](
            caller="cc",
            project_name="bridge-db",
            summary="validated Phase 4 hardening plan",
            tags=["SHIPPED"],
            timestamp="2026-04-15",
            ctx=mctx,
        )
        await all_fns["log_activity"](
            caller="codex",
            project_name="bridge-db",
            summary="captured architectural decision",
            timestamp="2026-04-15",
            ctx=mctx,
        )
        await all_fns["export_bridge_markdown"](ctx=mctx)
    finally:
        cfg.BRIDGE_FILE_PATH = original_bridge_path

    content = bridge_path.read_text(encoding="utf-8")
    assert "Capability baseline" in content
    assert "Phase 4 hardening" in content
    assert "validated Phase 4 hardening plan" in content
    assert "captured architectural decision" in content
    assert "### Cost (ccusage)" in content
    assert "$125" in content


async def test_sync_from_file_then_export_preserves_fallback_context_and_live_state(
    db: aiosqlite.Connection, all_fns: dict[str, Any], tmp_path: Path
) -> None:
    import bridge_db.config as cfg

    bridge_path = tmp_path / "claude_ai_context.md"
    bridge_path.write_text(
        """# Claude.ai <-> Claude Code <-> Codex Context Bridge

## Career & Professional Target
Fallback career context

## Speaking Engagements
Fallback speaking context

## Active Research Themes
Fallback research context

## Claude.ai Capabilities Summary
Fallback capability context

## Pending Handoffs
- stale handoff content from file should not be re-imported
""",
        encoding="utf-8",
    )

    original_bridge_path = cfg.BRIDGE_FILE_PATH
    cfg.BRIDGE_FILE_PATH = bridge_path

    mctx = make_ctx(db)
    try:
        sync_result = await all_fns["sync_from_file"](ctx=mctx)
        assert sync_result["count"] == 4

        await all_fns["create_handoff"](
            caller="claude_ai",
            project_name="bridge-db",
            project_path="/Users/d/Projects/bridge-db",
            phase="Phase 4 hardening",
            ctx=mctx,
        )
        await all_fns["log_activity"](
            caller="cc",
            project_name="bridge-db",
            summary="validated startup sync fallback path",
            timestamp="2026-04-15",
            ctx=mctx,
        )
        await all_fns["export_bridge_markdown"](ctx=mctx)
    finally:
        cfg.BRIDGE_FILE_PATH = original_bridge_path

    content = bridge_path.read_text(encoding="utf-8")
    assert "Fallback capability context" in content
    assert "Fallback research context" in content
    assert "Phase 4 hardening" in content
    assert "validated startup sync fallback path" in content
    assert "stale handoff content from file should not be re-imported" not in content


async def test_sync_status_and_export_capture_cross_client_state(
    db: aiosqlite.Connection, tmp_path: Path
) -> None:
    import bridge_db.config as cfg
    from bridge_db.tools import health as health_mod

    bridge_path = tmp_path / "claude_ai_context.md"
    bridge_path.write_text(
        """# Claude.ai <-> Claude Code <-> Codex Context Bridge

## Career & Professional Target
Operator-ready bridge role

## Speaking Engagements
Bridge talk prep

## Active Research Themes
Shared-state sync

## Claude.ai Capabilities Summary
Prefers MCP when available
""",
        encoding="utf-8",
    )

    original_bridge_path = cfg.BRIDGE_FILE_PATH
    cfg.BRIDGE_FILE_PATH = bridge_path

    cap = CaptureMCP()
    act_mod.register(cap)
    ctx_mod.register(cap)
    cost_mod.register(cap)
    exp_mod.register(cap)
    hnd_mod.register(cap)
    health_mod.register(cap)
    snap_mod.register(cap)

    mctx = make_ctx(db)
    try:
        await cap.fns["sync_from_file"](ctx=mctx)
        await cap.fns["create_handoff"](
            caller="claude_ai",
            project_name="bridge-db",
            project_path="/Users/d/Projects/bridge-db",
            phase="Phase 5 operator readiness",
            ctx=mctx,
        )
        await cap.fns["save_snapshot"](
            caller="codex",
            data={
                "infrastructure": "- bridge-db status tool live",
                "automation_digest": "- no automation drift detected",
                "active_projects": "- bridge-db",
            },
            snapshot_date="2026-04-17",
            ctx=mctx,
        )
        await cap.fns["log_activity"](
            caller="personal_ops",
            project_name="personal-ops",
            summary="checked bridge handoff inbox",
            timestamp="2026-04-17",
            ctx=mctx,
        )
        status_result = await cap.fns["status"](ctx=mctx)
        await cap.fns["export_bridge_markdown"](ctx=mctx)
    finally:
        cfg.BRIDGE_FILE_PATH = original_bridge_path

    content = bridge_path.read_text(encoding="utf-8")
    assert status_result["ok"] is True
    assert status_result["signals"]["pending_handoffs"] == 1
    assert status_result["latest_snapshots"]["codex"] == "2026-04-17"
    assert status_result["latest_activity"]["personal_ops"] == "2026-04-17 (personal-ops)"
    assert "Operator-ready bridge role" in content
    assert "Phase 5 operator readiness" in content
    assert "## Recent Personal Ops Activity" in content