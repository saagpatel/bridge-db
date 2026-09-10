"""Real-boundary regressions for the security capacity-policy bundle."""

from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx
from mcp.server.fastmcp.exceptions import ToolError

from bridge_db import config
from bridge_db.db import record_write_conflict
from bridge_db.tools import activity, conflicts, context, handoffs, snapshots


def _tools(module: Any) -> dict[str, Any]:
    cap = CaptureMCP()
    module.register(cap)
    return cap.fns


async def test_protected_activity_rejects_oversized_payload_without_mutation(
    db: aiosqlite.Connection,
) -> None:
    fns = _tools(activity)
    ctx = make_ctx(db, principal="codex")

    with pytest.raises(ToolError, match="activity.summary_utf8_bytes_exceeded"):
        await fns["log_activity"](
            caller="codex",
            project_name="bridge-db",
            summary="x" * (config.ACTIVITY_SUMMARY_MAX_BYTES + 1),
            tags=["LEDGER"],
            ctx=ctx,
        )

    row = await (await db.execute("SELECT COUNT(*) FROM activity_log")).fetchone()
    assert row is not None and row[0] == 0

    accepted = await fns["log_activity"](
        caller="codex",
        project_name="bridge-db",
        summary="x" * config.ACTIVITY_SUMMARY_MAX_BYTES,
        tags=["SHIPPED"],
        ctx=ctx,
    )
    assert accepted["ok"] is True


async def test_activity_combined_budget_rejects_before_write(
    db: aiosqlite.Connection,
) -> None:
    fns = _tools(activity)
    ctx = make_ctx(db, principal="codex")
    with pytest.raises(ToolError, match="activity.combined_utf8_bytes_exceeded"):
        await fns["log_activity"](
            caller="codex",
            project_name="p" * config.ACTIVITY_PROJECT_NAME_MAX_BYTES,
            summary="s" * config.ACTIVITY_SUMMARY_MAX_BYTES,
            branch="b" * config.ACTIVITY_BRANCH_MAX_BYTES,
            tags=["t" * config.ACTIVITY_TAG_MAX_BYTES]
            * config.ACTIVITY_TAGS_MAX_ITEMS,
            timestamp="2" * config.ACTIVITY_TIMESTAMP_MAX_BYTES,
            ctx=ctx,
        )
    row = await (await db.execute("SELECT COUNT(*) FROM activity_log")).fetchone()
    assert row is not None and row[0] == 0


async def test_protected_activity_quota_is_atomic_and_does_not_prune(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "ACTIVITY_PROTECTED_PER_SOURCE_QUOTA", 1)
    fns = _tools(activity)
    ctx = make_ctx(db, principal="codex")
    await fns["log_activity"](
        caller="codex",
        project_name="first",
        summary="first durable row",
        tags=["LEDGER"],
        ctx=ctx,
    )
    with pytest.raises(
        ToolError, match="activity.protected_per_source_quota_exceeded"
    ):
        await fns["log_activity"](
            caller="codex",
            project_name="second",
            summary="second durable row",
            tags=["SHIPPED"],
            ctx=ctx,
        )
    rows = await (
        await db.execute("SELECT project_name FROM activity_log")
    ).fetchall()
    assert [row["project_name"] for row in rows] == ["first"]


async def test_handoff_field_quota_and_pagination_are_bounded(
    db: aiosqlite.Connection,
) -> None:
    fns = _tools(handoffs)
    ctx = make_ctx(db, principal="claude_ai")
    with pytest.raises(ToolError, match="handoff.phase_utf8_bytes_exceeded"):
        await fns["create_handoff"](
            caller="claude_ai",
            project_name="bridge-db",
            phase="x" * (config.HANDOFF_PHASE_MAX_BYTES + 1),
            ctx=ctx,
        )

    await db.executemany(
        "INSERT INTO pending_handoffs (project_name) VALUES (?)",
        [(f"project-{i}",) for i in range(config.HANDOFF_OPEN_QUOTA)],
    )
    await db.commit()
    with pytest.raises(ToolError, match="handoff.open_queue_quota_exceeded"):
        await fns["create_handoff"](
            caller="claude_ai", project_name="overflow", ctx=ctx
        )
    count = await (
        await db.execute(
            "SELECT COUNT(*) FROM pending_handoffs WHERE status IN ('pending', 'active')"
        )
    ).fetchone()
    assert count is not None and count[0] == config.HANDOFF_OPEN_QUOTA

    first_page = await fns["get_pending_handoffs"](limit=2, ctx=ctx)
    assert len(first_page) == 2
    second_page = await fns["get_pending_handoffs"](
        limit=2, before_id=first_page[-1]["id"], ctx=ctx
    )
    assert len(second_page) == 2
    assert second_page[0]["id"] < first_page[-1]["id"]


