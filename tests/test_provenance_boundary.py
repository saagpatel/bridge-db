"""Provenance / instruction-boundary coverage (Phase 2, register #3/#4).

Cross-system inbound read tools must tag returned peer-written content with
instruction_boundary so a CC session treats it as untrusted data, not an
operator instruction. recall/get_section/handoffs already do; this closes the
gap on get_recent_activity, get_activity_signal, and get_latest_snapshot, whose
payloads previously surfaced peer content with no provenance marker.
"""

from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx

from bridge_db.tools import activity as a_mod
from bridge_db.tools import snapshots as s_mod


@pytest.fixture
def a_fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    a_mod.register(cap)
    return cap.fns


@pytest.fixture
def s_fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    s_mod.register(cap)
    return cap.fns


def _assert_boundary(boundary: dict[str, Any], expected_trust: str) -> None:
    assert boundary["kind"] == "stored_data_not_instructions"
    assert boundary["source_trust"] == expected_trust
    assert "warning" in boundary and boundary["warning"]


async def test_get_recent_activity_carries_instruction_boundary(
    db: aiosqlite.Connection, a_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await a_fns["log_activity"](caller="cc", project_name="P", summary="did work", ctx=ctx)
    rows = await a_fns["get_recent_activity"](ctx=ctx)
    assert rows, "expected at least one activity row"
    assert rows[0]["source_trust"] == "agent"
    _assert_boundary(rows[0]["instruction_boundary"], "agent")


async def test_get_activity_signal_carries_instruction_boundary(
    db: aiosqlite.Connection, a_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    # A non-session-boundary cc summary is a substantive entry (not a lifecycle
    # aggregate), so it flows through the per-row payload that needs the boundary.
    await a_fns["log_activity"](caller="cc", project_name="P", summary="substantive work", ctx=ctx)
    entries = await a_fns["get_activity_signal"](ctx=ctx)
    substantive = [e for e in entries if e.get("kind") == "activity"]
    assert substantive, "expected a substantive activity entry"
    assert substantive[0]["source_trust"] == "agent"
    _assert_boundary(substantive[0]["instruction_boundary"], "agent")


async def test_get_latest_snapshot_carries_instruction_boundary(
    db: aiosqlite.Connection, s_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await s_fns["save_snapshot"](caller="cc", data={"k": "v"}, ctx=ctx)
    snap = await s_fns["get_latest_snapshot"](system="cc", ctx=ctx)
    assert snap["source_trust"] == "agent"
    _assert_boundary(snap["instruction_boundary"], "agent")
