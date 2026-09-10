"""Tests for bridge markdown migration — semantic fidelity, not byte-perfect."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from bridge_db.migration import (
    extract_sections,
    migrate_from_markdown,
    parse_activity_lines,
    parse_cost_table,
    parse_subsections,
)

# ── Minimal bridge fixture ─────────────────────────────────────────────────


MINIMAL_BRIDGE = """\
---
name: claude_ai_context
type: reference
---
# Bridge

## Career & Professional Target

**Target role:** Staff Engineer at AI lab
**Timeline:** 12 months

## Speaking Engagements

SFHDI Roundtable — April 2026

## Active Research Themes

- AI coding practices
- Platform engineering

## Claude.ai Capabilities Summary

17 custom skills. Key workflow: project-scorer → vibe-code-handoff.

## Pending Handoffs
<!-- no entries -->

## Claude Code State Snapshot
Last exported: 2026-04-13

### Active Projects
- ink, bridge-db

### Lessons
- specta BigInt: use i32 not i64
- mcpServers in settings.json is dead config

### Key Patterns
- Shell Hooks — PreToolUse reads stdin JSON

### Infrastructure
- 36 skills, 25 hooks

### Cost (ccusage)
| Month | Cost |
|---|---|
| 2026-02 | $55 |
| 2026-03 | $2,620 |
| 2026-04 | $650 |
| **Total** | **$3,325** |

### Last Session (2026-04-14)
Phase 0 done. 63 GitHub repos.

## Recent Claude Code Activity
<!-- /end skill appends here -->

## Codex State Snapshot
Last exported: 2026-04-14

### Infrastructure
- Skills: 31 active
- Automations: 14 active

### Automation Digest (Last 7 Days)
- Portfolio: No live run yet.

### Active Codex Projects
- ResumeEvolver
- GithubRepoAuditor

## Recent Codex Activity
<!-- Codex bridge-sync automation appends here -->
- [2026-04-14] bridge-scaffolding: Initial Codex sections added
- [2026-04-14] bridge-sync: First manual export
"""


@pytest.fixture
def bridge_file(tmp_path: Path) -> Path:
    f = tmp_path / "bridge.md"
    f.write_text(MINIMAL_BRIDGE)
    return f


# ── Unit tests for parsers ─────────────────────────────────────────────────


def test_extract_sections_finds_all_headings() -> None:
    sections = extract_sections(MINIMAL_BRIDGE)
    assert "Career & Professional Target" in sections
    assert "Speaking Engagements" in sections
    assert "Active Research Themes" in sections
    assert "Claude.ai Capabilities Summary" in sections
    assert "Claude Code State Snapshot" in sections
    assert "Codex State Snapshot" in sections
    assert "Recent Codex Activity" in sections


def test_extract_sections_body_content() -> None:
    sections = extract_sections(MINIMAL_BRIDGE)
    assert "Staff Engineer" in sections["Career & Professional Target"]
    assert "SFHDI" in sections["Speaking Engagements"]


def test_parse_subsections_cc_snapshot() -> None:
    sections = extract_sections(MINIMAL_BRIDGE)
    cc_snap = sections["Claude Code State Snapshot"]
    from bridge_db.migration import CC_SNAPSHOT_KEYS

    data = parse_subsections(cc_snap, CC_SNAPSHOT_KEYS)
    assert "active_projects" in data
    assert "lessons" in data
    assert "patterns" in data
    assert "infrastructure" in data
    assert "last_session" in data
    assert "ink" in data["active_projects"]
    assert "specta BigInt" in data["lessons"]


def test_parse_cost_table_extracts_rows() -> None:
    cost_text = """\
| Month | Cost |
|---|---|
| 2026-02 | $55 |
| 2026-03 | $2,620 |
| 2026-04 | $650 |
| **Total** | **$3,325** |
"""
    records = parse_cost_table(cost_text)
    assert len(records) == 3  # Total row excluded (no YYYY-MM pattern)
    months = [r["month"] for r in records]
    assert "2026-02" in months
    assert "2026-03" in months
    assert "2026-04" in months
    amounts = {r["month"]: r["amount"] for r in records}
    assert amounts["2026-02"] == 55.0
    assert amounts["2026-03"] == 2620.0
    assert amounts["2026-04"] == 650.0


def test_parse_activity_lines_standard_format() -> None:
    text = """\
- [2026-04-14][SHIPPED] bridge-db: Phase 0 complete (feat/scaffold)
- [2026-04-13] ink: Did some work
<!-- comment -->

"""
    entries = parse_activity_lines(text, "cc")
    assert len(entries) == 2

    assert entries[0]["timestamp"] == "2026-04-14"
    assert entries[0]["project_name"] == "bridge-db"
    assert entries[0]["summary"] == "Phase 0 complete"
    assert entries[0]["branch"] == "feat/scaffold"
    assert json.loads(entries[0]["tags"]) == ["SHIPPED"]

    assert entries[1]["timestamp"] == "2026-04-13"
    assert entries[1]["project_name"] == "ink"
    assert entries[1]["branch"] is None
    assert json.loads(entries[1]["tags"]) == []


def test_parse_activity_lines_codex_format() -> None:
    text = """\
- [2026-04-14] bridge-scaffolding: Initial Codex sections added
- [2026-04-14] bridge-sync: First manual export — real infrastructure state populated
"""
    entries = parse_activity_lines(text, "codex")
    assert len(entries) == 2
    assert entries[0]["source"] == "codex"
    assert entries[0]["project_name"] == "bridge-scaffolding"
    assert "Initial Codex sections" in entries[0]["summary"]


def test_parse_activity_lines_skips_comments_and_blanks() -> None:
    text = """\