async def test_handoff_history_quota_is_non_destructive(
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "HANDOFF_TOTAL_ROWS_QUOTA", 3)
    await db.executemany(
        """
        INSERT INTO pending_handoffs (project_name, status, cleared_at)
        VALUES (?, 'cleared', CURRENT_TIMESTAMP)
        """,
        [(f"legacy-{i}",) for i in range(3)],
    )
    await db.commit()

    fns = _tools(handoffs)
    ctx = make_ctx(db, principal="claude_ai")
    with pytest.raises(ToolError, match="handoff.total_row_quota_exceeded"):
        await fns["create_handoff"](
            caller="claude_ai", project_name="overflow", ctx=ctx
        )

    rows = await (
        await db.execute(
            "SELECT project_name, status FROM pending_handoffs ORDER BY id"
        )
    ).fetchall()
    assert [(row["project_name"], row["status"]) for row in rows] == [
        ("legacy-0", "cleared"),
        ("legacy-1", "cleared"),
        ("legacy-2", "cleared"),
    ]


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (
            {"value": "x" * config.SNAPSHOT_JSON_MAX_BYTES},
            "snapshot.json_utf8_bytes_exceeded",
        ),
        (
            {"root": [[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[["x"]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]},
            "snapshot.json_depth_exceeded",
        ),
        (
            {"items": list(range(config.SNAPSHOT_JSON_MAX_NODES))},
            "snapshot.json_nodes_exceeded",
        ),
    ],
)
async def test_snapshot_budgets_reject_without_pruning_or_insert(
    db: aiosqlite.Connection, payload: dict[str, Any], error: str
) -> None:
    fns = _tools(snapshots)
    with pytest.raises(ToolError, match=error):
        await fns["save_snapshot"](
            caller="codex", data=payload, ctx=make_ctx(db, principal="codex")
        )
    row = await (await db.execute("SELECT COUNT(*) FROM system_snapshots")).fetchone()
    assert row is not None and row[0] == 0


async def test_oversized_legacy_snapshot_remains_readable(
    db: aiosqlite.Connection,
) -> None:
    legacy = '{"legacy":"' + ("x" * (config.SNAPSHOT_JSON_MAX_BYTES + 1)) + '"}'
    await db.execute(
        "INSERT INTO system_snapshots (system, snapshot_date, data) VALUES ('codex', '2026-01-01', ?)",
        (legacy,),
    )
    await db.commit()

    latest = await _tools(snapshots)["get_latest_snapshot"](system="codex", ctx=make_ctx(db))
    assert latest["data"]["legacy"].startswith("x")
    assert len(latest["data"]["legacy"]) > config.SNAPSHOT_JSON_MAX_BYTES


async def test_snapshot_exact_utf8_boundary_is_accepted(
    db: aiosqlite.Connection,
) -> None:
    # Compact JSON is exactly: {"v":"<payload>"} (8 bytes of framing).
    payload = {"v": "x" * (config.SNAPSHOT_JSON_MAX_BYTES - 8)}
    result = await _tools(snapshots)["save_snapshot"](
        caller="codex", data=payload, ctx=make_ctx(db, principal="codex")
    )
    assert result["ok"] is True


async def test_context_per_section_and_combined_budgets_preserve_legacy_rows(
    db: aiosqlite.Connection,
) -> None:
    fns = _tools(context)
    ctx = make_ctx(db, principal="claude_ai")
    legacy = "x" * (config.CONTEXT_SECTION_MAX_BYTES + 1)
    await db.execute(
        "INSERT INTO context_sections (section_name, owner, content) "
        "VALUES ('career', 'claude_ai', ?)",
        (legacy,),
    )
    await db.commit()

    stored = await fns["get_section"](section_name="career", ctx=ctx)
    assert stored["content"] == legacy
    with pytest.raises(ToolError, match="context.section_utf8_bytes_exceeded"):
        await fns["update_section"](
            caller="claude_ai",
            section_name="career",
            content=legacy,
            if_match_version=stored["version"],
            ctx=ctx,
        )
    unchanged = await fns["get_section"](section_name="career", ctx=ctx)
    assert unchanged["content"] == legacy
    recovered = await fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="recovered",
        if_match_version=stored["version"],
        ctx=ctx,
    )
    assert recovered["ok"] is True

    await db.execute("DELETE FROM context_sections")
    await db.executemany(
        "INSERT INTO context_sections (section_name, owner, content) VALUES (?, 'claude_ai', ?)",
        [
            ("career", "a" * config.CONTEXT_SECTION_MAX_BYTES),
            ("speaking", "b" * config.CONTEXT_SECTION_MAX_BYTES),
            ("research", "c" * config.CONTEXT_SECTION_MAX_BYTES),
            ("capabilities", "d" * config.CONTEXT_SECTION_MAX_BYTES),
        ],
    )
    await db.commit()
    with pytest.raises(ToolError, match="context.total_utf8_bytes_exceeded"):
        await fns["update_section"](
            caller="cc",
            section_name="portfolio",
            content="p",
            ctx=make_ctx(db, principal="cc"),
        )


