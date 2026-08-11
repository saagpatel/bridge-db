"""Migration: import the bounded legacy Markdown projection into SQLite.

Run with: uv run python -m bridge_db.migration

The migration is idempotent — it checks for existing rows before inserting and
skips anything already present. Safe to re-run.

The Markdown file is not a complete backup. It does not preserve every durable
surface, identifier, trust label, or receipt held by bridge-db.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import aiosqlite

from bridge_db.config import BRIDGE_FILE_PATH, DB_PATH
from bridge_db.db import open_db, repopulate_content_index

logger = logging.getLogger("bridge_db.migration")

# ── Section heading → DB section_name ────────────────────────────────────────

SECTION_MAP: dict[str, str] = {
    "Career & Professional Target": "career",
    "Speaking Engagements": "speaking",
    "Active Research Themes": "research",
    "Claude.ai Capabilities Summary": "capabilities",
}

BRIDGE_SECTION_HEADINGS = frozenset(
    {
        *SECTION_MAP.keys(),
        "Pending Handoffs",
        "Claude Code State Snapshot",
        "Recent Claude Code Activity",
        "Codex State Snapshot",
        "Recent Codex Activity",
        "Recent Notion OS Activity",
        "Recent Personal Ops Activity",
    }
)

# Snapshot sub-section label → JSON key mapping
CC_SNAPSHOT_KEYS: dict[str, str] = {
    "Active Projects": "active_projects",
    "Lessons": "lessons",
    "Key Patterns": "patterns",
    "Eval Findings": "eval_findings",
    "Infrastructure": "infrastructure",
    "Last Session": "last_session",
}

CODEX_SNAPSHOT_KEYS: dict[str, str] = {
    "Infrastructure": "infrastructure",
    "Automation Digest": "automation_digest",
    "Active Codex Projects": "active_projects",
}

# Activity line: - [YYYY-MM-DD][OPTIONAL_TAG] project: summary (optional branch)
_ACTIVITY_RE = re.compile(
    r"^-\s+\[(\d{4}-\d{2}-\d{2})\]\s*(?:\[([^\]]+)\]\s*)?(.+?):\s+(.+?)(?:\s+\(([^)]+)\))?\s*$"
)

# Cost table row: | YYYY-MM | $1,234 optional notes |
_COST_ROW_RE = re.compile(r"^\|\s*(\d{4}-\d{2})\s*\|\s*\$([0-9,]+)")

# Snapshot date: "Last exported: YYYY-MM-DD"
_SNAP_DATE_RE = re.compile(r"Last exported:\s*(\d{4}-\d{2}-\d{2})")


# ── Parsing helpers ──────────────────────────────────────────────────────────


def extract_sections(
    content: str, *, allowed_headings: frozenset[str] | None = None
) -> dict[str, str]:
    """Split on level-2 headings, return {heading_text: body_text}.

    When ``allowed_headings`` is supplied, only those bridge-owned top-level
    headings start a new section. This preserves nested ``##`` headings inside
    section bodies such as the Claude.ai long-form context sections.
    """
    sections: dict[str, str] = {}
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in content.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            if allowed_headings is None or heading in allowed_headings:
                if current_heading is not None:
                    sections[current_heading] = "\n".join(current_lines).strip()
                current_heading = heading
                current_lines = []
                continue
        if current_heading is not None:
            current_lines.append(line)

    if current_heading is not None:
        sections[current_heading] = "\n".join(current_lines).strip()

    return sections


def parse_subsections(content: str, key_map: dict[str, str]) -> dict[str, str]:
    """Split on level-3 (###) headings and return {key_map_key: body}."""
    result: dict[str, str] = {}
    current_label: str | None = None
    current_lines: list[str] = []

    for line in content.splitlines():
        if line.startswith("### "):
            if current_label is not None:
                body = "\n".join(current_lines).strip()
                # Match against any prefix of the key_map entries
                for label, key in key_map.items():
                    if current_label.startswith(label):
                        result[key] = body
                        break
            current_label = line[4:].strip()
            current_lines = []
        else:
            if current_label is not None:
                current_lines.append(line)

    # Last subsection
    if current_label is not None:
        body = "\n".join(current_lines).strip()
        for label, key in key_map.items():
            if current_label.startswith(label):
                result[key] = body
                break

    return result


def parse_cost_table(cost_section: str) -> list[dict[str, Any]]:
    """Parse markdown cost table rows into list of {month, amount} dicts."""
    records: list[dict[str, Any]] = []
    for line in cost_section.splitlines():
        m = _COST_ROW_RE.match(line.strip())
        if m:
            month = m.group(1)
            amount_str = m.group(2).replace(",", "")
            try:
                amount = float(amount_str)
                records.append({"month": month, "amount": amount})
            except ValueError:
                logger.warning("Could not parse cost amount: %s", m.group(2))
    return records


def _parse_activity_lines_with_malformed_count(
    text: str, source: str
) -> tuple[list[dict[str, Any]], int]:
    """Parse activity lines and count non-comment data lines that are malformed."""
    entries: list[dict[str, Any]] = []
    malformed = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("<!--") or line.startswith("-->"):
            continue
        m = _ACTIVITY_RE.match(line)
        if m:
            timestamp, raw_tags, project_name, summary, branch = m.groups()
            tags: list[str] = (
                [t.strip() for t in raw_tags.split("][") if t.strip()] if raw_tags else []
            )
            entries.append(
                {
                    "source": source,
                    "timestamp": timestamp,
                    "project_name": project_name.strip(),
                    "summary": summary.strip(),
                    "branch": branch,
                    "tags": json.dumps(tags),
                }
            )
        else:
            malformed += 1
            logger.debug("Skipping unparseable activity line: %s", line[:80])
    return entries, malformed


def parse_activity_lines(text: str, source: str) -> list[dict[str, Any]]:
    """Parse activity log lines into a list of dicts."""
    entries, _malformed = _parse_activity_lines_with_malformed_count(text, source)
    return entries


# ── DB insertion helpers ─────────────────────────────────────────────────────


async def _insert_context_section(
    db: aiosqlite.Connection, section_name: str, owner: str, content: str
) -> str:
    """Insert a context section and return imported, skipped, or conflicted."""
    cursor = await db.execute(
        "SELECT owner, content FROM context_sections WHERE section_name = ?",
        (section_name,),
    )
    existing = await cursor.fetchone()
    if existing is not None:
        if existing["owner"] == owner and existing["content"] == content:
            logger.debug("context_sections: %s already exists, skipping", section_name)
            return "skipped"
        logger.warning("context_sections: %s conflicts with existing row", section_name)
        return "conflicted"
    await db.execute(
        """
        INSERT INTO context_sections (section_name, owner, content)
        VALUES (?, ?, ?)
        """,
        (section_name, owner, content),
    )
    logger.info("Inserted context section: %s", section_name)
    return "imported"


async def _insert_snapshot(
    db: aiosqlite.Connection, system: str, snap_date: str, data: dict[str, Any]
) -> str:
    """Insert a snapshot and return imported, skipped, or conflicted."""
    cursor = await db.execute(
        "SELECT snapshot_date, data FROM system_snapshots WHERE system = ? LIMIT 1",
        (system,),
    )
    existing = await cursor.fetchone()
    encoded_data = json.dumps(data)
    if existing is not None:
        if existing["snapshot_date"] == snap_date and json.loads(existing["data"]) == data:
            logger.debug("system_snapshots: %s already has this snapshot", system)
            return "skipped"
        logger.warning("system_snapshots: %s conflicts with existing snapshot", system)
        return "conflicted"
    await db.execute(
        "INSERT INTO system_snapshots (system, snapshot_date, data) VALUES (?, ?, ?)",
        (system, snap_date, encoded_data),
    )
    logger.info("Inserted snapshot: system=%s date=%s", system, snap_date)
    return "imported"


async def _upsert_cost_record(
    db: aiosqlite.Connection, system: str, month: str, amount: float
) -> str:
    """Import a cost record without overwriting a conflicting canonical value."""
    cursor = await db.execute(
        "SELECT amount FROM cost_records WHERE system = ? AND month = ?",
        (system, month),
    )
    existing = await cursor.fetchone()
    if existing is not None:
        if float(existing["amount"]) == amount:
            return "skipped"
        logger.warning("cost_records: %s/%s conflicts with existing value", system, month)
        return "conflicted"
    await db.execute(
        """
        INSERT INTO cost_records (system, month, amount)
        VALUES (?, ?, ?)
        """,
        (system, month, amount),
    )
    logger.info("Inserted cost record: system=%s month=%s amount=%.0f", system, month, amount)
    return "imported"


async def _insert_activity(db: aiosqlite.Connection, entry: dict[str, Any]) -> str:
    """Insert an activity entry. Deduplicate only an exact semantic identity."""
    cursor = await db.execute(
        """
        SELECT 1 FROM activity_log
        WHERE source=? AND timestamp=? AND project_name=? AND summary=?
          AND branch IS ? AND tags=?
        """,
        (
            entry["source"],
            entry["timestamp"],
            entry["project_name"],
            entry["summary"],
            entry["branch"],
            entry["tags"],
        ),
    )
    if await cursor.fetchone() is not None:
        return "skipped"
    await db.execute(
        """
        INSERT INTO activity_log (source, timestamp, project_name, summary, branch, tags)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            entry["source"],
            entry["timestamp"],
            entry["project_name"],
            entry["summary"],
            entry["branch"],
            entry["tags"],
        ),
    )
    return "imported"


# ── Main migration entry point ───────────────────────────────────────────────


async def migrate_from_markdown(db: aiosqlite.Connection, bridge_path: Path) -> dict[str, Any]:
    """Parse bridge markdown and populate the DB. Idempotent.

    Returns per-table import counts plus an explicit record-level outcome
    summary. This imports a lossy projection and must not be represented as
    complete backup restoration.
    """
    if not bridge_path.exists():
        raise FileNotFoundError(f"Bridge file not found: {bridge_path}")

    content = bridge_path.read_text(encoding="utf-8")
    sections = extract_sections(content, allowed_headings=BRIDGE_SECTION_HEADINGS)
    logger.info("Parsed %d level-2 sections from %s", len(sections), bridge_path)

    counts: dict[str, int] = {
        "context_sections": 0,
        "snapshots": 0,
        "cost_records": 0,
        "activity_log": 0,
    }
    outcomes = {
        "parsed": 0,
        "imported": 0,
        "skipped": 0,
        "conflicted": 0,
        "malformed": 0,
    }

    def record_outcome(outcome: str) -> None:
        outcomes["parsed"] += 1
        outcomes[outcome] += 1

    # 1. Context sections (owned by claude_ai)
    for heading, section_name in SECTION_MAP.items():
        body = sections.get(heading, "")
        if body:
            outcome = await _insert_context_section(db, section_name, "claude_ai", body)
            record_outcome(outcome)
            if outcome == "imported":
                counts["context_sections"] += 1

    # 2. CC State Snapshot
    cc_snap_heading = "Claude Code State Snapshot"
    if cc_snap_heading in sections:
        cc_snap_content = sections[cc_snap_heading]
        snap_date_match = _SNAP_DATE_RE.search(cc_snap_content)
        snap_date = snap_date_match.group(1) if snap_date_match else "2026-01-01"

        snapshot_data = parse_subsections(cc_snap_content, CC_SNAPSHOT_KEYS)

        # Remove the cost sub-section from snapshot data (cost goes to its own table)
        cost_text = snapshot_data.pop("cost", "")

        outcome = await _insert_snapshot(db, "cc", snap_date, snapshot_data)
        record_outcome(outcome)
        if outcome == "imported":
            counts["snapshots"] += 1

        # Parse cost table from the "Cost" subsection
        # If cost wasn't in snapshot_data, search for it directly
        if not cost_text:
            # Look for ### Cost subsection manually
            cost_match = re.search(r"### Cost[^\n]*\n(.*?)(?=\n###|\Z)", cc_snap_content, re.DOTALL)
            if cost_match:
                cost_text = cost_match.group(1)

        for record in parse_cost_table(cost_text):
            outcome = await _upsert_cost_record(db, "cc", record["month"], record["amount"])
            record_outcome(outcome)
            if outcome == "imported":
                counts["cost_records"] += 1

    # 3. CC Activity
    cc_activity_heading = "Recent Claude Code Activity"
    if cc_activity_heading in sections:
        entries, malformed = _parse_activity_lines_with_malformed_count(
            sections[cc_activity_heading], "cc"
        )
        outcomes["malformed"] += malformed
        for entry in entries:
            outcome = await _insert_activity(db, entry)
            record_outcome(outcome)
            if outcome == "imported":
                counts["activity_log"] += 1

    # 4. Codex State Snapshot
    codex_snap_heading = "Codex State Snapshot"
    if codex_snap_heading in sections:
        codex_snap_content = sections[codex_snap_heading]
        snap_date_match = _SNAP_DATE_RE.search(codex_snap_content)
        snap_date = snap_date_match.group(1) if snap_date_match else "2026-01-01"

        snapshot_data = parse_subsections(codex_snap_content, CODEX_SNAPSHOT_KEYS)
        outcome = await _insert_snapshot(db, "codex", snap_date, snapshot_data)
        record_outcome(outcome)
        if outcome == "imported":
            counts["snapshots"] += 1

    # 5. Codex Activity
    codex_activity_heading = "Recent Codex Activity"
    if codex_activity_heading in sections:
        entries, malformed = _parse_activity_lines_with_malformed_count(
            sections[codex_activity_heading], "codex"
        )
        outcomes["malformed"] += malformed
        for entry in entries:
            outcome = await _insert_activity(db, entry)
            record_outcome(outcome)
            if outcome == "imported":
                counts["activity_log"] += 1

    await db.commit()
    # Bulk direct INSERTs above bypass the per-tool FTS5 hooks; rebuild the
    # content_index from source tables so recall stays consistent after bootstrap.
    await repopulate_content_index(db)
    result: dict[str, Any] = {
        **counts,
        **outcomes,
        "source_contract": "projection_only_not_complete_backup",
    }
    logger.info("Migration complete: %s", result)
    return result


async def _main() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logger.info("Opening DB: %s", DB_PATH)
    db = await open_db(DB_PATH)
    try:
        counts = await migrate_from_markdown(db, BRIDGE_FILE_PATH)
        print(f"Migration complete: {counts}")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(_main())
