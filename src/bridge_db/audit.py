"""JSONL audit log — append-only writer plus a tolerant line-by-line reader."""

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from bridge_db import clock, config

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
            "ts": clock.now().isoformat().replace("+00:00", "Z"),
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


def iter_jsonl_reverse(
    path: Path, *, max_bytes: int
) -> Iterator[dict[str, Any]]:
    """Yield valid JSON objects newest-line-first within a fixed byte horizon.

    The bounded binary read avoids materializing an unbounded append-only log.
    A record cut by the start boundary is discarded rather than parsed as a
    complete event; a missing final newline remains supported.
    """
    if max_bytes <= 0 or not path.exists():
        return
    try:
        size = path.stat().st_size
        start = max(0, size - max_bytes)
        with open(path, "rb") as f:
            starts_on_boundary = start == 0
            if start > 0:
                f.seek(start - 1)
                starts_on_boundary = f.read(1) == b"\n"
            f.seek(start)
            data = f.read(size - start)
    except OSError:
        return

    lines = data.splitlines()
    if start > 0 and not starts_on_boundary and lines:
        lines = lines[1:]
    for raw_line in reversed(lines):
        try:
            record = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            yield record
