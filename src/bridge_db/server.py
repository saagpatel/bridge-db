"""FastMCP server: lifespan, AppContext, and tool registration."""

import asyncio
import logging
import os
import sys
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from mcp.server.fastmcp import FastMCP
from mcp.types import ContentBlock

from bridge_db import clock, config
from bridge_db.db import open_db
from bridge_db.tenancy import TenancyTracker, owner_for_principal, tenancy_root

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
    tenancy_tracker: Any | None = None


class InstrumentedFastMCP(FastMCP):
    """Account every MCP tool request in the process-owned tenancy lease."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._bridge_shared_runtime = False
        self._bridge_request_lock = asyncio.Lock()
        self._bridge_shared_tenancy_tracker: TenancyTracker | None = None

    def enable_shared_runtime(self, tracker: TenancyTracker | None = None) -> None:
        """Serialize one broker's database access across MCP sessions."""
        self._bridge_shared_runtime = True
        self._bridge_shared_tenancy_tracker = tracker

    def shared_tenancy_tracker(self) -> TenancyTracker | None:
        return self._bridge_shared_tenancy_tracker

    def close_shared_runtime(self) -> None:
        tracker = self._bridge_shared_tenancy_tracker
        if tracker is not None:
            tracker.close(reason="shared_broker_close")
        self._bridge_shared_tenancy_tracker = None
        self._bridge_shared_runtime = False

    async def _call_tool_accounted(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        context = self.get_context()
        tracker = getattr(
            context.request_context.lifespan_context, "tenancy_tracker", None
        )
        if tracker is None:
            return await super().call_tool(name, arguments)
        tracker.request_started(name)
        outcome: Literal["succeeded", "failed"] = "failed"
        try:
            result = await super().call_tool(name, arguments)
            outcome = "succeeded"
            return result
        finally:
            tracker.request_finished(name, outcome=outcome)

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        if self._bridge_shared_runtime:
            async with self._bridge_request_lock:
                return await self._call_tool_accounted(name, arguments)
        return await self._call_tool_accounted(name, arguments)


async def monitor_tenancy_retirement(
    tracker: TenancyTracker,
    request_shutdown: Callable[[], None],
    *,
    poll_seconds: float = 1.0,
) -> None:
    """Cooperatively stop an idle generation after its exact drain marker appears."""
    while True:
        await asyncio.sleep(poll_seconds)
        if tracker.retirement_ready():
            request_shutdown()
            return


def build_tenancy_tracker(
    principal: str | None,
) -> tuple[TenancyTracker, dict[str, Any]]:
    """Build one tracker bound to the current immutable runtime identity."""
    from bridge_db.execution_generation import runtime_generation_identity

    runtime_generation = runtime_generation_identity()
    manifest_path = runtime_generation.get("manifest_path")
    execution_root = (
        Path(str(manifest_path)).parents[2]
        if runtime_generation["state"] == "verified" and manifest_path
        else None
    )
    tracker = TenancyTracker(
        root=tenancy_root(),
        owner=owner_for_principal(principal),
        principal=principal,
        generation=runtime_generation.get("generation_id"),
        execution_root=execution_root,
    )
    return tracker, runtime_generation


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncGenerator[AppContext, None]:  # noqa: ARG001
    from bridge_db.audit import log_audit
    from bridge_db.auth import (
        auth_mode,
        hash_token,
        load_principal_grants,
        resolve_grant,
    )
    raw_token = os.environ.get("BRIDGE_DB_PRINCIPAL_TOKEN")
    token = raw_token.strip() if raw_token is not None else None
    grant = resolve_grant(token, load_principal_grants(config.PRINCIPALS_PATH))
    principal = grant.caller if grant is not None else None
    tracker, runtime_generation = build_tenancy_tracker(principal)
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
    shared_tracker = (
        server.shared_tenancy_tracker()
        if isinstance(server, InstrumentedFastMCP)
        else None
    )
    owns_tracker = shared_tracker is None
    if shared_tracker is not None:
        tracker = shared_tracker
    else:
        tracker.start()
    cooperative_shutdown = asyncio.Event()
    monitor_task: asyncio.Task[None] | None = None
    if owns_tracker:
        server_task = asyncio.current_task()
        if server_task is None:
            tracker.close(reason="server_task_unavailable")
            raise RuntimeError("tenancy.server_task_unavailable")

        def request_cooperative_shutdown() -> None:
            cooperative_shutdown.set()
            server_task.cancel()

        monitor_task = asyncio.create_task(
            monitor_tenancy_retirement(tracker, request_cooperative_shutdown)
        )
    db: aiosqlite.Connection | None = None
    close_reason = "normal_close"
    try:
        db = await open_db(config.DB_PATH)
        yield AppContext(
            db=db,
            principal=principal,
            credential_hash=hash_token(token) if token and principal else None,
            credential_generation=grant.generation if grant is not None else None,
            generation_id=runtime_generation.get("generation_id"),
            generation_state=str(runtime_generation["state"]),
            runtime_generation=runtime_generation,
            tenancy_tracker=tracker,
        )
    except asyncio.CancelledError:
        close_reason = (
            "obsolete_generation_close"
            if cooperative_shutdown.is_set()
            else "server_cancelled"
        )
        raise
    except BaseException:
        close_reason = "server_error"
        raise
    finally:
        if monitor_task is not None:
            monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await monitor_task
        try:
            if db is not None:
                await db.close()
        finally:
            if owns_tracker:
                tracker.close(reason=close_reason)
        logger.info("bridge-db session shut down")


mcp = InstrumentedFastMCP(
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
