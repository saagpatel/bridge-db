"""Export tool: regenerate the markdown bridge file from DB."""

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from bridge_db import clock, config
from bridge_db.auth import require_bound_principal
from bridge_db.db import content_sha256, get_db, protected_tags_predicate
from bridge_db.instruction_boundary import (
    MARKDOWN_DOCUMENT_WARNING,
    markdown_boundary,
)
from bridge_db.tools.context import (
    owned_section_end_marker,
    owned_section_start_marker,
    parse_owned_sections,
)

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
_MARKDOWN_DATA_ESCAPES = str.maketrans(
    {
        "\\": "\\\\",
        "\n": "\\n",
        "\r": "\\r",
        "\v": "\\v",
        "\f": "\\f",
        "\x1c": "\\u001c",
        "\x1d": "\\u001d",
        "\x1e": "\\u001e",
        "\x85": "\\u0085",
        "\u2028": "\\u2028",
        "\u2029": "\\u2029",
    }
)


def _markdown_data(value: Any) -> str:
    """Render stored data without allowing it to create document lines."""
    return str(value).translate(_MARKDOWN_DATA_ESCAPES)


def _utf8_byte_count(content: str) -> int:
    """Return the on-disk byte count for UTF-8 encoded export content."""
    return len(content.encode("utf-8"))


class BridgeExportSafetyError(RuntimeError):
    """Raised when an export would overwrite the real fallback with empty context."""


@dataclass(frozen=True)
class ContextExportSnapshot:
    section_name: str
    version: int
    content_sha256: str


def _section_body(markdown: str, heading: str) -> str:
    section_name = next(
        (key for key, section_heading in _HEADING_MAP.items() if section_heading == heading),
        None,
    )
    if section_name is None:
        return ""
    return parse_owned_sections(markdown).get(section_name, "")


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
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
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
        SELECT snapshot_date, data, source_trust
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


