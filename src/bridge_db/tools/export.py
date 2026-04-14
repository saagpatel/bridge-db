"""Export tool: regenerate the markdown bridge file from DB."""

import json
import logging
from datetime import date
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from bridge_db import config
from bridge_db.db import get_db

logger = logging.getLogger("bridge_db.tools.export")

_SECTION_ORDER = ["career", "speaking", "research", "capabilities"]


async def build_markdown(db: Any) -> str:
    """Assemble the full bridge markdown from all tables."""
    today = str(date.today())

    # --- Context sections (Claude.ai-owned) ---
    cursor = await db.execute(
        "SELECT section_name, content FROM context_sections ORDER BY section_name"
    )
    sections: dict[str, str] = {r["section_name"]: r["content"] for r in await cursor.fetchall()}

    # --- CC State Snapshot ---
    cursor = await db.execute(
        "SELECT snapshot_date, data FROM system_snapshots WHERE system='cc' ORDER BY created_at DESC LIMIT 1"
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

        cc_snapshot_md = f"## Claude Code State Snapshot\nLast exported: {snap_date}\n\n"
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

    # --- Recent CC Activity ---
    cursor = await db.execute(
        "SELECT timestamp, project_name, summary, branch, tags FROM activity_log "
        "WHERE source='cc' ORDER BY timestamp DESC, created_at DESC LIMIT 20"
    )
    cc_activity_rows = await cursor.fetchall()
    cc_activity_lines: list[str] = []
    for r in reversed(cc_activity_rows):
        tags: list[str] = json.loads(r["tags"])
        tag_str = f" [{']['.join(tags)}]" if tags else ""
        branch_str = f" ({r['branch']})" if r["branch"] else ""
        cc_activity_lines.append(
            f"- [{r['timestamp']}]{tag_str} {r['project_name']}: {r['summary']}{branch_str}"
        )
    cc_activity_md = "## Recent Claude Code Activity\n"
    cc_activity_md += (
        "\n".join(cc_activity_lines) if cc_activity_lines else "_No activity yet._"
    ) + "\n"

    # --- Codex State Snapshot ---
    cursor = await db.execute(
        "SELECT snapshot_date, data FROM system_snapshots WHERE system='codex' ORDER BY created_at DESC LIMIT 1"
    )
    codex_snap_row = await cursor.fetchone()
    if codex_snap_row:
        cdata: dict[str, Any] = json.loads(codex_snap_row["data"])
        codex_snapshot_md = (
            f"## Codex State Snapshot\nLast exported: {codex_snap_row['snapshot_date']}\n\n"
        )
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
        "WHERE source='codex' ORDER BY timestamp DESC, created_at DESC LIMIT 20"
    )
    codex_activity_rows = await cursor.fetchall()
    codex_activity_lines: list[str] = []
    for r in reversed(codex_activity_rows):
        tags: list[str] = json.loads(r["tags"])
        tag_str = f" [{']['.join(tags)}]" if tags else ""
        branch_str = f" ({r['branch']})" if r["branch"] else ""
        codex_activity_lines.append(
            f"- [{r['timestamp']}]{tag_str} {r['project_name']}: {r['summary']}{branch_str}"
        )
    codex_activity_md = "## Recent Codex Activity\n"
    codex_activity_md += (
        "\n".join(codex_activity_lines) if codex_activity_lines else "_No activity yet._"
    ) + "\n"

    # --- Pending Handoffs ---
    cursor = await db.execute(
        "SELECT project_name, project_path, roadmap_file, phase, dispatched_at "
        "FROM pending_handoffs WHERE status='pending' ORDER BY dispatched_at DESC"
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
        heading_map = {
            "career": "## Career & Professional Target",
            "speaking": "## Speaking Engagements",
            "research": "## Active Research Themes",
            "capabilities": "## Claude.ai Capabilities Summary",
        }
        parts.append(heading_map[section_key])
        parts.append(sections.get(section_key, "_Not yet populated._"))
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

    return "\n".join(parts)


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def export_bridge_markdown(
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Regenerate the markdown bridge file from the database. Call after any write operation."""
        db = get_db(ctx)
        content = await build_markdown(db)

        bridge_path = config.BRIDGE_FILE_PATH
        bridge_path.parent.mkdir(parents=True, exist_ok=True)
        bridge_path.write_text(content, encoding="utf-8")

        logger.info("bridge markdown exported: %s (%d bytes)", bridge_path, len(content))
        return {"ok": True, "path": str(bridge_path), "bytes": len(content)}
