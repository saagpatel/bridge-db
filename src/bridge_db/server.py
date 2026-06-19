"""FastMCP server: lifespan, AppContext, and tool registration."""

import logging
import os
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiosqlite
from mcp.server.fastmcp import FastMCP

from bridge_db import config
from bridge_db.db import open_db

# Logging — stderr only (stdout is the MCP JSON-RPC channel)
logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("BRIDGE_DB_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("bridge_db.server")


@dataclass
class AppContext:
    db: aiosqlite.Connection
    principal: str | None = None


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncGenerator[AppContext, None]:  # noqa: ARG001
    from bridge_db.audit import log_audit
    from bridge_db.auth import auth_mode, load_principals, resolve_principal

    raw_token = os.environ.get("BRIDGE_DB_PRINCIPAL_TOKEN")
    token = raw_token.strip() if raw_token is not None else None
    principal = resolve_principal(token, load_principals(config.PRINCIPALS_PATH))
    if raw_token is not None and principal is None:
        # Env var was set but did not resolve to a principal: either blank
        # (shell-quoting bug) or a stale/wrong token. Audit so the misconfig
        # is visible rather than silently starting unbound.
        reason = "token blank" if not token else "token present but not enrolled"
        log_audit("auth.bind", None, None, ok=False, detail=reason)
    elif principal is not None:
        log_audit("auth.bind", None, None, ok=True, detail=f"principal={principal}")
    logger.info(
        "bridge-db starting, db=%s principal=%s auth_mode=%s",
        config.DB_PATH,
        principal or "unbound",
        auth_mode(),
    )
    db = await open_db(config.DB_PATH)
    try:
        yield AppContext(db=db, principal=principal)
    finally:
        await db.close()
        logger.info("bridge-db shut down")


mcp = FastMCP(
    "bridge-db",
    instructions=(
        "SQLite-backed bridge for shared state between Claude.ai, Claude Code, "
        "Codex, Notion OS, and personal-ops. "
        "Use log_activity/get_recent_activity for raw session activity, "
        "get_activity_signal for operator-facing activity with lifecycle telemetry compressed, "
        "get_shipped_events/confirm_shipped_sync/record_shipped_event_disposition "
        "for shipped-event sync, mark_shipped_processed only for non-shipped "
        "operational rows, "
        "create_handoff/get_pending_handoffs for project handoffs, "
        "save_snapshot/get_latest_snapshot for system state, "
        "update_section/get_section/get_all_sections/sync_from_file for long-lived context "
        "(career, speaking, research, capabilities), "
        "record_cost/get_cost_history for cost tracking, "
        "recall for FTS5 lexical search across all bridge content, "
        "recall_stats and audit_tail for observability over the query and audit logs, "
        "health/status for diagnostics, "
        "and export_bridge_markdown to regenerate the human-readable markdown file."
    ),
    lifespan=app_lifespan,
)

# Register all tool groups
from bridge_db.tools import register_all  # noqa: E402

register_all(mcp)
