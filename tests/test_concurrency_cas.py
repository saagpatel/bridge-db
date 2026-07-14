"""Concurrency / optimistic-concurrency (CAS) tests for the 3-way shared bridge.

Covers two cross-session data-integrity hazards from the 2026-06-19 failure &
threat register:

- update_section lost-update (register rank #1): a stale read-modify-write must
  not silently clobber a concurrent write — `if_match_updated_at` makes the write
  conditional on the section being unchanged since the caller read it.
- pick_up_handoff double-claim TOCTOU (register rank #6): two callers that both
  pass the pending-status SELECT must not both transition the row — the
  status-guarded UPDATE is the real claim.
"""

from typing import Any, cast

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx
from mcp.server.fastmcp.exceptions import ToolError

from bridge_db.tools import conflicts as conflict_mod
from bridge_db.tools import context as c_mod
from bridge_db.tools import handoffs as h_mod


@pytest.fixture
def c_fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    c_mod.register(cap)
    return cap.fns


@pytest.fixture
def h_fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    h_mod.register(cap)
    return cap.fns


# ── update_section optimistic concurrency (rank #1) ──────────────────────────


async def test_update_section_if_match_success(
    db: aiosqlite.Connection, c_fns: dict[str, Any]
) -> None:
    """A write whose if_match equals the current updated_at succeeds."""
    ctx = make_ctx(db)
    await c_fns["update_section"](
        caller="claude_ai", section_name="career", content="v1", ctx=ctx
    )
    current = await c_fns["get_section"](section_name="career", ctx=ctx)

    result = await c_fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="v2",
        if_match_updated_at=current["updated_at"],
        ctx=ctx,
    )
    assert result["ok"] is True
    refreshed = await c_fns["get_section"](section_name="career", ctx=ctx)
    assert refreshed["content"] == "v2"


async def test_update_section_if_match_version_success_increments_version(
    db: aiosqlite.Connection, c_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await c_fns["update_section"](
        caller="claude_ai", section_name="career", content="v1", ctx=ctx
    )
    current = await c_fns["get_section"](section_name="career", ctx=ctx)

    result = await c_fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="v2",
        if_match_version=current["version"],
        ctx=ctx,
    )

    assert result["ok"] is True
    assert result["version"] == current["version"] + 1
    refreshed = await c_fns["get_section"](section_name="career", ctx=ctx)
    assert refreshed["content"] == "v2"
    assert refreshed["version"] == result["version"]


async def test_update_section_if_match_conflict_preserves_concurrent_write(
    db: aiosqlite.Connection, c_fns: dict[str, Any]
) -> None:
    """A stale if_match must NOT overwrite a concurrent update — the silent
    lost-update is converted into an explicit conflict, and the concurrent
    writer's content survives."""
    ctx = make_ctx(db)
    await c_fns["update_section"](
        caller="claude_ai", section_name="career", content="v1", ctx=ctx
    )
    stale = await c_fns["get_section"](section_name="career", ctx=ctx)
    stale_updated_at = stale["updated_at"]

    # Simulate a concurrent writer changing the row (and its updated_at) after our
    # read but before our write — the exact 3-way bridge lost-update window.
    await db.execute(
        "UPDATE context_sections SET content = 'concurrent edit', "
        "updated_at = '2099-01-01T00:00:00Z' WHERE section_name = 'career'"
    )
    await db.commit()

    result = await c_fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="stale clobber",
        if_match_updated_at=stale_updated_at,
        ctx=ctx,
    )
    assert result["ok"] is False
    assert result["conflict"] is True
    assert result["current_updated_at"] == "2099-01-01T00:00:00Z"

    # The concurrent write is intact — our stale write did not land.
    survivor = await c_fns["get_section"](section_name="career", ctx=ctx)
    assert survivor["content"] == "concurrent edit"


async def test_update_section_version_cas_catches_same_second_conflict(
    db: aiosqlite.Connection, c_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await c_fns["update_section"](
        caller="claude_ai", section_name="career", content="v1", ctx=ctx
    )
    await db.execute(
        "UPDATE context_sections SET updated_at = '2026-01-01T00:00:00Z', version = 1 "
        "WHERE section_name = 'career'"
    )
    await db.commit()
    stale = await c_fns["get_section"](section_name="career", ctx=ctx)

    await db.execute(
        "UPDATE context_sections SET content = 'concurrent same-second edit', "
        "updated_at = '2026-01-01T00:00:00Z', version = 2 WHERE section_name = 'career'"
    )
    await db.commit()

    result = await c_fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="stale clobber",
        if_match_updated_at=stale["updated_at"],
        if_match_version=stale["version"],
        ctx=ctx,
    )

    assert result["ok"] is False
    assert result["conflict"] is True
    assert result["receipt_id"]
    assert result["current_version"] == 2
    survivor = await c_fns["get_section"](section_name="career", ctx=ctx)
    assert survivor["content"] == "concurrent same-second edit"


async def test_update_section_without_if_match_rejected(
    db: aiosqlite.Connection, c_fns: dict[str, Any]
) -> None:
    """CAS is unconditional for existing rows (INV-4, flipped to the sole
    behavior 2026-07-12 when the warn/enforce canary was cut): a blind
    write against an existing section is rejected with a durable receipt
    instead of silently displacing committed work."""
    ctx = make_ctx(db)
    await c_fns["update_section"](
        caller="claude_ai", section_name="career", content="v1", ctx=ctx
    )

    result = await c_fns["update_section"](
        caller="claude_ai", section_name="career", content="blind v2", ctx=ctx
    )

    assert result["ok"] is False
    assert result["conflict"] is True
    assert result["reason_code"] == "missing_cas"
    survivor = await c_fns["get_section"](section_name="career", ctx=ctx)
    assert survivor["content"] == "v1"


