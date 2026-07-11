"""Export tool: regenerate the markdown bridge file from DB."""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from bridge_db import clock, config
from bridge_db.db import content_sha256, get_db, protected_tags_predicate

logger = logging.getLogger("bridge_db.tools.export")

_SECTION_ORDER = ["career", "speaking", "research", "capabilities"]
_EMPTY_SECTION_PLACEHOLDER = "_Not yet populated._"
_ALLOW_EMPTY_EXPORT_ENV = "BRIDGE_DB_ALLOW_EMPTY_BRIDGE_EXPORT"
_HEADING_MAP = {
    "career": "## Career & Professional Target",
    "speaking": "## Speaking Engagements",
    "research": "## Active Research Themes",
    "capabilities": "## Claude.ai Capabilities Summary",
}


class BridgeExportSafetyError(RuntimeError):
    """Raised when an export would overwrite the real fallback with empty context."""


def _section_body(markdown: str, heading: str) -> str:
    marker = f"{heading}\n"
    start = markdown.find(marker)
    if start == -1:
        return ""
    body_start = start + len(marker)
    next_heading = markdown.find("\n## ", body_start)
    if next_heading == -1:
        return markdown[body_start:].strip()
    return markdown[body_start:next_heading].strip()


def _core_context_is_placeholder_only(content: str) -> bool:
    return all(
        _section_body(content, _HEADING_MAP[section_key]) == _EMPTY_SECTION_PLACEHOLDER
        for section_key in _SECTION_ORDER
    )


def _is_claude_home_bridge_path(path: Path) -> bool:
    try:
        path.expanduser().resolve(strict=False).relative_to(
            (Path.home() / ".claude" / "projects").resolve(strict=False)
        )
    except ValueError:
        return False
    return path.name == "claude_ai_context.md"


def _validate_bridge_file_export(content: str, path: Path) -> None:
    if not _is_claude_home_bridge_path(path):
        return
    if os.environ.get(_ALLOW_EMPTY_EXPORT_ENV) == "1":
        return
    if not _core_context_is_placeholder_only(content):
        return
    raise BridgeExportSafetyError(
        "Refusing to overwrite the Claude.ai fallback bridge file with an export "
        f"whose core context sections are all placeholders. Set {_ALLOW_EMPTY_EXPORT_ENV}=1 "
        "only for an intentional empty bootstrap."
    )


