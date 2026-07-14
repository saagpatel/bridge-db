"""Tests for the export_bridge_markdown tool (semantic fidelity, not byte-perfect)."""

from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx

from bridge_db.db import insert_activity_row
from bridge_db.tools import activity as act_mod
from bridge_db.tools import context as ctx_mod
from bridge_db.tools import cost as cost_mod
from bridge_db.tools import export as exp_mod
from bridge_db.tools import handoffs as hnd_mod
from bridge_db.tools import snapshots as snap_mod
from bridge_db.tools.export import build_markdown as _build_markdown


def _always_claude_home_bridge_path(_path: Path) -> bool:
    return True


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
    mctx = make_ctx(db, principal="claude_ai")
    await all_fns["create_handoff"](
        caller="claude_ai",
        project_name="MyProject",
        project_path="/home/user/Projects/MyProject",
        phase="Phase 3",
        ctx=mctx,
    )
    md = await _build_markdown(db)
    assert "MyProject" in md
    assert "Phase 3" in md


async def test_export_includes_section_content(
    db: aiosqlite.Connection, all_fns: dict[str, Any]
) -> None:
    mctx = make_ctx(db, principal="claude_ai")
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

    mctx = make_ctx(db, principal="claude_ai")
    result = await all_fns["export_bridge_markdown"](ctx=mctx)
    assert result["ok"] is True
    assert cfg.BRIDGE_FILE_PATH.exists()
    content = cfg.BRIDGE_FILE_PATH.read_text()
    assert "Claude Code State Snapshot" in content

    cfg.BRIDGE_FILE_PATH = original  # restore


