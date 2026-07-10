"""Tests for handoff queue tools."""

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx
from mcp.server.fastmcp.exceptions import ToolError

from bridge_db import config
from bridge_db.tools import handoffs as mod
from bridge_db.tools.health import collect_status_summary


@pytest.fixture
def fns(db: aiosqlite.Connection) -> dict[str, Any]:
    cap = CaptureMCP()
    mod.register(cap)
    return cap.fns


def _registry(tmp_path: Path) -> Path:
    """A registry where 'IncidentMgmt' and 'IncidentManagement' share one key."""
    reg = tmp_path / "project-registry.json"
    reg.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "canonical_key": "incidentmgmt",
                        "display_name": "IncidentMgmt",
                        "repo_full_name": "saagpatel/IncidentManagement",
                        "aliases": [],
                    }
                ],
                "resolution_overrides": {},
            }
        )
    )
    return reg


async def _all_handoff_rows(db: aiosqlite.Connection) -> list[tuple[Any, ...]]:
    cursor = await db.execute("SELECT * FROM pending_handoffs ORDER BY id")
    rows = await cursor.fetchall()
    return [tuple(row) for row in rows]


async def test_create_handoff_requires_claude_ai(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    with pytest.raises(ToolError, match="claude_ai"):
        await fns["create_handoff"](caller="cc", project_name="P", ctx=ctx)


async def test_create_handoff_inserts_pending(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    result = await fns["create_handoff"](
        caller="claude_ai",
        project_name="MyProject",
        project_path="/home/user/Projects/MyProject",
        roadmap_file="ROADMAP.md",
        phase="Phase 2",
        ctx=ctx,
    )
    assert result["ok"] is True
    assert result["status"] == "pending"

    cursor = await db.execute("SELECT * FROM pending_handoffs")
    rows: list[aiosqlite.Row] = await cursor.fetchall()  # type: ignore[assignment]
    assert len(rows) == 1
    assert rows[0]["project_name"] == "MyProject"
    assert rows[0]["status"] == "pending"


async def test_create_handoff_persists_and_echoes_source_trust(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    asserted = await fns["create_handoff"](
        caller="claude_ai", project_name="Operatorish", source_trust="operator", ctx=ctx
    )
    ingested = await fns["create_handoff"](
        caller="claude_ai", project_name="Ingestedish", source_trust="ingested", ctx=ctx
    )
    defaulted = await fns["create_handoff"](
        caller="claude_ai", project_name="Defaulted", ctx=ctx
    )

    assert asserted["source_trust"] == "operator"
    assert ingested["source_trust"] == "ingested"
    assert defaulted["source_trust"] == "agent"

    cursor = await db.execute(
        "SELECT project_name, source_trust FROM pending_handoffs ORDER BY id"
    )
    rows: list[aiosqlite.Row] = await cursor.fetchall()  # type: ignore[assignment]
    trust = {r["project_name"]: r["source_trust"] for r in rows}
    assert trust["Operatorish"] == "operator"
    assert trust["Ingestedish"] == "ingested"
    assert trust["Defaulted"] == "agent"


async def test_create_handoff_audit_carries_source_trust(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["create_handoff"](
        caller="claude_ai", project_name="P", source_trust="operator", ctx=ctx
    )
    events = [
        json.loads(line)
        for line in config.AUDIT_LOG_PATH.read_text(encoding="utf-8").splitlines()
    ]
    create_events = [e for e in events if e["tool"] == "create_handoff"]
    assert create_events, "expected a create_handoff audit event"
    assert create_events[-1]["detail"] == "source_trust=operator"


async def test_get_pending_handoffs_returns_pending_only(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["create_handoff"](caller="claude_ai", project_name="A", ctx=ctx)
    await fns["create_handoff"](caller="claude_ai", project_name="B", ctx=ctx)
    # Mark one as cleared directly
    await db.execute(
        "UPDATE pending_handoffs SET status='cleared' WHERE project_name='A'"
    )
    await db.commit()

    pending = await fns["get_pending_handoffs"](ctx=ctx)
    assert len(pending) == 1
    assert pending[0]["project_name"] == "B"


async def test_get_pending_handoffs_surfaces_source_trust(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["create_handoff"](
        caller="claude_ai", project_name="Op", source_trust="operator", ctx=ctx
    )
    await fns["create_handoff"](
        caller="claude_ai", project_name="In", source_trust="ingested", ctx=ctx
    )
    await fns["create_handoff"](
        caller="claude_ai", project_name="Ag", ctx=ctx
    )  # default agent

    pending = await fns["get_pending_handoffs"](ctx=ctx)
    trust = {h["project_name"]: h["source_trust"] for h in pending}
    assert trust["Op"] == "operator"
    assert trust["In"] == "ingested"
    assert trust["Ag"] == "agent"
    for handoff in pending:
        boundary = handoff["instruction_boundary"]
        assert boundary["kind"] == "stored_data_not_instructions"
        assert boundary["source_trust"] == handoff["source_trust"]
        assert "non-operator content requires operator review" in boundary["warning"]


async def test_pick_up_handoff(db: aiosqlite.Connection, fns: dict[str, Any]) -> None:
    ctx = make_ctx(db)
    created = await fns["create_handoff"](
        caller="claude_ai", project_name="P", source_trust="operator", ctx=ctx
    )
    handoff_id = created["handoff_id"]

    # operator-trust handoff → fast path, picks up in one call.
    result = await fns["pick_up_handoff"](caller="cc", handoff_id=handoff_id, ctx=ctx)
    assert result["ok"] is True
    assert result["status"] == "active"

    cursor = await db.execute(
        "SELECT status, picked_up_at FROM pending_handoffs WHERE id = ?", (handoff_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == "active"
    assert row["picked_up_at"] is not None


async def test_pick_up_handoff_rejects_claude_ai(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    created = await fns["create_handoff"](caller="claude_ai", project_name="P", ctx=ctx)
    with pytest.raises(ToolError):
        await fns["pick_up_handoff"](
            caller="claude_ai", handoff_id=created["handoff_id"], ctx=ctx
        )


async def test_pick_up_nonexistent_handoff_raises(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    with pytest.raises(ToolError, match="No handoff found"):
        await fns["pick_up_handoff"](caller="cc", handoff_id=9999, ctx=ctx)


async def test_pick_up_cc_agent_requires_confirmation_no_transition(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    created = await fns["create_handoff"](
        caller="claude_ai", project_name="P", ctx=ctx
    )  # agent
    handoff_id = created["handoff_id"]

    result = await fns["pick_up_handoff"](caller="cc", handoff_id=handoff_id, ctx=ctx)
    assert result["ok"] is False
    assert result["requires_confirmation"] is True
    assert result["source_trust"] == "agent"
    assert result["status"] == "pending"

    cursor = await db.execute(
        "SELECT status, picked_up_at FROM pending_handoffs WHERE id = ?", (handoff_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == "pending"
    assert row["picked_up_at"] is None


async def test_pick_up_cc_agent_with_confirm_activates(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    created = await fns["create_handoff"](caller="claude_ai", project_name="P", ctx=ctx)
    handoff_id = created["handoff_id"]
    result = await fns["pick_up_handoff"](
        caller="cc", handoff_id=handoff_id, confirm=True, ctx=ctx
    )
    assert result["ok"] is True
    assert result["status"] == "active"
    assert result["source_trust"] == "agent"

    cursor = await db.execute(
        "SELECT status, picked_up_at FROM pending_handoffs WHERE id = ?", (handoff_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == "active"
    assert row["picked_up_at"] is not None


async def test_pick_up_cc_ingested_gate_then_confirm(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    """ingested is non-operator: gated (no transition) without confirm, active with confirm."""
    ctx = make_ctx(db)
    created = await fns["create_handoff"](
        caller="claude_ai", project_name="P", source_trust="ingested", ctx=ctx
    )
    handoff_id = created["handoff_id"]

    gated = await fns["pick_up_handoff"](caller="cc", handoff_id=handoff_id, ctx=ctx)
    assert gated["requires_confirmation"] is True
    assert gated["source_trust"] == "ingested"
    cursor = await db.execute(
        "SELECT status, picked_up_at FROM pending_handoffs WHERE id = ?", (handoff_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == "pending"
    assert row["picked_up_at"] is None

    confirmed = await fns["pick_up_handoff"](
        caller="cc", handoff_id=handoff_id, confirm=True, ctx=ctx
    )
    assert confirmed["ok"] is True
    assert confirmed["status"] == "active"


async def test_pick_up_codex_agent_refused_even_with_confirm(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    created = await fns["create_handoff"](
        caller="claude_ai", project_name="P", ctx=ctx
    )  # agent
    handoff_id = created["handoff_id"]

    with pytest.raises(ToolError, match="operator"):
        await fns["pick_up_handoff"](caller="codex", handoff_id=handoff_id, ctx=ctx)
    # confirm=True must NOT bypass the codex refusal.
    with pytest.raises(ToolError, match="operator"):
        await fns["pick_up_handoff"](
            caller="codex", handoff_id=handoff_id, confirm=True, ctx=ctx
        )

    cursor = await db.execute(
        "SELECT status, picked_up_at FROM pending_handoffs WHERE id = ?", (handoff_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == "pending"
    assert row["picked_up_at"] is None


async def test_pick_up_codex_ingested_refused(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    """Codex refusal keys on non-operator, not just 'agent' — ingested is refused too."""
    ctx = make_ctx(db)
    created = await fns["create_handoff"](
        caller="claude_ai", project_name="P", source_trust="ingested", ctx=ctx
    )
    handoff_id = created["handoff_id"]
    with pytest.raises(ToolError, match="operator"):
        await fns["pick_up_handoff"](caller="codex", handoff_id=handoff_id, ctx=ctx)

    cursor = await db.execute(
        "SELECT status, picked_up_at FROM pending_handoffs WHERE id = ?", (handoff_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == "pending"
    assert row["picked_up_at"] is None


@pytest.mark.parametrize("source_trust", ["agent", "ingested"])
async def test_codex_cannot_pick_up_non_operator_handoffs_even_with_confirm(
    db: aiosqlite.Connection, fns: dict[str, Any], source_trust: str
) -> None:
    ctx = make_ctx(db)
    created = await fns["create_handoff"](
        caller="claude_ai",
        project_name=f"NonOperator-{source_trust}",
        source_trust=source_trust,
        ctx=ctx,
    )

    with pytest.raises(ToolError, match="Codex cannot pick up"):
        await fns["pick_up_handoff"](
            caller="codex", handoff_id=created["handoff_id"], confirm=True, ctx=ctx
        )

    cursor = await db.execute(
        "SELECT status, picked_up_at FROM pending_handoffs WHERE id = ?",
        (created["handoff_id"],),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == "pending"
    assert row["picked_up_at"] is None


async def test_pick_up_codex_operator_activates_one_call(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    created = await fns["create_handoff"](
        caller="claude_ai", project_name="P", source_trust="operator", ctx=ctx
    )
    handoff_id = created["handoff_id"]
    result = await fns["pick_up_handoff"](
        caller="codex", handoff_id=handoff_id, ctx=ctx
    )
    assert result["ok"] is True
    assert result["status"] == "active"

    cursor = await db.execute(
        "SELECT status FROM pending_handoffs WHERE id = ?", (handoff_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == "active"


async def test_pick_up_gate_writes_distinct_audit_events(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    """confirmation_required / refused / allowed each emit a distinct audit event."""
    ctx = make_ctx(db)
    agent = await fns["create_handoff"](
        caller="claude_ai", project_name="Agentish", ctx=ctx
    )
    operator = await fns["create_handoff"](
        caller="claude_ai", project_name="Opish", source_trust="operator", ctx=ctx
    )

    await fns["pick_up_handoff"](caller="cc", handoff_id=agent["handoff_id"], ctx=ctx)
    with pytest.raises(ToolError):
        await fns["pick_up_handoff"](
            caller="codex", handoff_id=agent["handoff_id"], ctx=ctx
        )
    await fns["pick_up_handoff"](
        caller="cc", handoff_id=operator["handoff_id"], ctx=ctx
    )

    events = [
        json.loads(line)
        for line in config.AUDIT_LOG_PATH.read_text(encoding="utf-8").splitlines()
    ]
    details = [e["detail"] for e in events if e["tool"] == "pick_up_handoff"]
    assert (
        len(details) == 3
    )  # exactly one event per pickup call (tmp log isolated by conftest)
    assert any("decision=confirmation_required" in d for d in details)
    assert any("decision=refused" in d for d in details)
    assert any("decision=allowed" in d for d in details)


async def test_clear_handoff_by_project_name(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["create_handoff"](caller="claude_ai", project_name="MyProject", ctx=ctx)

    result = await fns["clear_handoff"](caller="cc", project_name="MyProject", ctx=ctx)
    assert result["ok"] is True
    assert result["cleared"] is True
    assert result["cleared_count"] == 1

    cursor = await db.execute(
        "SELECT status FROM pending_handoffs WHERE project_name='MyProject'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == "cleared"


async def test_clear_handoff_clears_all_matching_rows(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    first = await fns["create_handoff"](
        caller="claude_ai", project_name="MyProject", ctx=ctx
    )
    second = await fns["create_handoff"](
        caller="claude_ai", project_name="MyProject", ctx=ctx
    )
    await fns["pick_up_handoff"](
        caller="cc", handoff_id=second["handoff_id"], confirm=True, ctx=ctx
    )

    result = await fns["clear_handoff"](caller="cc", project_name="MyProject", ctx=ctx)

    assert result["ok"] is True
    assert result["cleared"] is True
    assert result["cleared_count"] == 2
    assert sorted(result["handoff_ids"]) == sorted(
        [first["handoff_id"], second["handoff_id"]]
    )

    cursor = await db.execute(
        "SELECT COUNT(*) FROM pending_handoffs WHERE project_name='MyProject' AND status != 'cleared'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 0


async def test_clear_handoff_missing_project_returns_ok(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    result = await fns["clear_handoff"](
        caller="cc", project_name="DoesNotExist", ctx=ctx
    )
    assert result["ok"] is True
    assert result["cleared"] is False


async def test_clear_handoff_rejects_claude_ai(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    with pytest.raises(ToolError):
        await fns["clear_handoff"](caller="claude_ai", project_name="P", ctx=ctx)


async def test_handoff_lifecycle_across_pending_pickup_and_clear(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    first = await fns["create_handoff"](
        caller="claude_ai",
        project_name="BridgeStatus",
        project_path="/home/user/Projects/bridge-db",
        phase="Phase 5",
        source_trust="operator",
        ctx=ctx,
    )
    second = await fns["create_handoff"](
        caller="claude_ai",
        project_name="BridgeExport",
        project_path="/home/user/Projects/bridge-db",
        phase="Phase 5",
        ctx=ctx,
    )

    pending_before = await fns["get_pending_handoffs"](ctx=ctx)
    assert [handoff["project_name"] for handoff in pending_before] == [
        "BridgeExport",
        "BridgeStatus",
    ]

    picked_up = await fns["pick_up_handoff"](
        caller="codex", handoff_id=first["handoff_id"], ctx=ctx
    )
    assert picked_up["status"] == "active"

    pending_after_pickup = await fns["get_pending_handoffs"](ctx=ctx)
    assert [handoff["project_name"] for handoff in pending_after_pickup] == [
        "BridgeExport"
    ]

    cleared = await fns["clear_handoff"](
        caller="codex", project_name="BridgeStatus", ctx=ctx
    )
    assert cleared["ok"] is True
    assert cleared["cleared_count"] == 1

    pending_after_clear = await fns["get_pending_handoffs"](ctx=ctx)
    assert [handoff["project_name"] for handoff in pending_after_clear] == [
        "BridgeExport"
    ]

    cursor = await db.execute(
        "SELECT status, picked_up_at, cleared_at FROM pending_handoffs WHERE id = ?",
        (first["handoff_id"],),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == "cleared"
    assert row["picked_up_at"] is not None
    assert row["cleared_at"] is not None

    cursor = await db.execute(
        "SELECT status FROM pending_handoffs WHERE id = ?",
        (second["handoff_id"],),
    )
    second_row = await cursor.fetchone()
    assert second_row is not None
    assert second_row["status"] == "pending"


async def test_clear_refuses_other_roles_active_claim(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    created = await fns["create_handoff"](
        caller="claude_ai",
        project_name="ForeignClaim",
        project_path="/tmp/fc",
        phase="Phase 1",
        source_trust="operator",
        ctx=ctx,
    )
    await fns["pick_up_handoff"](
        caller="codex", handoff_id=created["handoff_id"], ctx=ctx
    )

    result = await fns["clear_handoff"](
        caller="cc", project_name="ForeignClaim", ctx=ctx
    )
    assert result["ok"] is True
    assert result["cleared"] is False
    assert result["refused_count"] == 1
    assert result["refused_ids"] == [created["handoff_id"]]
    assert "reason" in result

    cursor = await db.execute(
        "SELECT status FROM pending_handoffs WHERE id = ?", (created["handoff_id"],)
    )
    row = await cursor.fetchone()
    assert row is not None and row["status"] == "active"


async def test_clear_allows_own_active_claim(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    created = await fns["create_handoff"](
        caller="claude_ai",
        project_name="OwnClaim",
        project_path="/tmp/oc",
        phase="Phase 1",
        source_trust="operator",
        ctx=ctx,
    )
    await fns["pick_up_handoff"](
        caller="codex", handoff_id=created["handoff_id"], ctx=ctx
    )

    result = await fns["clear_handoff"](
        caller="codex", project_name="OwnClaim", ctx=ctx
    )
    assert result["cleared"] is True
    assert result["cleared_count"] == 1
    assert result["refused_count"] == 0
    cursor = await db.execute(
        "SELECT status FROM pending_handoffs WHERE id = ?", (created["handoff_id"],)
    )
    row = await cursor.fetchone()
    assert row is not None and row["status"] == "cleared"


async def test_clear_allows_unclaimed_pending(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["create_handoff"](
        caller="claude_ai",
        project_name="NeverClaimed",
        project_path="/tmp/nc",
        phase="Phase 1",
        ctx=ctx,
    )
    result = await fns["clear_handoff"](
        caller="cc", project_name="NeverClaimed", ctx=ctx
    )
    assert result["cleared"] is True
    assert result["refused_count"] == 0


async def test_clear_allows_legacy_null_claimant_active(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    created = await fns["create_handoff"](
        caller="claude_ai",
        project_name="LegacyActive",
        project_path="/tmp/la",
        phase="Phase 1",
        source_trust="operator",
        ctx=ctx,
    )
    await fns["pick_up_handoff"](
        caller="codex", handoff_id=created["handoff_id"], ctx=ctx
    )
    # Simulate a pre-v13 active row: claimant identity was never recorded.
    await db.execute(
        "UPDATE pending_handoffs SET claimed_by = NULL WHERE id = ?",
        (created["handoff_id"],),
    )
    await db.commit()

    result = await fns["clear_handoff"](
        caller="cc", project_name="LegacyActive", ctx=ctx
    )
    assert result["cleared"] is True
    assert result["cleared_count"] == 1


async def test_pick_up_records_claimant(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    created = await fns["create_handoff"](
        caller="claude_ai",
        project_name="ClaimProj",
        project_path="/tmp/claimproj",
        phase="Phase 1",
        source_trust="operator",
        ctx=ctx,
    )
    picked = await fns["pick_up_handoff"](
        caller="codex", handoff_id=created["handoff_id"], ctx=ctx
    )
    assert picked["status"] == "active"
    assert picked["claimed_by"] == "codex"

    cursor = await db.execute(
        "SELECT claimed_by FROM pending_handoffs WHERE id = ?", (created["handoff_id"],)
    )
    row = await cursor.fetchone()
    assert row is not None and row["claimed_by"] == "codex"


async def test_status_freshness_classifies_stale_handoffs_without_mutating_rows(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    pending = await fns["create_handoff"](
        caller="claude_ai", project_name="StalePending", ctx=ctx
    )
    active = await fns["create_handoff"](
        caller="claude_ai",
        project_name="StaleActive",
        source_trust="operator",
        ctx=ctx,
    )
    await fns["pick_up_handoff"](
        caller="codex", handoff_id=active["handoff_id"], ctx=ctx
    )
    await db.execute(
        "UPDATE pending_handoffs SET dispatched_at = ? WHERE id = ?",
        ("2026-01-01T00:00:00Z", pending["handoff_id"]),
    )
    await db.execute(
        "UPDATE pending_handoffs SET picked_up_at = ? WHERE id = ?",
        ("2026-01-02T00:00:00Z", active["handoff_id"]),
    )
    await db.commit()
    before = await _all_handoff_rows(db)

    summary = await collect_status_summary(db, now=datetime(2026, 7, 8, tzinfo=UTC))

    handoffs = summary["freshness"]["handoffs"]
    assert handoffs["pending_count"] == 1
    assert handoffs["stale_pending_count"] == 1
    assert handoffs["active_count"] == 1
    assert handoffs["stale_active_count"] == 1
    assert summary["freshness"]["overall"] == "stale"
    assert any(
        action["action"] == "review_stale_handoff"
        for action in summary["freshness"]["next_actions"]
    )
    assert await _all_handoff_rows(db) == before


async def test_create_handoff_resolves_canonical_key(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECT_REGISTRY_PATH", _registry(tmp_path))
    ctx = make_ctx(db)
    result = await fns["create_handoff"](
        caller="claude_ai", project_name="IncidentMgmt", ctx=ctx
    )
    assert result["canonical_key"] == "saagpatel/IncidentManagement"

    cursor = await db.execute(
        "SELECT canonical_key FROM pending_handoffs WHERE project_name = 'IncidentMgmt'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["canonical_key"] == "saagpatel/IncidentManagement"

    pending = await fns["get_pending_handoffs"](ctx=ctx)
    assert pending[0]["canonical_key"] == "saagpatel/IncidentManagement"


async def test_create_handoff_canonical_key_none_when_registry_absent(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECT_REGISTRY_PATH", tmp_path / "missing.json")
    ctx = make_ctx(db)
    result = await fns["create_handoff"](
        caller="claude_ai", project_name="IncidentMgmt", ctx=ctx
    )
    assert result["canonical_key"] is None

    cursor = await db.execute("SELECT canonical_key FROM pending_handoffs")
    row = await cursor.fetchone()
    assert row is not None
    assert row["canonical_key"] is None


async def test_clear_handoff_matches_canonical_alias(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handoff dispatched as 'IncidentMgmt' clears when /end passes the sibling
    name 'IncidentManagement' — both resolve to the same canonical key (F1)."""
    monkeypatch.setattr(config, "PROJECT_REGISTRY_PATH", _registry(tmp_path))
    ctx = make_ctx(db)
    created = await fns["create_handoff"](
        caller="claude_ai", project_name="IncidentMgmt", ctx=ctx
    )

    # 'IncidentManagement' != the stored project_name, so this clears ONLY via the
    # shared canonical key — proving canonical matching, not string matching.
    result = await fns["clear_handoff"](
        caller="cc", project_name="IncidentManagement", ctx=ctx
    )
    assert result["cleared"] is True
    assert result["cleared_count"] == 1
    assert result["handoff_id"] == created["handoff_id"]
    assert result["canonical_key"] == "saagpatel/IncidentManagement"

    cursor = await db.execute(
        "SELECT status FROM pending_handoffs WHERE id = ?", (created["handoff_id"],)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == "cleared"


async def test_create_handoff_enforce_rejects_unbound_connection(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_db import config as bridge_config
    from bridge_db.tools import handoffs as handoffs_module

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "enforce")
    cap = CaptureMCP()
    handoffs_module.register(cap)
    with pytest.raises(ToolError, match="Unauthenticated connection"):
        await cap.fns["create_handoff"](
            caller="claude_ai",
            project_name="TestProject",
            ctx=make_ctx(db, principal=None),
        )


async def test_clear_handoff_canonical_does_not_overmatch_other_projects(
    db: aiosqlite.Connection,
    fns: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical matching must not clear an unrelated project that resolves to a
    different (or no) canonical key."""
    monkeypatch.setattr(config, "PROJECT_REGISTRY_PATH", _registry(tmp_path))
    ctx = make_ctx(db)
    keep = await fns["create_handoff"](
        caller="claude_ai", project_name="weekly-review", ctx=ctx
    )
    await fns["create_handoff"](
        caller="claude_ai", project_name="IncidentMgmt", ctx=ctx
    )

    result = await fns["clear_handoff"](
        caller="cc", project_name="IncidentManagement", ctx=ctx
    )
    assert result["cleared_count"] == 1

    cursor = await db.execute(
        "SELECT status FROM pending_handoffs WHERE id = ?", (keep["handoff_id"],)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == "pending"


async def test_create_handoff_clamps_operator_label(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import CaptureMCP, make_ctx

    from bridge_db import config as bridge_config
    from bridge_db.tools import handoffs as handoffs_module

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "warn")
    cap = CaptureMCP()
    handoffs_module.register(cap)
    result = await cap.fns["create_handoff"](
        caller="claude_ai",
        project_name="TestProject",
        source_trust="operator",
        ctx=make_ctx(db, principal="claude_ai"),
    )
    assert result["source_trust"] == "agent"
    assert result["source_trust_clamped"] is True
    cursor = await db.execute(
        "SELECT source_trust FROM pending_handoffs WHERE id = ?",
        (result["handoff_id"],),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["source_trust"] == "agent"


async def test_pick_up_handoff_codex_principal_cannot_spoof_cc_caller_in_warn(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A codex-bound connection cannot dodge the Codex refusal by claiming caller='cc'.

    In warn mode require_caller only audits a caller/principal mismatch, so the
    provenance gate must key on the bound principal, not the claimed caller.
    """
    from conftest import CaptureMCP, make_ctx

    from bridge_db import config as bridge_config
    from bridge_db.tools import handoffs as handoffs_module

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "warn")
    cap = CaptureMCP()
    handoffs_module.register(cap)

    # Seed an agent-trust (non-operator) pending handoff via the claude_ai path.
    created = await cap.fns["create_handoff"](
        caller="claude_ai",
        project_name="P",
        ctx=make_ctx(db, principal="claude_ai"),
    )
    handoff_id = created["handoff_id"]
    assert created["source_trust"] == "agent"

    # A codex-BOUND connection claims caller='cc' and tries to confirm-pick-up.
    with pytest.raises(ToolError, match="Codex cannot pick up"):
        await cap.fns["pick_up_handoff"](
            caller="cc",
            handoff_id=handoff_id,
            confirm=True,
            ctx=make_ctx(db, principal="codex"),
        )

    # The handoff stays pending — the spoof did not transition it.
    cursor = await db.execute(
        "SELECT status, picked_up_at FROM pending_handoffs WHERE id = ?", (handoff_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == "pending"
    assert row["picked_up_at"] is None


async def test_pick_up_handoff_off_mode_unbound_gates_on_claimed_caller(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy/off path: with no principal bound, the gate falls back to the claimed caller.

    A cc caller confirming an agent-trust handoff on an unbound connection still
    succeeds, proving the principal fallback preserves current behavior.
    """
    from conftest import CaptureMCP, make_ctx

    from bridge_db import config as bridge_config
    from bridge_db.tools import handoffs as handoffs_module

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "off")
    cap = CaptureMCP()
    handoffs_module.register(cap)

    created = await cap.fns["create_handoff"](
        caller="claude_ai",
        project_name="P",
        ctx=make_ctx(db),  # principal defaults to None (unbound)
    )
    handoff_id = created["handoff_id"]
    assert created["source_trust"] == "agent"

    result = await cap.fns["pick_up_handoff"](
        caller="cc",
        handoff_id=handoff_id,
        confirm=True,
        ctx=make_ctx(db),  # unbound → gate_identity falls back to caller='cc'
    )
    assert result["ok"] is True
    assert result["status"] == "active"
