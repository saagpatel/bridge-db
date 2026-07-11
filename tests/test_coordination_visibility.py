"""Coordination-visibility coverage: open write_conflicts surfaced in health/status,
and live handoff claims readable through get_pending_handoffs.

Before this change the receipts ledger had no nag path (health/status never
mentioned write_conflicts) and claimed_by/picked_up_at were written on pickup
but returned by no read surface.
"""

from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx

from bridge_db import config
from bridge_db.db import record_write_conflict
from bridge_db.tools import handoffs as handoffs_mod
from bridge_db.tools import health as health_mod


@pytest.fixture
def health_fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    health_mod.register(cap)
    return cap.fns


@pytest.fixture
def handoff_fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    handoffs_mod.register(cap)
    return cap.fns


@pytest.fixture(autouse=True)
def patch_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point DB_PATH at the test DB so db_exists reflects reality."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    (tmp_path / "test.db").touch()


async def _seed_open_conflict(db: aiosqlite.Connection, target_key: str) -> int:
    receipt_id = await record_write_conflict(
        db,
        surface="context_section",
        target_key=target_key,
        operation="update_section",
        reason="stale_cas",
        attempted_by="cc",
    )
    await db.commit()
    return receipt_id


# ── health / status ──────────────────────────────────────────────────────────


async def test_health_counts_open_conflicts_without_folding_into_ok(
    db: aiosqlite.Connection, health_fns: dict[str, Any]
) -> None:
    await _seed_open_conflict(db, "career")
    resolved_id = await _seed_open_conflict(db, "speaking")
    await db.execute(
        "UPDATE write_conflicts SET status = 'resolved' WHERE id = ?", (resolved_id,)
    )
    await db.commit()

    result = await health_fns["health"](ctx=make_ctx(db))
    assert result["open_write_conflicts"] == 1
    assert result["oldest_open_conflict_age_hours"] is not None
    assert result["oldest_open_conflict_age_hours"] >= 0.0
    # Soft signal: a receipt is the conflict machinery working, not a broken bridge.
    assert result["ok"] is True


async def test_health_open_conflicts_zero_on_clean_db(
    db: aiosqlite.Connection, health_fns: dict[str, Any]
) -> None:
    result = await health_fns["health"](ctx=make_ctx(db))
    assert result["open_write_conflicts"] == 0
    assert result["oldest_open_conflict_age_hours"] is None


async def test_status_surfaces_open_conflicts_signal(
    db: aiosqlite.Connection, health_fns: dict[str, Any]
) -> None:
    # The count rides in signals exactly like its siblings — no special-cased
    # next-command key (review finding: signal-with-remediation must follow
    # the one existing convention, not grow a parallel key species).
    clean = await health_fns["status"](ctx=make_ctx(db))
    assert clean["signals"]["open_write_conflicts"] == 0
    assert "open_write_conflicts_next_command" not in clean

    await _seed_open_conflict(db, "career")
    flagged = await health_fns["status"](ctx=make_ctx(db))
    assert flagged["signals"]["open_write_conflicts"] == 1


# ── get_pending_handoffs status filter ───────────────────────────────────────


async def test_default_pending_contract_unchanged(
    db: aiosqlite.Connection, handoff_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await handoff_fns["create_handoff"](
        caller="claude_ai", project_name="VisibilityProj", ctx=ctx
    )
    rows = await handoff_fns["get_pending_handoffs"](ctx=ctx)
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["claimed_by"] is None
    assert rows[0]["picked_up_at"] is None


async def test_active_filter_exposes_claimant(
    db: aiosqlite.Connection, handoff_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    created = await handoff_fns["create_handoff"](
        caller="claude_ai", project_name="ClaimedProj", ctx=ctx
    )
    # agent-trust handoff: cc pickup needs the explicit confirm step
    picked = await handoff_fns["pick_up_handoff"](
        caller="cc", handoff_id=created["handoff_id"], confirm=True, ctx=ctx
    )
    assert picked["ok"] is True
    await handoff_fns["create_handoff"](
        caller="claude_ai", project_name="StillPendingProj", ctx=ctx
    )

    pending = await handoff_fns["get_pending_handoffs"](ctx=ctx)
    assert [r["project_name"] for r in pending] == ["StillPendingProj"]

    active = await handoff_fns["get_pending_handoffs"](status="active", ctx=ctx)
    assert len(active) == 1
    assert active[0]["project_name"] == "ClaimedProj"
    assert active[0]["claimed_by"] == "cc"
    assert active[0]["picked_up_at"] is not None

    both = await handoff_fns["get_pending_handoffs"](status="all", ctx=ctx)
    assert {r["project_name"] for r in both} == {"ClaimedProj", "StillPendingProj"}


async def test_cleared_rows_stay_excluded_from_all(
    db: aiosqlite.Connection, handoff_fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await handoff_fns["create_handoff"](
        caller="claude_ai", project_name="DoneProj", ctx=ctx
    )
    cleared = await handoff_fns["clear_handoff"](
        caller="cc", project_name="DoneProj", ctx=ctx
    )
    assert cleared["cleared"] is True
    both = await handoff_fns["get_pending_handoffs"](status="all", ctx=ctx)
    assert both == []