<!-- /end skill appends here -->
<!-- Format: - [DATE] ... -->

"""
    entries = parse_activity_lines(text, "cc")
    assert len(entries) == 0


# ── Integration tests (full migration) ────────────────────────────────────


async def test_migration_populates_context_sections(
    db: aiosqlite.Connection, bridge_file: Path
) -> None:
    counts = await migrate_from_markdown(db, bridge_file)
    assert counts["context_sections"] == 4

    cursor = await db.execute(
        "SELECT section_name, owner FROM context_sections ORDER BY section_name"
    )
    rows: list[aiosqlite.Row] = await cursor.fetchall()  # type: ignore[assignment]
    section_names = {r["section_name"] for r in rows}
    assert section_names == {"career", "speaking", "research", "capabilities"}

    # All owned by claude_ai
    for r in rows:
        assert r["owner"] == "claude_ai"


async def test_migration_section_content_correct(
    db: aiosqlite.Connection, bridge_file: Path
) -> None:
    await migrate_from_markdown(db, bridge_file)

    cursor = await db.execute("SELECT content FROM context_sections WHERE section_name='career'")
    row = await cursor.fetchone()
    assert row is not None
    assert "Staff Engineer" in row["content"]


async def test_migration_populates_cc_snapshot(db: aiosqlite.Connection, bridge_file: Path) -> None:
    await migrate_from_markdown(db, bridge_file)

    cursor = await db.execute("SELECT snapshot_date, data FROM system_snapshots WHERE system='cc'")
    row = await cursor.fetchone()
    assert row is not None
    assert row["snapshot_date"] == "2026-04-13"
    data: dict[str, Any] = json.loads(row["data"])
    assert "active_projects" in data
    assert "lessons" in data
    assert "ink" in data["active_projects"]


async def test_migration_populates_codex_snapshot(
    db: aiosqlite.Connection, bridge_file: Path
) -> None:
    await migrate_from_markdown(db, bridge_file)

    cursor = await db.execute(
        "SELECT snapshot_date, data FROM system_snapshots WHERE system='codex'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["snapshot_date"] == "2026-04-14"
    data = json.loads(row["data"])
    assert "infrastructure" in data
    assert "automation_digest" in data
    assert "active_projects" in data


async def test_migration_populates_cost_records(
    db: aiosqlite.Connection, bridge_file: Path
) -> None:
    await migrate_from_markdown(db, bridge_file)

    cursor = await db.execute(
        "SELECT month, amount FROM cost_records WHERE system='cc' ORDER BY month"
    )
    rows: list[aiosqlite.Row] = await cursor.fetchall()  # type: ignore[assignment]
    assert len(rows) == 3
    months = [r["month"] for r in rows]
    assert months == ["2026-02", "2026-03", "2026-04"]
    amounts = {r["month"]: r["amount"] for r in rows}
    assert amounts["2026-02"] == 55.0
    assert amounts["2026-03"] == 2620.0


async def test_migration_populates_codex_activity(
    db: aiosqlite.Connection, bridge_file: Path
) -> None:
    await migrate_from_markdown(db, bridge_file)

    cursor = await db.execute("SELECT COUNT(*) FROM activity_log WHERE source='codex'")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 2  # bridge-scaffolding + bridge-sync


async def test_migration_cc_activity_empty(db: aiosqlite.Connection, bridge_file: Path) -> None:
    await migrate_from_markdown(db, bridge_file)

    cursor = await db.execute("SELECT COUNT(*) FROM activity_log WHERE source='cc'")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 0  # Only HTML comments, no real entries


async def test_migration_is_idempotent(db: aiosqlite.Connection, bridge_file: Path) -> None:
    counts1 = await migrate_from_markdown(db, bridge_file)
    counts2 = await migrate_from_markdown(db, bridge_file)

    # Second run inserts nothing
    assert counts2["context_sections"] == 0
    assert counts2["snapshots"] == 0
    assert counts2["activity_log"] == 0

    # Data still present
    cursor = await db.execute("SELECT COUNT(*) FROM context_sections")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 4  # unchanged from first run

    _ = counts1  # suppress unused warning


async def test_migration_populates_content_index(
    db: aiosqlite.Connection, bridge_file: Path
) -> None:
    """migrate_from_markdown must leave content_index in sync with source rows.

    The bootstrap path uses direct INSERTs that bypass the per-tool FTS5 hooks,
    so migrate_from_markdown calls repopulate_content_index at the end. Regression
    test to catch future changes that drop that call.
    """
    await migrate_from_markdown(db, bridge_file)

    # content_index row count equals the sum of source rows it mirrors.
    cursor = await db.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM context_sections)
          + (SELECT COUNT(*) FROM activity_log)
          + (SELECT COUNT(*) FROM system_snapshots)
          + (SELECT COUNT(*) FROM pending_handoffs)
          AS src_total,
            (SELECT COUNT(*) FROM content_index) AS fts_total
        """
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["src_total"] > 0, "fixture should have seeded some rows"
    assert row["fts_total"] == row["src_total"], "content_index not synced after migration"

    # MATCH works — a term from a seeded section reaches the FTS index.
    cursor = await db.execute(
        "SELECT COUNT(*) FROM content_index WHERE content_index MATCH 'Staff'"
    )
    match_row = await cursor.fetchone()
    assert match_row is not None
    assert match_row[0] >= 1