async def build_markdown(
    db: Any, *, context_snapshot: list[ContextExportSnapshot] | None = None
) -> str:
    """Assemble the full bridge markdown from all tables."""
    today = clock.now().date().isoformat()

    # --- Context sections (Claude.ai-owned) ---
    cursor = await db.execute(
        "SELECT section_name, content, version, source_trust "
        "FROM context_sections ORDER BY section_name"
    )
    section_rows = await cursor.fetchall()
    sections: dict[str, tuple[str, str]] = {
        r["section_name"]: (r["content"], r["source_trust"]) for r in section_rows
    }
    if context_snapshot is not None:
        context_snapshot.extend(
            ContextExportSnapshot(
                section_name=row["section_name"],
                version=int(row["version"]),
                content_sha256=content_sha256(str(row["content"]).strip("\n")),
            )
            for row in section_rows
            if row["section_name"] in _SECTION_ORDER
        )

    # --- CC State Snapshot ---
    cursor = await db.execute(
        "SELECT snapshot_date, data, source_trust FROM system_snapshots "
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
            cost_table += f"| {_markdown_data(cr['month'])} | ${cr['amount']:.0f} |\n"
            total += cr["amount"]
        cost_table += f"| **Total** | **${total:.0f}** |\n"

        cc_snapshot_md = (
            "## Claude Code State Snapshot\n"
            f"{markdown_boundary(cc_snap_row['source_trust'])}\n"
            f"Last exported: {_markdown_data(snap_date)}\n\n"
        )
        for key, label in [
            ("active_projects", "Active Projects"),
            ("lessons", "Lessons"),
            ("patterns", "Key Patterns"),
            ("eval_findings", "Eval Findings"),
            ("infrastructure", "Infrastructure"),
        ]:
            if val := data.get(key):
                cc_snapshot_md += f"### {label}\n{_markdown_data(val)}\n\n"
        cc_snapshot_md += f"### Cost (ccusage)\n{cost_table}\n"
        if last := data.get("last_session"):
            cc_snapshot_md += (
                f"### Last Session ({_markdown_data(snap_date)})\n"
                f"{_markdown_data(last)}\n"
            )
    else:
        cc_snapshot_md = "## Claude Code State Snapshot\n_No snapshot yet._\n"

    def _render_activity_rows(
        rows: list[Any], *, newest_first: bool = False
    ) -> list[str]:
        lines: list[str] = []
        for r in rows if newest_first else reversed(rows):
            tags: list[str] = json.loads(r["tags"])
            tag_str = (
                f" [{']['.join(_markdown_data(tag) for tag in tags)}]" if tags else ""
            )
            branch_str = f" ({_markdown_data(r['branch'])})" if r["branch"] else ""
            lines.append(markdown_boundary(r["source_trust"]))
            lines.append(
                f"- [{_markdown_data(r['timestamp'])}]{tag_str} "
                f"{_markdown_data(r['project_name'])}: "
                f"{_markdown_data(r['summary'])}{branch_str}"
            )
        return lines

    # --- Recent CC Activity ---
    cursor = await db.execute(
        "SELECT timestamp, project_name, summary, branch, tags, source_trust FROM activity_log "
        "WHERE source='cc' ORDER BY created_at DESC, id DESC LIMIT 20"
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
        codex_snapshot_md = (
            "## Codex State Snapshot\n"
            f"{markdown_boundary(codex_snap_row['source_trust'])}\n"
            "Last exported: "
            f"{_markdown_data(codex_snap_row['snapshot_date'])}\n\n"
        )
        for key, label in [
            ("infrastructure", "Infrastructure"),
            ("automation_digest", "Automation Digest (Last 7 Days)"),
            ("active_projects", "Active Codex Projects"),
        ]:
            if val := cdata.get(key):
                codex_snapshot_md += f"### {label}\n{_markdown_data(val)}\n\n"
    else:
        codex_snapshot_md = "## Codex State Snapshot\n_No snapshot yet._\n"

    # --- Recent Codex Activity ---
    cursor = await db.execute(
        "SELECT timestamp, project_name, summary, branch, tags, source_trust FROM activity_log "
        "WHERE source='codex' ORDER BY created_at DESC, id DESC LIMIT 20"
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
            "SELECT timestamp, project_name, summary, branch, tags, source_trust FROM activity_log "
            "WHERE source=? ORDER BY created_at DESC, id DESC LIMIT 20",
            (source,),
        )
        extra_rows = await cursor.fetchall()
        if extra_rows:
            lines = _render_activity_rows(extra_rows)
            extra_activity_mds.append(heading + "\n" + "\n".join(lines) + "\n")

    # --- Pinned Ledger (retention-protected rows, all sources) ---
    protected_sql, protected_params = protected_tags_predicate()
    cursor = await db.execute(
        "SELECT timestamp, project_name, summary, branch, tags, source_trust FROM activity_log "
        f"WHERE {protected_sql} "  # noqa: S608
        "ORDER BY created_at DESC, id DESC LIMIT 15",
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
        "SELECT project_name, project_path, roadmap_file, phase, dispatched_at, source_trust "
        "FROM pending_handoffs WHERE status='pending' ORDER BY dispatched_at DESC, id DESC"
    )
    handoff_rows = await cursor.fetchall()
    if handoff_rows:
        handoff_lines: list[str] = []
        for r in handoff_rows:
            handoff_lines.append(markdown_boundary(r["source_trust"]))
            line = f"- **{_markdown_data(r['project_name'])}**"
            if r["project_path"]:
                line += f" — path: `{_markdown_data(r['project_path'])}`"
            if r["roadmap_file"]:
                line += f", roadmap: `{_markdown_data(r['roadmap_file'])}`"
            if r["phase"]:
                line += f", phase: {_markdown_data(r['phase'])}"
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
        MARKDOWN_DOCUMENT_WARNING,
        "",
    ]

    for section_key in _SECTION_ORDER:
        content, source_trust = sections.get(
            section_key, (_EMPTY_SECTION_PLACEHOLDER, "unknown")
        )
        parts.append(_HEADING_MAP[section_key])
        parts.append(owned_section_start_marker(section_key))
        parts.append(markdown_boundary(source_trust))
        parts.append(content)
        parts.append(owned_section_end_marker(section_key))
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


async def record_context_export_state(
    db: Any, snapshot: list[ContextExportSnapshot]
) -> int:
    """Record the context-section versions/hashes represented in the markdown export."""
    exported = 0
    for row in snapshot:
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
                row.section_name,
                row.version,
                row.content_sha256,
            ),
        )
        exported += 1
    return exported


