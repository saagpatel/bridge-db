"""FastMCP server: lifespan, AppContext, and tool registration."""

import logging
import os
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import aiosqlite
from mcp.server.fastmcp import FastMCP

from bridge_db import clock, config
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
    credential_hash: str | None = None
    credential_generation: int | None = None
    generation_id: str | None = None
    generation_state: str = "mutable_direct_path"
    runtime_generation: dict[str, Any] | None = None


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncGenerator[AppContext, None]:  # noqa: ARG001
    from bridge_db.audit import log_audit
    from bridge_db.auth import auth_mode, hash_token, load_principal_grants, resolve_grant
    from bridge_db.execution_generation import runtime_generation_identity

    raw_token = os.environ.get("BRIDGE_DB_PRINCIPAL_TOKEN")
    token = raw_token.strip() if raw_token is not None else None
    grant = resolve_grant(token, load_principal_grants(config.PRINCIPALS_PATH))
    principal = grant.caller if grant is not None else None
    runtime_generation = runtime_generation_identity()
    if grant is not None and clock.now() >= grant.expires_at:
        log_audit(
            "auth.bind",
            principal,
            None,
            ok=False,
            detail=f"principal={principal} reason=expired",
        )
    elif raw_token is not None and principal is None:
        # Env var was set but did not resolve to a principal: either blank
        # (shell-quoting bug) or a stale/wrong token. Audit so the misconfig
        # is visible rather than silently starting unbound.
        reason = "token blank" if not token else "token present but not enrolled"
        log_audit("auth.bind", None, None, ok=False, detail=reason)
    elif principal is not None:
        log_audit("auth.bind", None, None, ok=True, detail=f"principal={principal}")
    logger.info(
        "bridge-db starting, db=%s principal=%s auth_mode=%s generation=%s state=%s",
        config.DB_PATH,
        principal or "unbound",
        auth_mode(),
        runtime_generation.get("generation_id") or "mutable",
        runtime_generation["state"],
    )
    db = await open_db(config.DB_PATH)
    try:
        yield AppContext(
            db=db,
            principal=principal,
            credential_hash=hash_token(token) if token and principal else None,
            credential_generation=grant.generation if grant is not None else None,
            generation_id=runtime_generation.get("generation_id"),
            generation_state=str(runtime_generation["state"]),
            runtime_generation=runtime_generation,
        )
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
        "get_shipped_events/record_disposition "
        "for shipped-event sync (record_disposition writes the terminal sync "
        "state on the activity row: disposition='synced' for a downstream "
        "receipt, or a policy value for a non-receipt decision), "
        "get_write_conflicts for stale-write and raced-claim receipts, "
        "create_handoff/get_pending_handoffs for project handoffs; pickup returns "
        "a short-lived completion capability that clear_handoff must consume, "
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
