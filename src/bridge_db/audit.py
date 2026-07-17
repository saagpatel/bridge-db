"""JSONL audit log — append-only writer plus a tolerant line-by-line reader."""

import json
import logging
import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from bridge_db import clock, config
from bridge_db.evidence import append_jsonl_durable

logger = logging.getLogger("bridge_db.audit")


class AuditUnavailableError(RuntimeError):
    """Neither primary audit evidence nor a durable failure receipt was writable."""


def log_audit(
    tool: str,
    caller: str | None,
    project: str | None,
    ok: bool,
    detail: str | None = None,
) -> dict[str, Any]:
    """Write audit evidence or continue with an independent durable failure receipt.

    A failed primary projection is not hidden: it creates a minimized receipt
    at ``AUDIT_FAILURE_LOG_PATH`` and returns ``audit_degraded=True``. If both
    evidence paths fail, this raises ``AuditUnavailableError``.
    """
    event: dict[str, Any] = {
        "ts": clock.now().isoformat().replace("+00:00", "Z"),
        "tool": tool,
        "caller": caller,
        "project": project,
        "ok": ok,
        "detail": detail,
    }
    try:
        result = append_jsonl_durable(
            config.AUDIT_LOG_PATH,
            event,
            rotate_bytes=config.AUDIT_LOG_ROTATE_BYTES,
        )
        return {
            "audit_degraded": False,
            "path": str(result.path),
            "rotated": str(result.rotated_path) if result.rotated_path else None,
        }
    except Exception as primary_error:
        logger.warning(
            "primary audit projection failed; recording durable degradation receipt",
            exc_info=True,
        )
        serialized = json.dumps(event, sort_keys=True, default=str).encode("utf-8")
        receipt = {
            "ts": clock.now().isoformat().replace("+00:00", "Z"),
            "kind": "audit_write_failure",
            "tool": tool,
            "caller": caller,
            "project": project,
            "event_sha256": hashlib.sha256(serialized).hexdigest(),
            "primary_error": type(primary_error).__name__,
            "status": "open",
        }
        try:
            result = append_jsonl_durable(
                config.AUDIT_FAILURE_LOG_PATH,
                receipt,
                rotate_bytes=config.AUDIT_LOG_ROTATE_BYTES,
            )
        except Exception as receipt_error:
            raise AuditUnavailableError(
                "audit write failed and durable failure evidence is unavailable; "
                "the caller must not claim an auditable success"
            ) from receipt_error
        return {
            "audit_degraded": True,
            "failure_receipt_path": str(result.path),
            "event_sha256": receipt["event_sha256"],
        }


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