async def test_update_section_new_section_insert_needs_no_cas(
    db: aiosqlite.Connection, c_fns: dict[str, Any]
) -> None:
    """A brand-new section has nothing to CAS against: the first write
    succeeds without if_match_version or if_match_updated_at."""
    ctx = make_ctx(db)
    result = await c_fns["update_section"](
        caller="claude_ai", section_name="career", content="v1", ctx=ctx
    )

    assert result["ok"] is True
    assert result["version"] == 1
    section = await c_fns["get_section"](section_name="career", ctx=ctx)
    assert section["content"] == "v1"


async def test_get_write_conflicts_reads_section_conflict_receipts(
    db: aiosqlite.Connection, c_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await c_fns["update_section"](
        caller="claude_ai", section_name="career", content="v1", ctx=ctx
    )
    stale = await c_fns["get_section"](section_name="career", ctx=ctx)
    await c_fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="v2",
        if_match_version=stale["version"],
        ctx=ctx,
    )
    conflict = await c_fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="stale",
        if_match_version=stale["version"],
        ctx=ctx,
    )

    cap = CaptureMCP()
    conflict_mod.register(cap)
    receipts = await cap.fns["get_write_conflicts"](ctx=ctx)

    assert receipts[0]["id"] == conflict["receipt_id"]
    assert receipts[0]["surface"] == "context_section"
    assert receipts[0]["target_key"] == "career"
    assert receipts[0]["reason"] == "stale_cas"


# ── pick_up_handoff double-claim TOCTOU (rank #6) ────────────────────────────


class _RaceOnPickupSelect:
    """aiosqlite proxy that injects a concurrent claim at the pick_up_handoff
    TOCTOU window. The first time the pickup SELECT runs, it transitions the row
    to 'active' (as a racing 'cc'/'codex' caller would) before the tool's
    guarded UPDATE — deterministically reproducing the lost-update race that a
    single shared test connection otherwise can't surface."""

    def __init__(self, real: aiosqlite.Connection, handoff_id: int) -> None:
        self._real = real
        self._handoff_id = handoff_id
        self.injected = False

    async def execute(self, sql: str, parameters: Any = ()) -> Any:
        cursor = await self._real.execute(sql, parameters)
        if not self.injected and sql.lstrip().startswith(
            "SELECT id, project_name, status"
        ):
            self.injected = True
            await self._real.execute(
                "UPDATE pending_handoffs SET status = 'active', "
                "picked_up_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                (self._handoff_id,),
            )
            await self._real.commit()
        return cursor

    async def commit(self) -> None:
        await self._real.commit()

    async def rollback(self) -> None:
        await self._real.rollback()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


async def test_pick_up_handoff_rejects_concurrent_claim(
    db: aiosqlite.Connection, h_fns: dict[str, Any]
) -> None:
    """If another caller claims the handoff between our SELECT and UPDATE, the
    status-guarded UPDATE matches 0 rows and the pickup is refused instead of
    silently double-claiming."""
    created = await h_fns["create_handoff"](
        caller="claude_ai",
        project_name="P",
        source_trust="operator",
        ctx=make_ctx(db, principal="claude_ai"),
    )
    await db.execute(
        "UPDATE pending_handoffs SET source_trust = 'operator' WHERE id = ?",
        (created["handoff_id"],),
    )
    await db.commit()
    handoff_id = created["handoff_id"]

    racer = _RaceOnPickupSelect(db, handoff_id)
    racing_ctx = make_ctx(cast(aiosqlite.Connection, racer))
    with pytest.raises(ToolError, match="another caller"):
        await h_fns["pick_up_handoff"](
            caller="cc", handoff_id=handoff_id, ctx=racing_ctx
        )
    # False-green guard: the race must actually have fired at the SELECT→UPDATE window.
    # If the pickup SELECT is reformatted so the proxy's prefix match misses, this
    # assertion fails loudly instead of the test silently passing.
    assert racer.injected is True

    # The row was claimed exactly once (by the injected racer); the losing call
    # did not re-transition it.
    cursor = await db.execute(
        "SELECT status FROM pending_handoffs WHERE id = ?", (handoff_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == "active"
    receipt_cursor = await db.execute(
        "SELECT surface, target_key, operation, reason FROM write_conflicts"
    )
    receipt = await receipt_cursor.fetchone()
    assert receipt is not None
    assert receipt["surface"] == "handoff"
    assert receipt["target_key"] == str(handoff_id)
    assert receipt["operation"] == "pick_up_handoff"
    assert receipt["reason"] == "raced_claim"


async def test_pick_up_handoff_normal_path_still_works(
    db: aiosqlite.Connection, h_fns: dict[str, Any]
) -> None:
    """The status guard must not regress the uncontended happy path."""
    created = await h_fns["create_handoff"](
        caller="claude_ai",
        project_name="P",
        source_trust="operator",
        ctx=make_ctx(db, principal="claude_ai"),
    )
    await db.execute(
        "UPDATE pending_handoffs SET source_trust = 'operator' WHERE id = ?",
        (created["handoff_id"],),
    )
    await db.commit()
    result = await h_fns["pick_up_handoff"](
        caller="cc", handoff_id=created["handoff_id"], ctx=make_ctx(db)
    )
    assert result["ok"] is True
    assert result["status"] == "active"