def write_bridge_file(content: str) -> None:
    """Write the markdown bridge file atomically (F8).

    The notification-hub watcher tails this file's *directory* and any file-based
    client (Claude.ai) reads it directly. A plain ``write_text`` truncates then
    rewrites in place, so a concurrent reader can observe a half-written file.
    Writing to a temp file in the same directory and ``os.replace``-ing it into
    place makes the swap atomic: readers always see either the complete old or the
    complete new file, never a partial. The temp name is dot-prefixed so the
    watcher's exact-path filter ignores it (verified: macOS still emits a
    ``modified`` event on the destination after the replace, so detection is
    unaffected).
    """
    path = config.BRIDGE_FILE_PATH
    _validate_bridge_file_export(content, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _is_codex_operating_snapshot(data: dict[str, Any]) -> bool:
    return {"infrastructure", "automation_digest", "active_projects"}.issubset(data)


async def _latest_codex_operating_snapshot(
    db: Any,
) -> tuple[Any | None, dict[str, Any] | None]:
    cursor = await db.execute(
        """
        SELECT snapshot_date, data
        FROM system_snapshots
        WHERE system='codex'
        ORDER BY created_at DESC, id DESC
        """
    )
    for row in await cursor.fetchall():
        data: dict[str, Any] = json.loads(row["data"])
        if _is_codex_operating_snapshot(data):
            return row, data
    return None, None


async def build_markdown(db: Any) -> str:
    """Assemble the full bridge markdown from all tables."""
    today = clock.now().date().isoformat()

    # --- Context sections (Claude.ai-owned) ---
    cursor = await db.execute(
        "SELECT section_name, content FROM context_sections ORDER BY section_name"
    )
    sections: dict[str, str] = {
        r["section_name"]: r["content"] for r in await cursor.fetchall()
    }

    # --- CC State Snapshot ---
    cursor = await db.execute(
        "SELECT snapshot_date, data FROM system_snapshots "
        "WHERE system='cc' ORDER BY created_at DESC, id DESC LIMIT 1"
    )
    cc_snap_row = await cursor.fetchone()
    cc_snapshot_md = ""
    if cc_snap_row:
        snap_date = cc_snap_row["snapshot_date"]
        data: dict[str, Any] = json.loads(cc_snap_row["data"])
        cost_cursor = await db.execute(
            "SELECT month, amount FROM cost_records WHERE system='cc' ORDER BY month DESC LIMIT 12"
        )
        cost_rows = await cost_cursor.fetchall()
        cost_table = "| Month | Cost |\n|---|---|\n"
        total = 0.0
        for cr in cost_rows:
            cost_table += f"| {cr['month']} | ${cr['amount']:.0f} |\n"
            total += cr["amount"]
        cost_table += f"| **Total** | **${total:.0f}** |\n"

        cc_snapshot_md = (
            f"## Claude Code State Snapshot\nLast exported: {snap_date}\n\n"
        )
        for key, label in [
            ("active_projects", "Active Projects"),
            ("lessons", "Lessons"),
            ("patterns", "Key Patterns"),
            ("eval_findings", "Eval Findings"),
            ("infrastructure", "Infrastructure"),
        ]:
            if val := data.get(key):
                cc_snapshot_md += f"### {label}\n{val}\n\n"
        cc_snapshot_md += f"### Cost (ccusage)\n{cost_table}\n"
        if last := data.get("last_session"):
            cc_snapshot_md += f"### Last Session ({snap_date})\n{last}\n"
    else:
        cc_snapshot_md = "## Claude Code State Snapshot\n_No snapshot yet._\n"

    def _render_activity_rows(
        rows: list[Any], *, newest_first: bool = False
    ) -> list[str]:
        lines: list[str] = []
        for r in rows if newest_first else reversed(rows):
            tags: list[str] = json.loads(r["tags"])
            tag_str = f" [{']['.join(tags)}]" if tags else ""
            branch_str = f" ({r['branch']})" if r["branch"] else ""
            lines.append(
                f"- [{r['timestamp']}]{tag_str} {r['project_name']}: {r['summary']}{branch_str}"
            )
        return lines

    # --- Recent CC Activity ---
    cursor = await db.execute(
        "SELECT timestamp, project_name, summary, branch, tags FROM activity_log "
        "WHERE source='cc' ORDER BY timestamp DESC, created_at DESC, id DESC LIMIT 20"
    )
    cc_activity_rows = await cursor.fetchall()
    cc_activity_lines = _render_activity_rows(cc_activity_rows)
    cc_activity_md = "## Recent Claude Code Activity\n"
    cc_activity_md += (
        "\n".join(cc_activity_lines) if cc_activity_lines else "_No activity yet._"
    ) + "\n"

    # --- Codex State Snapshot ---
    codex_snap_row, cdata = await _latest_codex_operating_snapshot(db)
    if codex_snap_row and cdata is not None:
        codex_snapshot_md = f"## Codex State Snapshot\nLast exported: {codex_snap_row['snapshot_date']}\n\n"
        for key, label in [
            ("infrastructure", "Infrastructure"),
            ("automation_digest", "Automation Digest (Last 7 Days)"),
            ("active_projects", "Active Codex Projects"),
        ]:
            if val := cdata.get(key):
                codex_snapshot_md += f"### {label}\n{val}\n\n"
    else:
        codex_snapshot_md = "## Codex State Snapshot\n_No snapshot yet._\n"

    # --- Recent Codex Activity ---
    cursor = await db.execute(
        "SELECT timestamp, project_name, summary, branch, tags FROM activity_log "
        "WHERE source='codex' ORDER BY timestamp DESC, created_at DESC, id DESC LIMIT 20"
    )
    codex_activity_rows = await cursor.fetchall()
    codex_activity_lines = _render_activity_rows(codex_activity_rows)
    codex_activity_md = "## Recent Codex Activity\n"
    codex_activity_md += (
        "\n".join(codex_activity_lines)
        if codex_activity_lines
        else "_No activity yet._"
    ) + "\n"

    # --- Additional source activity (notion_os, personal_ops) — rendered only if rows exist ---
    _EXTRA_SOURCES: dict[str, str] = {
        "notion_os": "## Recent Notion OS Activity",
        "personal_ops": "## Recent Personal Ops Activity",
    }
    extra_activity_mds: list[str] = []
    for source, heading in _EXTRA_SOURCES.items():
        cursor = await db.execute(
            "SELECT timestamp, project_name, summary, branch, tags FROM activity_log "
            "WHERE source=? ORDER BY timestamp DESC, created_at DESC, id DESC LIMIT 20",
            (source,),
        )
        extra_rows = await cursor.fetchall()
        if extra_rows:
            lines = _render_activity_rows(extra_rows)
            extra_activity_mds.append(heading + "\n" + "\n".join(lines) + "\n")

    # --- Pinned Ledger (retention-protected rows, all sources) ---
    protected_sql, protected_params = protected_tags_predicate()
    cursor = await db.execute(
        "SELECT timestamp, project_name, summary, branch, tags FROM activity_log "
        f"WHERE {protected_sql} "  # noqa: S608
        "ORDER BY timestamp DESC, created_at DESC, id DESC LIMIT 15",
        protected_params,
    )
    ledger_rows = await cursor.fetchall()
    ledger_md = ""
    if ledger_rows:
        ledger_md = (
            "## Pinned Ledger\n"
            + "\n".join(_render_activity_rows(ledger_rows, newest_first=True))
            + "\n"
        )

    # --- Pending Handoffs ---
    cursor = await db.execute(
        "SELECT project_name, project_path, roadmap_file, phase, dispatched_at "
        "FROM pending_handoffs WHERE status='pending' ORDER BY dispatched_at DESC, id DESC"
    )
    handoff_rows = await cursor.fetchall()
    if handoff_rows:
        handoff_lines: list[str] = []
        for r in handoff_rows:
            line = f"- **{r['project_name']}**"
            if r["project_path"]:
                line += f" — path: `{r['project_path']}`"
            if r["roadmap_file"]:
                line += f", roadmap: `{r['roadmap_file']}`"
            if r["phase"]:
                line += f", phase: {r['phase']}"
            handoff_lines.append(line)
        handoffs_md = "## Pending Handoffs\n" + "\n".join(handoff_lines) + "\n"
    else:
        handoffs_md = "## Pending Handoffs\n<!-- No pending handoffs -->\n"

    # --- Assemble full document ---
    parts = [
        "---",
        "name: claude_ai_context",
        "description: Three-way bridge — context shared between Claude.ai, Claude Code, and Codex",
        "type: reference",
        "---",
        "",
        "# Claude.ai <-> Claude Code <-> Codex Context Bridge",
        f"Last synced: {today}",
        "",
    ]

    for section_key in _SECTION_ORDER:
        parts.append(_HEADING_MAP[section_key])
        parts.append(sections.get(section_key, _EMPTY_SECTION_PLACEHOLDER))
        parts.append("")

    parts.append(handoffs_md)
    parts.append("")
    parts.append(cc_snapshot_md)
    parts.append("")
    parts.append(cc_activity_md)
    parts.append("")
    parts.append(codex_snapshot_md)
    parts.append("")
    parts.append(codex_activity_md)

    for extra_md in extra_activity_mds:
        parts.append("")
        parts.append(extra_md)

    if ledger_md:
        parts.append("")
        parts.append(ledger_md)

    return "\n".join(parts)


async def record_context_export_state(db: Any) -> int:
    """Record the context-section versions/hashes represented in the markdown export."""
    cursor = await db.execute(
        """
        SELECT section_name, content, version
        FROM context_sections
        WHERE section_name IN (?, ?, ?, ?)
        """,
        tuple(_SECTION_ORDER),
    )
    rows = await cursor.fetchall()
    exported = 0
    for row in rows:
        await db.execute(
            """
            INSERT INTO context_section_export_state (
                section_name, exported_version, exported_content_sha256, exported_at
            )
            VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            ON CONFLICT(section_name) DO UPDATE SET
                exported_version = excluded.exported_version,
                exported_content_sha256 = excluded.exported_content_sha256,
                exported_at = excluded.exported_at
            """,
            (
                row["section_name"],
                row["version"],
                content_sha256(str(row["content"]).strip("\n")),
            ),
        )
        exported += 1
    return exported


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def export_bridge_markdown(
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Regenerate the markdown bridge file from the database. Call after any write operation."""
        db = get_db(ctx)
        content = await build_markdown(db)

        write_bridge_file(content)
        exported_context_sections = await record_context_export_state(db)
        await db.commit()
        bridge_path = config.BRIDGE_FILE_PATH

        logger.info(
            "bridge markdown exported: %s (%d bytes)", bridge_path, len(content)
        )
        return {
            "ok": True,
            "path": str(bridge_path),
            "bytes": len(content),
            "exported_context_sections": exported_context_sections,
        }
