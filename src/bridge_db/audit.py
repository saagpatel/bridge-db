"""JSONL audit log — append-only, never raises."""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from bridge_db import config

logger = logging.getLogger("bridge_db.audit")


def log_audit(
    tool: str,
    caller: str | None,
    project: str | None,
    ok: bool,
    detail: str | None = None,
) -> None:
    """Append one audit event to the audit JSONL log. Never raises."""
    try:
        event: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "tool": tool,
            "caller": caller,
            "project": project,
            "ok": ok,
            "detail": detail,
        }
        config.AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(config.AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        logger.debug("audit log write failed", exc_info=True)