async def test_write_conflicts_aggregate_exact_identity_without_losing_variants(
    db: aiosqlite.Connection,
) -> None:
    async def record(digest: str) -> int:
        return await record_write_conflict(
            db,
            surface="context_section",
            target_key="career",
            operation="update_section",
            attempted_by="claude_ai",
            principal="claude_ai",
            stale_version=1,
            current_version=2,
            reason="stale_cas",
            attempted_content_sha256=digest,
        )

    first = await record("a" * 64)
    repeat = await record("a" * 64)
    variant = await record("b" * 64)
    await db.commit()

    assert repeat == first
    assert variant != first
    rows = await _tools(conflicts)["get_write_conflicts"](
        status="open", ctx=make_ctx(db)
    )
    by_id = {row["id"]: row for row in rows}
    assert by_id[first]["occurrence_count"] == 2
    assert by_id[first]["aggregation_state"] == "exact_identity"
    assert by_id[variant]["occurrence_count"] == 1


async def test_write_conflict_identity_overflow_and_detail_truncation_are_explicit(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "WRITE_CONFLICT_MAX_IDENTITIES", 1)
    first = await record_write_conflict(
        db,
        surface="context_section",
        target_key="career",
        operation="update_section",
        reason="stale_cas",
        detail={"large": "x" * (config.WRITE_CONFLICT_DETAIL_MAX_BYTES + 1)},
    )
    overflow = await record_write_conflict(
        db,
        surface="context_section",
        target_key="speaking",
        operation="update_section",
        reason="stale_cas",
    )
    overflow_repeat = await record_write_conflict(
        db,
        surface="context_section",
        target_key="research",
        operation="update_section",
        reason="stale_cas",
    )
    await db.commit()

    assert overflow_repeat == overflow
    rows = await _tools(conflicts)["get_write_conflicts"](
        status="open", ctx=make_ctx(db)
    )
    by_id = {row["id"]: row for row in rows}
    assert by_id[first]["detail"]["detail_truncated"] is True
    assert by_id[overflow]["aggregation_state"] == "capacity_overflow"
    assert by_id[overflow]["occurrence_count"] == 2
    assert by_id[overflow]["detail"]["detail_truncated"] is True


async def test_v21_migration_preserves_legacy_conflict_rows(tmp_path: Any) -> None:
    from bridge_db.db import SCHEMA_VERSION, open_db

    db_path = tmp_path / "v20.db"
    legacy = await aiosqlite.connect(db_path)
    await legacy.executescript(
        """
        CREATE TABLE write_conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            surface TEXT NOT NULL,
            target_key TEXT NOT NULL,
            operation TEXT NOT NULL,
            attempted_by TEXT,
            principal TEXT,
            stale_version INTEGER,
            current_version INTEGER,
            stale_updated_at TEXT,
            current_updated_at TEXT,
            attempted_source_trust TEXT,
            current_source_trust TEXT,
            attempted_content_sha256 TEXT,
            current_content_sha256 TEXT,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            detail_json TEXT,
            created_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00Z'
        );
        INSERT INTO write_conflicts
            (surface, target_key, operation, reason, detail_json)
        VALUES ('context_section', 'career', 'update_section', 'stale_cas', '{}');
        PRAGMA user_version = 20;
        """
    )
    await legacy.commit()
    await legacy.close()

    migrated = await open_db(db_path)
    try:
        version = await (await migrated.execute("PRAGMA user_version")).fetchone()
        row = await (
            await migrated.execute(
                "SELECT target_key, identity_hash, occurrence_count, aggregation_state "
                "FROM write_conflicts"
            )
        ).fetchone()
        assert version is not None and version[0] == SCHEMA_VERSION
        assert row is not None
        assert row["target_key"] == "career"
        assert row["identity_hash"] is None
        assert row["occurrence_count"] == 1
        assert row["aggregation_state"] == "legacy"
    finally:
        await migrated.close()
