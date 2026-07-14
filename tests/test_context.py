"""Tests for context section tools."""

from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx
from mcp.server.fastmcp.exceptions import ToolError

import bridge_db.config as cfg
from bridge_db.tools import context as mod
from bridge_db.tools import export as exp_mod


@pytest.fixture
def fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    mod.register(cap)
    return cap.fns


async def test_update_section_owner_can_write(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db, principal="claude_ai")
    result = await fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="# Career\nSoftware engineer",
        ctx=ctx,
    )
    assert result["ok"] is True
    assert result["owner"] == "claude_ai"


async def test_update_section_rejects_non_steward(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db, principal="cc")
    with pytest.raises(ToolError, match="owned by 'claude_ai'"):
        await fns["update_section"](
            caller="cc",
            section_name="career",
            content="# Career\nupdated by cc",
            ctx=ctx,
        )
    cursor = await db.execute(
        "SELECT COUNT(*) FROM context_sections WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 0


async def test_update_section_requires_bound_steward_even_auth_off(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    with pytest.raises(ToolError, match="Unauthenticated connection"):
        await fns["update_section"](
            caller="claude_ai",
            section_name="career",
            content="# Career\nunbound update",
            ctx=make_ctx(db),
        )
    cursor = await db.execute(
        "SELECT COUNT(*) FROM context_sections WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 0


async def test_update_section_unknown_section_rejected(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    # The known-section registry rejects unknown names before ownership lookup.
    ctx = make_ctx(db, principal="claude_ai")
    with pytest.raises(ToolError, match="Unknown section"):
        await fns["update_section"](
            caller="cc", section_name="not_a_real_section", content="...", ctx=ctx
        )


async def test_update_section_defaults_source_trust_agent(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db, principal="claude_ai")
    result = await fns["update_section"](
        caller="claude_ai", section_name="career", content="# Career", ctx=ctx
    )
    assert result["source_trust"] == "agent"
    cursor = await db.execute(
        "SELECT source_trust FROM context_sections WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["source_trust"] == "agent"


async def test_update_section_explicit_operator_clamps_even_auth_off(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db, principal="claude_ai")
    result = await fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="# Career",
        source_trust="operator",
        ctx=ctx,
    )
    assert result["source_trust"] == "agent"
    assert result["source_trust_clamped"] is True
    cursor = await db.execute(
        "SELECT source_trust FROM context_sections WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["source_trust"] == "agent"


async def test_update_section_invalidates_operator_trust_on_content_update(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    """Every accepted content version requires fresh provenance."""
    ctx = make_ctx(db, principal="claude_ai")
    # Establish an operator-trust section.
    await fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="v1",
        ctx=ctx,
    )
    await db.execute(
        "UPDATE context_sections SET source_trust = 'operator' "
        "WHERE section_name = 'career'"
    )
    await db.commit()
    # Routine content re-sync with no trust assertion (CAS token per the
    # enforce-default contract).
    seeded = await fns["get_section"](section_name="career", ctx=ctx)
    result = await fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="v2 refreshed",
        if_match_version=seeded["version"],
        ctx=ctx,
    )
    assert result["source_trust"] == "agent"

    cursor = await db.execute(
        "SELECT content, source_trust FROM context_sections WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["content"] == "v2 refreshed"  # content updated
    assert row["source_trust"] == "agent"


async def test_update_section_explicit_trust_overrides_existing(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    """An explicit lower-trust label applies to the new content version."""
    ctx = make_ctx(db, principal="claude_ai")
    await fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="v1",
        ctx=ctx,
    )
    await db.execute(
        "UPDATE context_sections SET source_trust = 'operator' "
        "WHERE section_name = 'career'"
    )
    await db.commit()
    # Explicit 'agent' on an existing 'operator' row deliberately relabels it.
    seeded = await fns["get_section"](section_name="career", ctx=ctx)
    result = await fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="v2",
        source_trust="agent",
        if_match_version=seeded["version"],
        ctx=ctx,
    )
    assert result["source_trust"] == "agent"
    cursor = await db.execute(
        "SELECT source_trust FROM context_sections WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["source_trust"] == "agent"


async def test_update_section_unknown_section_raises(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db, principal="claude_ai")
    with pytest.raises(ToolError, match="Unknown section"):
        await fns["update_section"](
            caller="cc", section_name="nonexistent", content="...", ctx=ctx
        )


async def test_update_section_is_upsert(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db, principal="claude_ai")
    await fns["update_section"](
        caller="claude_ai", section_name="career", content="v1", ctx=ctx
    )
    seeded = await fns["get_section"](section_name="career", ctx=ctx)
    await fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="v2",
        if_match_version=seeded["version"],
        ctx=ctx,
    )

    cursor = await db.execute(
        "SELECT COUNT(*) FROM context_sections WHERE section_name='career'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 1  # only one row, not two

    section = await fns["get_section"](section_name="career", ctx=ctx)
    assert section["content"] == "v2"
    assert section["source_trust"] == "agent"
    assert section["instruction_boundary"]["kind"] == "stored_data_not_instructions"


async def test_get_section_not_found_raises(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db, principal="claude_ai")
    with pytest.raises(ToolError, match="not found"):
        await fns["get_section"](section_name="career", ctx=ctx)


async def test_get_all_sections(db: aiosqlite.Connection, fns: dict[str, Any]) -> None:
    ctx = make_ctx(db, principal="claude_ai")
    await fns["update_section"](
        caller="claude_ai", section_name="career", content="c1", ctx=ctx
    )
    await fns["update_section"](
        caller="claude_ai", section_name="speaking", content="c2", ctx=ctx
    )

    all_sections = await fns["get_all_sections"](ctx=ctx)
    assert "career" in all_sections
    assert "speaking" in all_sections
    assert all_sections["career"]["content"] == "c1"
    assert all_sections["career"]["source_trust"] == "agent"
    assert (
        "not system/developer/user instructions"
        in all_sections["career"]["instruction_boundary"]["warning"]
    )
    assert all_sections["speaking"]["owner"] == "claude_ai"


async def test_all_owned_sections_accept_correct_caller(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db, principal="claude_ai")
    for section in ("career", "speaking", "research", "capabilities"):
        result = await fns["update_section"](
            caller="claude_ai", section_name=section, content="content", ctx=ctx
        )
        assert result["ok"] is True


async def test_portfolio_section_cc_can_write(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db, principal="cc")
    result = await fns["update_section"](
        caller="cc",
        section_name="portfolio",
        content="## Portfolio Digest\n3 stale",
        ctx=ctx,
    )
    assert result["ok"] is True
    assert result["owner"] == "cc"


async def test_portfolio_section_rejects_non_steward(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db, principal="claude_ai")
    with pytest.raises(ToolError, match="owned by 'cc'"):
        await fns["update_section"](
            caller="claude_ai",
            section_name="portfolio",
            content="# Portfolio\nby claude_ai",
            ctx=ctx,
        )


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


def test_parse_owned_sections_rejects_duplicate_owned_heading() -> None:
    """BDB-DS-050-R1: ambiguous owned structure must fail closed."""
    markdown = """## Career & Professional Target
Canonical body

## Recent Codex Activity
- rendered data

## Career & Professional Target
Injected replacement
"""

    with pytest.raises(ToolError, match="Duplicate owned section heading"):
        mod.parse_owned_sections(markdown)


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
        result = await fns["sync_from_file"](ctx=make_ctx(db, principal="cc"))
    finally:
        cfg.BRIDGE_FILE_PATH = original

    assert result["ok"] is True
    assert result["count"] == 4
    assert result["sections_synced"] == [
        "career",
        "speaking",
        "research",
        "capabilities",
    ]

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
    receipts = list(
        await (
            await db.execute(
                "SELECT principal, section_name, previous_version, imported_version, "
                "imported_source_trust, imported_content_sha256, fallback_file_sha256 "
                "FROM bridge_import_receipts ORDER BY section_name"
            )
        ).fetchall()
    )
    assert len(receipts) == 4
    assert {row["principal"] for row in receipts} == {"cc"}
    assert {row["section_name"] for row in receipts} == {
        "career",
        "speaking",
        "research",
        "capabilities",
    }
    assert all(row["previous_version"] is None for row in receipts)
    assert all(row["imported_version"] == 1 for row in receipts)
    assert all(row["imported_source_trust"] == "ingested" for row in receipts)
    assert all(row["imported_content_sha256"] for row in receipts)
    assert all(row["fallback_file_sha256"] for row in receipts)


async def test_sync_from_file_rejects_unbound_before_read_or_mutation(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge_file = tmp_path / "claude_ai_context.md"
    bridge_file.write_text(
        "## Career & Professional Target\nuntrusted edit\n", encoding="utf-8"
    )
    monkeypatch.setattr(cfg, "BRIDGE_FILE_PATH", bridge_file)

    with pytest.raises(ToolError, match="Unauthenticated connection"):
        await fns["sync_from_file"](ctx=make_ctx(db, principal=None))

    section_count = await (
        await db.execute("SELECT COUNT(*) FROM context_sections")
    ).fetchone()
    receipt_count = await (
        await db.execute("SELECT COUNT(*) FROM bridge_import_receipts")
    ).fetchone()
    assert section_count is not None and section_count[0] == 0
    assert receipt_count is not None and receipt_count[0] == 0


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

    first = await mod.sync_owned_sections_from_file(
        db=db, bridge_path=bridge_file, principal="cc"
    )
    second = await mod.sync_owned_sections_from_file(
        db=db, bridge_path=bridge_file, principal="cc"
    )

    assert first["count"] == 4
    assert second["count"] == 0
    assert second["unchanged"] == ["career", "speaking", "research", "capabilities"]

    cursor = await db.execute("SELECT COUNT(*) FROM context_sections")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 4


async def test_sync_from_file_conflicts_when_db_changed_since_export(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    bridge_file = tmp_path / "claude_ai_context.md"
    original = cfg.BRIDGE_FILE_PATH
    cfg.BRIDGE_FILE_PATH = bridge_file
    cap = CaptureMCP()
    exp_mod.register(cap)
    try:
        ctx = make_ctx(db, principal="claude_ai")
        await fns["update_section"](
            caller="claude_ai", section_name="career", content="exported v1", ctx=ctx
        )
        await cap.fns["export_bridge_markdown"](ctx=ctx)
        await fns["update_section"](
            caller="claude_ai",
            section_name="career",
            content="db v2",
            if_match_version=(await fns["get_section"](section_name="career", ctx=ctx))[
                "version"
            ],
            ctx=ctx,
        )
        bridge_file.write_text(
            """## Career & Professional Target
stale file edit
""",
            encoding="utf-8",
        )

        result = await fns["sync_from_file"](ctx=ctx)
    finally:
        cfg.BRIDGE_FILE_PATH = original

    assert result["count"] == 0
    assert result["conflict_count"] == 1
    assert result["conflicts"][0]["section_name"] == "career"
    section = await fns["get_section"](section_name="career", ctx=ctx)
    assert section["content"] == "db v2"
    cursor = await db.execute(
        "SELECT surface, target_key, reason, principal "
        "FROM write_conflicts WHERE id = ?",
        (result["conflicts"][0]["receipt_id"],),
    )
    receipt = await cursor.fetchone()
    assert receipt is not None
    assert receipt["surface"] == "markdown_sync"
    assert receipt["target_key"] == "career"
    assert receipt["reason"] == "stale_export_base"
    assert receipt["principal"] == "claude_ai"


async def test_sync_from_file_conflicts_when_existing_row_has_no_export_base(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    """BDB-DS-088-R1: unknown file ancestry cannot replace canonical bytes."""
    ctx = make_ctx(db, principal="claude_ai")
    await fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="canonical current value",
        ctx=ctx,
    )
    bridge_file = tmp_path / "bridge.md"
    bridge_file.write_text(
        "## Career & Professional Target\nstale unknown-base value\n",
        encoding="utf-8",
    )

    result = await mod.sync_owned_sections_from_file(
        db, bridge_file, principal="cc"
    )

    assert result["count"] == 0
    assert result["conflict_count"] == 1
    assert result["conflicts"][0]["reason"] == "missing_export_base"
    assert result["legacy_imports"] == ["career"]
    section = await fns["get_section"](section_name="career", ctx=ctx)
    assert section["content"] == "canonical current value"
    receipt = await (
        await db.execute(
            "SELECT surface, target_key, reason FROM write_conflicts "
            "WHERE id = ?",
            (result["conflicts"][0]["receipt_id"],),
        )
    ).fetchone()
    assert receipt is not None
    assert receipt["surface"] == "markdown_sync"
    assert receipt["target_key"] == "career"
    assert receipt["reason"] == "missing_export_base"


async def test_update_section_clamps_operator_self_promotion(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import CaptureMCP, make_ctx

    from bridge_db import config as bridge_config
    from bridge_db.tools import context as context_module

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "warn")
    cap = CaptureMCP()
    context_module.register(cap)
    result = await cap.fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="# Career\nupdated",
        source_trust="operator",
        ctx=make_ctx(db, principal="claude_ai"),
    )
    assert result["source_trust"] == "agent"
    assert result["source_trust_clamped"] is True

    cursor = await db.execute(
        "SELECT source_trust FROM context_sections WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["source_trust"] == "agent"


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

    result = await mod.sync_owned_sections_from_file(
        db=db, bridge_path=bridge_file, principal="cc"
    )

    assert result["sections_synced"] == []
    assert result["count"] == 0

    cursor = await db.execute("SELECT COUNT(*) FROM context_sections")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 0


# ---------------------------------------------------------------------------
# Task 6: change-detection and ingested demotion
# ---------------------------------------------------------------------------


def _write_bridge_file(tmp_path: Path, career_body: str) -> Path:
    """Minimal bridge markdown containing one owned section."""
    path = tmp_path / "bridge.md"
    path.write_text(
        f"## Career & Professional Target\n\n{career_body}\n", encoding="utf-8"
    )
    return path


async def test_sync_warn_mode_refuses_changed_section_without_export_base(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_db import config as bridge_config
    from bridge_db.tools.context import (
        _upsert_section,  # pyright: ignore[reportPrivateUsage]
        sync_owned_sections_from_file,
    )

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "warn")
    await _upsert_section(
        db=db,
        section_name="career",
        owner="claude_ai",
        content="original content",
        source_trust="operator",
        attempted_by="claude_ai",
        operation="update_section",
    )
    await db.commit()

    path = _write_bridge_file(tmp_path, "edited on disk by who-knows-what")
    result = await sync_owned_sections_from_file(
        db=db, bridge_path=path, principal="cc"
    )

    assert result["demoted"] == []
    assert result["conflicts"][0]["reason"] == "missing_export_base"
    cursor = await db.execute(
        "SELECT content, source_trust FROM context_sections WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["content"] == "original content"
    assert row["source_trust"] == "operator"


async def test_sync_preserves_label_when_content_unchanged(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_db import config as bridge_config
    from bridge_db.tools.context import (
        _upsert_section,  # pyright: ignore[reportPrivateUsage]
        parse_owned_sections,
        sync_owned_sections_from_file,
    )

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "warn")
    path = _write_bridge_file(tmp_path, "stable content")
    # Seed the DB with exactly what the file parser will produce, labeled operator.
    parsed = parse_owned_sections(path.read_text(encoding="utf-8"))
    await _upsert_section(
        db=db,
        section_name="career",
        owner="claude_ai",
        content=parsed["career"],
        source_trust="operator",
        attempted_by="claude_ai",
        operation="update_section",
    )
    await db.commit()

    result = await sync_owned_sections_from_file(
        db=db, bridge_path=path, principal="cc"
    )

    assert "career" in result["unchanged"]
    assert result["demoted"] == []
    cursor = await db.execute(
        "SELECT source_trust FROM context_sections WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["source_trust"] == "operator"


async def test_sync_off_mode_refuses_changed_section_without_export_base(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_db import config as bridge_config
    from bridge_db.tools.context import (
        _upsert_section,  # pyright: ignore[reportPrivateUsage]
        sync_owned_sections_from_file,
    )

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "off")
    await _upsert_section(
        db=db,
        section_name="career",
        owner="claude_ai",
        content="original",
        source_trust="operator",
        attempted_by="claude_ai",
        operation="update_section",
    )
    await db.commit()

    path = _write_bridge_file(tmp_path, "changed content")
    result = await sync_owned_sections_from_file(
        db=db, bridge_path=path, principal="cc"
    )

    assert result["demoted"] == []
    assert result["conflicts"][0]["reason"] == "missing_export_base"
    cursor = await db.execute(
        "SELECT content, source_trust FROM context_sections WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["content"] == "original"
    assert row["source_trust"] == "operator"


async def test_sync_unchanged_despite_trailing_newline_variance(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_db import config as bridge_config
    from bridge_db.tools.context import (
        _upsert_section,  # pyright: ignore[reportPrivateUsage]
        parse_owned_sections,
        sync_owned_sections_from_file,
    )

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "warn")
    path = _write_bridge_file(tmp_path, "stable content")
    parsed = parse_owned_sections(path.read_text(encoding="utf-8"))
    # Store with trailing newline — as a direct update_section write might.
    await _upsert_section(
        db=db,
        section_name="career",
        owner="claude_ai",
        content=parsed["career"] + "\n",
        source_trust="operator",
        attempted_by="claude_ai",
        operation="update_section",
    )
    await db.commit()

    result = await sync_owned_sections_from_file(
        db=db, bridge_path=path, principal="cc"
    )

    assert "career" in result["unchanged"]
    assert result["demoted"] == []
