"""Tests for the live invariant vocabulary (DST Phase 0)."""

import aiosqlite
import pytest

from bridge_db import config
from bridge_db.audit import iter_jsonl
from bridge_db.invariants import (
    InvariantViolation,
    always,
    always_tx,
    reset_sometimes_counts,
    sometimes,
    sometimes_counts,
)
from bridge_db.tools.context import (
    _upsert_section,  # pyright: ignore[reportPrivateUsage]
)


def _audit_events() -> list[dict[str, object]]:
    return list(iter_jsonl(config.AUDIT_LOG_PATH))


def test_always_holds_is_silent() -> None:
    always(True, "INV-test: never fires", key="value")
    assert _audit_events() == []


def test_always_violation_raises_with_context_and_audits() -> None:
    with pytest.raises(InvariantViolation) as exc_info:
        always(False, "INV-test: fired", handoff_id=7, rowcount=2)

    assert "INV-test: fired" in str(exc_info.value)
    assert "handoff_id" in str(exc_info.value)
    assert exc_info.value.context == {"handoff_id": 7, "rowcount": 2}

    events = _audit_events()
    assert len(events) == 1
    assert events[0]["tool"] == "invariant.violation"
    assert events[0]["ok"] is False
    detail = events[0]["detail"]
    assert isinstance(detail, str)
    assert "INV-test: fired" in detail
    assert "handoff_id" in detail


def test_always_violation_is_an_assertion_error() -> None:
    # Callers that catch AssertionError for last-resort handling still see it.
    with pytest.raises(AssertionError):
        always(False, "INV-test: fired")


def test_always_renders_non_json_context() -> None:
    with pytest.raises(InvariantViolation):
        always(False, "INV-test: fired", weird=object())


def test_sometimes_counts_and_audits_when_reached() -> None:
    sometimes("test_label")
    sometimes("test_label")
    sometimes("other_label", True)

    assert sometimes_counts() == {"test_label": 2, "other_label": 1}
    events = _audit_events()
    assert [e["detail"] for e in events] == ["test_label", "test_label", "other_label"]
    assert all(e["tool"] == "invariant.sometimes" for e in events)
    assert all(e["ok"] is True for e in events)


def test_sometimes_false_condition_is_a_noop() -> None:
    sometimes("unreached", False)
    assert sometimes_counts() == {}
    assert _audit_events() == []


def test_reset_sometimes_counts() -> None:
    sometimes("test_label")
    reset_sometimes_counts()
    assert sometimes_counts() == {}


async def test_section_cas_write_steps_version_and_counts_rejections(
    db: aiosqlite.Connection,
) -> None:
    """Planted INV-4 assertions: a CAS write steps version by exactly 1
    (always() stays silent) and a stale CAS is counted as reached."""
    first = await _upsert_section(
        db,
        "career",
        "claude_ai",
        "v1 content",
        attempted_by="claude_ai",
        operation="update_section",
    )
    await db.commit()
    assert first == {"written": True, "legacy_blind_write": False, "receipt_id": None}

    cursor = await db.execute(
        "SELECT version FROM context_sections WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row is not None
    base_version = row["version"]

    cas_write = await _upsert_section(
        db,
        "career",
        "claude_ai",
        "v2 content",
        attempted_by="claude_ai",
        operation="update_section",
        if_match_version=base_version,
    )
    await db.commit()
    assert cas_write == {
        "written": True,
        "legacy_blind_write": False,
        "receipt_id": None,
    }

    stale = await _upsert_section(
        db,
        "career",
        "claude_ai",
        "v3 content",
        attempted_by="claude_ai",
        operation="update_section",
        if_match_version=base_version,
    )
    assert stale["written"] is False
    assert stale["reason"] == "stale_cas"
    # INV-5: the rejection receipt is staged by _upsert_section itself, in
    # the same transaction as the refused write — not left to the caller.
    assert stale["receipt_id"] is not None
    assert sometimes_counts().get("stale_cas_rejection") == 1


async def test_always_tx_holds_is_silent(db: aiosqlite.Connection) -> None:
    await always_tx(db, True, "INV-test: holds", key="value")
    assert _audit_events() == []


async def test_always_tx_violation_rolls_back_before_raising(
    db: aiosqlite.Connection,
) -> None:
    """A violation inside an open write transaction must not leave the
    uncommitted write on the shared connection for a later caller's commit
    to flush silently."""
    first = await _upsert_section(
        db,
        "career",
        "claude_ai",
        "tx probe",
        attempted_by="claude_ai",
        operation="update_section",
    )
    assert first["written"] is True  # write open, NOT committed

    with pytest.raises(InvariantViolation):
        await always_tx(db, False, "INV-test: tx fired", key=1)

    cursor = await db.execute(
        "SELECT COUNT(*) AS n FROM context_sections WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["n"] == 0  # the open write was rolled back, not left dangling