async def export_bridge_file(
    db: Any,
    content: str,
    context_snapshot: list[ContextExportSnapshot],
    *,
    principal: str,
    trigger: str,
    projection_job_id: int | None = None,
) -> int:
    """CAS-protect and durably attribute one complete fallback-file export."""
    if not db.in_transaction:
        raise BridgeExportSafetyError(
            "Bridge export requires a caller-held write transaction acquired "
            "before rendering"
        )
    if projection_job_id is not None:
        cursor = await db.execute(
            "SELECT status FROM bridge_projection_jobs WHERE id = ?",
            (projection_job_id,),
        )
        projection_job = await cursor.fetchone()
        if projection_job is None:
            await db.rollback()
            raise BridgeExportSafetyError(
                f"Projection job {projection_job_id} does not exist"
            )
        if projection_job["status"] != "pending":
            await db.rollback()
            raise BridgeExportSafetyError(
                f"Projection job {projection_job_id} is not pending"
            )

    path = config.BRIDGE_FILE_PATH
    current_content = path.read_text(encoding="utf-8") if path.exists() else None
    current_hash = content_sha256(current_content) if current_content is not None else None
    intended_hash = content_sha256(content)
    cursor = await db.execute(
        "SELECT exported_content_sha256 FROM bridge_file_export_state WHERE singleton = 1"
    )
    state = await cursor.fetchone()
    expected_hash = state["exported_content_sha256"] if state is not None else None

    projection_already_rendered = (
        projection_job_id is not None and current_hash == intended_hash
    )
    if current_content is not None and expected_hash is not None:
        if current_hash != expected_hash and not projection_already_rendered:
            raise BridgeExportSafetyError(
                "Refusing to overwrite the fallback bridge file because it changed "
                "since the last export; import or merge the file edits first."
            )
    elif current_content is not None:
        # One-time v15 bootstrap: old databases have per-owned-section export
        # hashes but no whole-file hash. Accept the current file only when every
        # recorded editable section still matches its last exported image.
        cursor = await db.execute(
            "SELECT section_name, exported_content_sha256 "
            "FROM context_section_export_state"
        )
        section_states = await cursor.fetchall()
        safe_bootstrap = current_content == content
        if not safe_bootstrap:
            try:
                parsed = parse_owned_sections(current_content)
            except Exception as exc:
                raise BridgeExportSafetyError(
                    "Refusing to bootstrap export state from an ambiguous bridge file"
                ) from exc
            if not section_states:
                cursor = await db.execute(
                    "SELECT section_name, content FROM context_sections"
                )
                section_states = [
                    {
                        "section_name": row["section_name"],
                        "exported_content_sha256": content_sha256(
                            str(row["content"]).strip("\n")
                        ),
                    }
                    for row in await cursor.fetchall()
                ]
            safe_bootstrap = all(
                content_sha256(parsed.get(row["section_name"], ""))
                == row["exported_content_sha256"]
                for row in section_states
            ) and bool(section_states)
        if not safe_bootstrap:
            raise BridgeExportSafetyError(
                "Refusing to overwrite an untracked fallback bridge file; import "
                "or merge its owned-section edits first."
            )

    if not projection_already_rendered:
        write_bridge_file(content)
    exported_context_sections = await record_context_export_state(db, context_snapshot)
    await db.execute(
        """
        INSERT INTO bridge_file_export_state (
            singleton, exported_content_sha256, exported_at
        ) VALUES (1, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        ON CONFLICT(singleton) DO UPDATE SET
            exported_content_sha256 = excluded.exported_content_sha256,
            exported_at = excluded.exported_at
        """,
        (intended_hash,),
    )
    if projection_job_id is not None:
        projection_cursor = await db.execute(
            """
            UPDATE bridge_projection_jobs SET
                status = 'completed',
                attempts = attempts + 1,
                error_category = NULL,
                projected_content_sha256 = ?,
                completed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE id = ? AND status = 'pending'
            """,
            (intended_hash, projection_job_id),
        )
        if projection_cursor.rowcount != 1:
            raise BridgeExportSafetyError(
                f"Projection job {projection_job_id} changed before completion"
            )
    exported_context_sections = len(context_snapshot)
    await db.execute(
        """
        INSERT INTO bridge_export_receipts (
            principal, trigger, projection_job_id, previous_content_sha256,
            exported_content_sha256, exported_context_sections, byte_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            principal,
            trigger,
            projection_job_id,
            current_hash if projection_already_rendered else expected_hash,
            intended_hash,
            exported_context_sections,
            _utf8_byte_count(content),
        ),
    )
    return exported_context_sections


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def export_bridge_markdown(
        projection_job_id: Annotated[
            int | None,
            Field(
                ge=1,
                description=(
                    "Exact pending shipped-disposition projection job to retry. "
                    "Omit for a manual export that does not complete queued jobs."
                ),
            ),
        ] = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Regenerate the markdown bridge file from the database. Call after any write operation."""
        principal = require_bound_principal(ctx, tool="export_bridge_markdown")
        db = get_db(ctx)
        try:
            await db.execute("BEGIN IMMEDIATE")
            if projection_job_id is not None:
                cursor = await db.execute(
                    """
                    SELECT job.status, job.reason, activity.source
                    FROM bridge_projection_jobs AS job
                    LEFT JOIN activity_log AS activity
                      ON job.reason = 'shipped_disposition'
                     AND job.target_key = CAST(activity.id AS TEXT)
                    WHERE job.id = ?
                    """,
                    (projection_job_id,),
                )
                job = await cursor.fetchone()
                if job is None:
                    raise ToolError(
                        f"Projection job {projection_job_id} does not exist"
                    )
                if job["status"] != "pending":
                    raise ToolError(
                        f"Projection job {projection_job_id} is not pending"
                    )
                if job["reason"] != "shipped_disposition" or job["source"] is None:
                    raise ToolError(
                        f"Projection job {projection_job_id} has no verifiable source owner"
                    )
                if job["source"] != principal:
                    raise ToolError(
                        f"Projection job {projection_job_id} belongs to "
                        f"'{job['source']}', not '{principal}'"
                    )
            context_snapshot: list[ContextExportSnapshot] = []
            content = await build_markdown(db, context_snapshot=context_snapshot)
            exported_context_sections = await export_bridge_file(
                db,
                content,
                context_snapshot,
                principal=principal,
                trigger=(
                    "projection_retry" if projection_job_id is not None else "manual"
                ),
                projection_job_id=projection_job_id,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        bridge_path = config.BRIDGE_FILE_PATH
        byte_count = _utf8_byte_count(content)

        logger.info("bridge markdown exported: %s (%d bytes)", bridge_path, byte_count)
        return {
            "ok": True,
            "path": str(bridge_path),
            "bytes": byte_count,
            "exported_context_sections": exported_context_sections,
            "projection_job_id": projection_job_id,
        }
