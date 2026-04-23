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


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncGenerator[AppContext, None]:  # noqa: ARG001
    logger.info("bridge-db starting, db=%s", config.DB_PATH)
    db = await open_db(config.DB_PATH)
    try:
        yield AppContext(db=db)
    finally:
        await db.close()
        logger.info("bridge-db shut down")


mcp = FastMCP(
    "bridge-db",
    instructions=(
        "SQLite-backed bridge for shared state between Claude.ai, Claude Code, and Codex. "
        "Use log_activity/get_recent_activity for session activity, "
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
