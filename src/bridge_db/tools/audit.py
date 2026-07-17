"""Audit tail tool: read the audit JSONL log with simple filters."""

import heapq
import logging
from collections.abc import Iterator
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from bridge_db import config
from bridge_db.evidence import iter_jsonl_family_reverse

logger = logging.getLogger("bridge_db.tools.audit")

AUDIT_TAIL_MAX_SCAN_BYTES = 1024 * 1024


def collect_audit_tail(
    *,
    limit: int = 50,
    caller: str | None = None,
    tool: str | None = None,
    since: str | None = None,
    ok: bool | None = None,
) -> list[dict[str, Any]]:
    """Return recent audit events from a bounded newest-file horizon."""

    def matching_records() -> Iterator[dict[str, Any]]:
        for record in iter_jsonl_family_reverse(
            config.AUDIT_LOG_PATH, max_bytes=AUDIT_TAIL_MAX_SCAN_BYTES
        ):
            if caller is not None and record.get("caller") != caller:
                continue
            if tool is not None and record.get("tool") != tool:
                continue
            if ok is not None and record.get("ok") is not ok:
                continue
            if since is not None:
                ts = record.get("ts")
                if not isinstance(ts, str) or ts < since:
                    continue
            yield record

    return heapq.nlargest(
        limit, matching_records(), key=lambda record: record.get("ts") or ""
    )


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def audit_tail(
        limit: Annotated[int, Field(description="Max entries to return", ge=1, le=500)] = 50,
        caller: Annotated[
            str | None, Field(description="Filter by caller, e.g. 'cc', 'codex', 'claude_ai'")
        ] = None,
        tool: Annotated[
            str | None, Field(description="Filter by tool name, e.g. 'log_activity'")
        ] = None,
        since: Annotated[
            str | None,
            Field(
                description=("Only entries at or after this ISO8601 timestamp or YYYY-MM-DD date")
            ),
        ] = None,
        ok: Annotated[
            bool | None,
            Field(description="If set, return only entries matching this ok flag"),
        ] = None,
    ) -> list[dict[str, Any]]:
        """Return recent audit events, newest first, with optional filters.

        Reads one bounded newest-byte horizon across the active audit log and
        losslessly rotated segments. Missing files return []; malformed or
        boundary-truncated lines are skipped. Timestamps are ISO8601 UTC;
        `since` compares as string, which matches temporal order for that
        format.
        """
        return collect_audit_tail(limit=limit, caller=caller, tool=tool, since=since, ok=ok)
