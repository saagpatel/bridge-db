"""Migration: parse the existing bridge markdown file and populate the SQLite DB.

Run with: uv run python -m bridge_db.migration

The migration is idempotent — it checks for existing rows before inserting and
skips anything already present. Safe to re-run.
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
from bridge_db.db import open_db

logger = logging.getLogger("bridge_db.migration")

# ── Section heading → DB section_name ────────────────────────────────────────

SECTION_MAP: dict[str, str] = {
    "Career & Professional Target": "career",
    "Speaking Engagements": "speaking",
    "Active Research Themes": "research",
    "Claude.ai Capabilities Summary": "capabilities",
}

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


def extract_sections(content: str) -> dict[str, str]:
    """Split on level-2 (##) headings, return {heading_text: body_text}."""
    sections: dict[str, str] = {}
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in content.splitlines():
        if line.startswith("## "):
            if current_heading is not None:
                sections[current_heading] = "\n".join(current_lines).strip()
            current_heading = line[3:].strip()
            current_lines = []
        else:
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


def parse_activity_lines(text: str, source: str) -> list[dict[str, Any]]:
    """Parse activity log lines into list of dicts. Skips HTML comments and blanks."""
    entries: list[dict[str, Any]] = []
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
            logger.debug("Skipping unparseable activity line: %s", line[:80])
    return entries


# ── DB insertion helpers ─────────────────────────────────────────────────────


async def _insert_context_section(
    db: aiosqlite.Connection, section_name: str, owner: str, content: str
) -> bool:
    """Insert a context section. Returns True if inserted, False if already present."""
    cursor = await db.execute(
        "SELECT 1 FROM context_sections WHERE section_name = ?", (section_name,)
    )
    if await cursor.fetchone() is not None:
        logger.debug("context_sections: %s already exists, skipping", section_name)
        return False
    await db.execute(
        """
        INSERT INTO context_sections (section_name, owner, content)
        VALUES (?, ?, ?)
        """,
        (section_name, owner, content),
    )
    logger.info("Inserted context section: %s", section_name)
    return True


async def _insert_snapshot(
    db: aiosqlite.Connection, system: str, snap_date: str, data: dict[str, Any]
) -> bool:
    """Insert a snapshot. Returns True if inserted, False if system already has one."""
    cursor = await db.execute("SELECT 1 FROM system_snapshots WHERE system = ? LIMIT 1", (system,))
    if await cursor.fetchone() is not None:
        logger.debug("system_snapshots: %s already has a snapshot, skipping", system)
        return False
    await db.execute(
        "INSERT INTO system_snapshots (system, snapshot_date, data) VALUES (?, ?, ?)",
        (system, snap_date, json.dumps(data)),
    )
    logger.info("Inserted snapshot: system=%s date=%s", system, snap_date)
    return True


async def _upsert_cost_record(
    db: aiosqlite.Connection, system: str, month: str, amount: float
) -> None:
    """Upsert a cost record."""
    await db.execute(
        """
        INSERT INTO cost_records (system, month, amount)
        VALUES (?, ?, ?)
        ON CONFLICT(system, month) DO UPDATE SET amount = excluded.amount
        """,
        (system, month, amount),
    )
    logger.info("Upserted cost record: system=%s month=%s amount=%.0f", system, month, amount)


async def _insert_activity(db: aiosqlite.Connection, entry: dict[str, Any]) -> bool:
    """Insert an activity entry. Deduplicates on (source, timestamp, project_name)."""
    cursor = await db.execute(
        "SELECT 1 FROM activity_log WHERE source=? AND timestamp=? AND project_name=?",
        (entry["source"], entry["timestamp"], entry["project_name"]),
    )
    if await cursor.fetchone() is not None:
        return False
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
    return True


# ── Main migration entry point ───────────────────────────────────────────────


async def migrate_from_markdown(db: aiosqlite.Connection, bridge_path: Path) -> dict[str, Any]:
    """Parse bridge markdown and populate the DB. Idempotent.

    Returns a summary dict with counts of rows inserted per table.
    """
    if not bridge_path.exists():
        raise FileNotFoundError(f"Bridge file not found: {bridge_path}")

    content = bridge_path.read_text(encoding="utf-8")
    sections = extract_sections(content)
    logger.info("Parsed %d level-2 sections from %s", len(sections), bridge_path)

    counts: dict[str, int] = {
        "context_sections": 0,
        "snapshots": 0,
        "cost_records": 0,
        "activity_log": 0,
    }

    # 1. Context sections (owned by claude_ai)
    for heading, section_name in SECTION_MAP.items():
        body = sections.get(heading, "")
        if body:
            inserted = await _insert_context_section(db, section_name, "claude_ai", body)
            if inserted:
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

        inserted = await _insert_snapshot(db, "cc", snap_date, snapshot_data)
        if inserted:
            counts["snapshots"] += 1

        # Parse cost table from the "Cost" subsection
        # If cost wasn't in snapshot_data, search for it directly
        if not cost_text:
            # Look for ### Cost subsection manually
            cost_match = re.search(r"### Cost[^\n]*\n(.*?)(?=\n###|\Z)", cc_snap_content, re.DOTALL)
            if cost_match:
                cost_text = cost_match.group(1)

        for record in parse_cost_table(cost_text):
            await _upsert_cost_record(db, "cc", record["month"], record["amount"])
            counts["cost_records"] += 1

    # 3. CC Activity
    cc_activity_heading = "Recent Claude Code Activity"
    if cc_activity_heading in sections:
        for entry in parse_activity_lines(sections[cc_activity_heading], "cc"):
            if await _insert_activity(db, entry):
                counts["activity_log"] += 1

    # 4. Codex State Snapshot
    codex_snap_heading = "Codex State Snapshot"
    if codex_snap_heading in sections:
        codex_snap_content = sections[codex_snap_heading]
        snap_date_match = _SNAP_DATE_RE.search(codex_snap_content)
        snap_date = snap_date_match.group(1) if snap_date_match else "2026-01-01"

        snapshot_data = parse_subsections(codex_snap_content, CODEX_SNAPSHOT_KEYS)
        inserted = await _insert_snapshot(db, "codex", snap_date, snapshot_data)
        if inserted:
            counts["snapshots"] += 1

    # 5. Codex Activity
    codex_activity_heading = "Recent Codex Activity"
    if codex_activity_heading in sections:
        for entry in parse_activity_lines(sections[codex_activity_heading], "codex"):
            if await _insert_activity(db, entry):
                counts["activity_log"] += 1

    await db.commit()
    logger.info("Migration complete: %s", counts)
    return counts


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
