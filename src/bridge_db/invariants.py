"""Live protocol invariants (DST Phase 0, R3 §1.5).

TigerBeetle-style assertion vocabulary, on in production:

- ``always(condition, message, **context)`` — a protocol invariant that must
  hold on every execution. A violation writes an audit event and raises
  ``InvariantViolation``: crashing the tool call loudly beats continuing on
  corrupt coordination state.
- ``sometimes(label, condition)`` — a reachability counter. Records, both
  in-process and in the audit JSONL, that a labelled condition was actually
  reached — so a guard that never fires is distinguishable from a dead one.
  Never raises.

The simulation harness (Phase 2) aggregates ``sometimes`` labels across seed
batches; in production each hit is one greppable audit line.
"""

import json
import logging
from typing import Any

from bridge_db.audit import log_audit

logger = logging.getLogger("bridge_db.invariants")

_sometimes_counts: dict[str, int] = {}


def _render_context(context: dict[str, Any]) -> str:
    return json.dumps(context, default=str, sort_keys=True)


class InvariantViolation(AssertionError):
    """A live protocol invariant was violated; carries the assertion context."""

    def __init__(self, message: str, context: dict[str, Any]) -> None:
        self.context = context
        if context:
            message = f"{message} | context={_render_context(context)}"
        super().__init__(message)


def always(condition: bool, message: str, **context: Any) -> None:
    """Assert a protocol invariant. Violation = audit line + loud crash."""
    if condition:
        return
    log_audit(
        "invariant.violation",
        None,
        None,
        ok=False,
        detail=f"{message} context={_render_context(context)}",
    )
    logger.error("invariant violated: %s context=%s", message, context)
    raise InvariantViolation(message, context)


async def always_tx(db: Any, condition: bool, message: str, **context: Any) -> None:
    """``always()`` for a site with an open write transaction on ``db``.

    Rolls the transaction back before raising: the MCP dispatcher converts
    the raise into an error result for the one tool call (the process
    survives), and without the rollback the long-lived shared connection
    would keep the uncommitted write open until an unrelated later caller's
    ``commit()`` silently flushed the corrupt state to disk.
    """
    if not condition:
        try:
            await db.rollback()
        except Exception:
            logger.exception("rollback before invariant raise failed")
    always(condition, message, **context)


def sometimes(label: str, condition: bool = True) -> None:
    """Record that a labelled condition was reached. Counter only; never raises."""
    if not condition:
        return
    _sometimes_counts[label] = _sometimes_counts.get(label, 0) + 1
    log_audit("invariant.sometimes", None, None, ok=True, detail=label)


def sometimes_counts() -> dict[str, int]:
    """Snapshot of in-process ``sometimes`` counters (sim/test aggregation)."""
    return dict(_sometimes_counts)


def reset_sometimes_counts() -> None:
    """Clear in-process counters (sim/test use only)."""
    _sometimes_counts.clear()
