"""Audit tail tool: read the audit JSONL log with simple filters."""

import logging
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from bridge_db import config
from bridge_db.audit import iter_jsonl

logger = logging.getLogger("bridge_db.tools.audit")


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

        Reads `config.AUDIT_LOG_PATH`. Missing file returns []. Malformed lines
        are skipped. Timestamps are ISO8601 UTC; `since` compares as string,
        which matches temporal order for that format.
        """
        matched: list[dict[str, Any]] = []
        for record in iter_jsonl(config.AUDIT_LOG_PATH):
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
            matched.append(record)

        matched.sort(key=lambda r: r.get("ts") or "", reverse=True)
        return matched[:limit]