async def test_export_records_context_section_export_state(
    db: aiosqlite.Connection, all_fns: dict[str, Any], tmp_path: Path
) -> None:
    import bridge_db.config as cfg

    original = cfg.BRIDGE_FILE_PATH
    cfg.BRIDGE_FILE_PATH = tmp_path / "bridge.md"
    try:
        ctx = make_ctx(db, principal="claude_ai")
        await all_fns["update_section"](
            caller="claude_ai",
            section_name="career",
            content="exported career",
            ctx=ctx,
        )
        result = await all_fns["export_bridge_markdown"](ctx=ctx)
    finally:
        cfg.BRIDGE_FILE_PATH = original

    assert result["exported_context_sections"] == 1
    cursor = await db.execute(
        """
        SELECT s.version, e.exported_version, e.exported_content_sha256
        FROM context_sections AS s
        JOIN context_section_export_state AS e ON e.section_name = s.section_name
        WHERE s.section_name = 'career'
        """
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["exported_version"] == row["version"]
    assert row["exported_content_sha256"]


async def test_export_frontmatter_present(db: aiosqlite.Connection) -> None:
    md = await _build_markdown(db)
    assert "---" in md
    assert "name: claude_ai_context" in md
    assert "type: reference" in md


async def test_export_includes_additional_source_activity_sections(
    db: aiosqlite.Connection, all_fns: dict[str, Any]
) -> None:
    mctx = make_ctx(db, principal="claude_ai")
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

    mctx = make_ctx(db, principal="claude_ai")
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
            project_path="/home/user/Projects/bridge-db",
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


async def test_export_uses_latest_codex_operating_snapshot(
    db: aiosqlite.Connection, all_fns: dict[str, Any]
) -> None:
    mctx = make_ctx(db, principal="claude_ai")
    await all_fns["save_snapshot"](
        caller="codex",
        data={
            "infrastructure": "- bridge health: green",
            "automation_digest": "- no automation drift detected",
            "active_projects": "- bridge-db",
        },
        snapshot_date="2026-04-15",
        ctx=mctx,
    )
    await all_fns["save_snapshot"](
        caller="codex",
        data={
            "consulted_node": {
                "latest_consultation": "newer advisory metadata, not bridge state"
            }
        },
        snapshot_date="2026-04-16",
        ctx=mctx,
    )

    md = await _build_markdown(db)
    assert "## Codex State Snapshot" in md
    assert "Last exported: 2026-04-15" in md
    assert "- bridge health: green" in md
    assert "- bridge-db" in md
    assert "newer advisory metadata" not in md


async def test_export_uses_latest_cc_snapshot_when_created_at_ties(
    db: aiosqlite.Connection, all_fns: dict[str, Any]
) -> None:
    mctx = make_ctx(db)
    await all_fns["save_snapshot"](
        caller="cc",
        data={"active_projects": "OLD SNAPSHOT MARKER"},
        snapshot_date="2026-06-01",
        ctx=mctx,
    )
    await all_fns["save_snapshot"](
        caller="cc",
        data={"active_projects": "NEW SNAPSHOT MARKER"},
        snapshot_date="2026-06-02",
        ctx=mctx,
    )
    await db.execute(
        "UPDATE system_snapshots SET created_at = '2026-06-01T00:00:00Z' WHERE system = 'cc'"
    )
    await db.commit()

    md = await _build_markdown(db)

    assert "NEW SNAPSHOT MARKER" in md
    assert "OLD SNAPSHOT MARKER" not in md


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

    mctx = make_ctx(db, principal="claude_ai")
    try:
        sync_result = await all_fns["sync_from_file"](ctx=mctx)
        assert sync_result["count"] == 4

        await all_fns["create_handoff"](
            caller="claude_ai",
            project_name="bridge-db",
            project_path="/home/user/Projects/bridge-db",
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

    mctx = make_ctx(db, principal="claude_ai")
    try:
        await cap.fns["sync_from_file"](ctx=mctx)
        await cap.fns["create_handoff"](
            caller="claude_ai",
            project_name="bridge-db",
            project_path="/home/user/Projects/bridge-db",
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
    assert (
        status_result["latest_activity"]["personal_ops"] == "2026-04-17 (personal-ops)"
    )
    assert "Operator-ready bridge role" in content
    assert "Phase 5 operator readiness" in content
    assert "## Recent Personal Ops Activity" in content


def test_write_bridge_file_writes_content_and_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "claude_ai_context.md"
    monkeypatch.setattr(exp_mod.config, "BRIDGE_FILE_PATH", target)

    exp_mod.write_bridge_file("hello bridge\n")

    assert target.read_text(encoding="utf-8") == "hello bridge\n"
    # the atomic temp file must not linger in the watched directory
    assert [p.name for p in tmp_path.iterdir()] == ["claude_ai_context.md"]


def test_write_bridge_file_rejects_empty_core_export_to_claude_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "claude_ai_context.md"
    monkeypatch.setattr(exp_mod.config, "BRIDGE_FILE_PATH", target)
    monkeypatch.setattr(
        exp_mod, "_is_claude_home_bridge_path", _always_claude_home_bridge_path
    )
    content = "\n".join(
        [
            "## Career & Professional Target",
            "_Not yet populated._",
            "## Speaking Engagements",
            "_Not yet populated._",
            "## Active Research Themes",
            "_Not yet populated._",
            "## Claude.ai Capabilities Summary",
            "_Not yet populated._",
        ]
    )

    with pytest.raises(exp_mod.BridgeExportSafetyError, match="all placeholders"):
        exp_mod.write_bridge_file(content)

    assert not target.exists()


def test_write_bridge_file_allows_empty_core_export_to_non_fallback_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "claude_ai_context.md"
    monkeypatch.setattr(exp_mod.config, "BRIDGE_FILE_PATH", target)
    content = "\n".join(
        [
            "## Career & Professional Target",
            "_Not yet populated._",
            "## Speaking Engagements",
            "_Not yet populated._",
            "## Active Research Themes",
            "_Not yet populated._",
            "## Claude.ai Capabilities Summary",
            "_Not yet populated._",
        ]
    )

    exp_mod.write_bridge_file(content)

    assert target.read_text(encoding="utf-8") == content


def test_write_bridge_file_allows_intentional_empty_fallback_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "claude_ai_context.md"
    monkeypatch.setattr(exp_mod.config, "BRIDGE_FILE_PATH", target)
    monkeypatch.setattr(
        exp_mod, "_is_claude_home_bridge_path", _always_claude_home_bridge_path
    )
    monkeypatch.setenv("BRIDGE_DB_ALLOW_EMPTY_BRIDGE_EXPORT", "1")
    content = "\n".join(
        [
            "## Career & Professional Target",
            "_Not yet populated._",
            "## Speaking Engagements",
            "_Not yet populated._",
            "## Active Research Themes",
            "_Not yet populated._",
            "## Claude.ai Capabilities Summary",
            "_Not yet populated._",
        ]
    )

    exp_mod.write_bridge_file(content)

    assert target.read_text(encoding="utf-8") == content


def test_write_bridge_file_allows_populated_core_export_to_claude_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "claude_ai_context.md"
    monkeypatch.setattr(exp_mod.config, "BRIDGE_FILE_PATH", target)
    monkeypatch.setattr(
        exp_mod, "_is_claude_home_bridge_path", _always_claude_home_bridge_path
    )
    content = "\n".join(
        [
            "## Career & Professional Target",
            "real career context",
            "## Speaking Engagements",
            "_Not yet populated._",
            "## Active Research Themes",
            "_Not yet populated._",
            "## Claude.ai Capabilities Summary",
            "_Not yet populated._",
        ]
    )

    exp_mod.write_bridge_file(content)

    assert target.read_text(encoding="utf-8") == content


def test_write_bridge_file_atomic_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "claude_ai_context.md"
    target.write_text("original\n", encoding="utf-8")
    monkeypatch.setattr(exp_mod.config, "BRIDGE_FILE_PATH", target)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(exp_mod.os, "replace", boom)

    with pytest.raises(OSError, match="simulated replace failure"):
        exp_mod.write_bridge_file("new content that must not partially land\n")

    # atomicity: the original file is untouched and the temp file is cleaned up
    assert target.read_text(encoding="utf-8") == "original\n"
    assert [p.name for p in tmp_path.iterdir()] == ["claude_ai_context.md"]


async def test_export_renders_pinned_ledger_section(
    db: aiosqlite.Connection, all_fns: dict[str, Any]
) -> None:
    await insert_activity_row(
        db,
        source="cc",
        timestamp="2026-01-01",
        project_name="p",
        summary="durable milestone",
        tags=["LEDGER"],
    )
    await db.commit()
    md = await _build_markdown(db)
    assert "## Pinned Ledger" in md
    assert "durable milestone" in md


async def test_export_pinned_ledger_caps_cross_source_rows_newest_first(
    db: aiosqlite.Connection, all_fns: dict[str, Any]
) -> None:
    for index in range(16):
        await insert_activity_row(
            db,
            source="cc" if index % 2 == 0 else "codex",
            timestamp=f"2026-01-{index + 1:02d}",
            project_name=f"project-{index % 3}",
            summary=f"pinned ledger {index:02d}",
            tags=["SHIPPED" if index % 2 == 0 else "LEDGER"],
        )
    await db.commit()

    md = await _build_markdown(db)
    pinned_section = md.split("## Pinned Ledger\n", 1)[1]
    pinned_lines = [line for line in pinned_section.splitlines() if "pinned ledger" in line]

    assert len(pinned_lines) == 15
    assert "pinned ledger 00" not in pinned_section
    assert "pinned ledger 15" in pinned_lines[0]
    assert "pinned ledger 01" in pinned_lines[-1]


async def test_export_omits_pinned_ledger_when_empty(
    db: aiosqlite.Connection, all_fns: dict[str, Any]
) -> None:
    md = await _build_markdown(db)
    assert "## Pinned Ledger" not in md
