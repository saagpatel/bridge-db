"""JSONL audit log — append-only writer plus a tolerant line-by-line reader."""

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
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


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield parsed records from a JSONL file.

    Missing file → empty iterator. Blank lines and malformed JSON lines are
    skipped silently so a single bad write cannot break downstream readers.
    """
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record
