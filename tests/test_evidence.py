"""Durable-evidence lifecycle boundary and crash-safety tests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from conftest import CaptureMCP, make_ctx

from bridge_db import audit, config
from bridge_db import evidence as evidence_mod
from bridge_db.audit import AuditUnavailableError
from bridge_db.evidence import (
    append_jsonl_durable,
    evidence_disposition_inventory,
    evidence_file_inventory,
    iter_jsonl_family_reverse,
    legacy_raw_query_inventory,
)
from bridge_db.tools import activity as activity_mod
from bridge_db.tools import health as health_mod


def _encoded_size(event: Mapping[str, object]) -> int:
    return len((json.dumps(event, separators=(",", ":")) + "\n").encode())


def test_lossless_rotation_preserves_boundary_records(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    first = {"ts": "2026-01-01T00:00:00Z", "id": 1}
    second = {"ts": "2026-01-02T00:00:00Z", "id": 2}
    boundary = _encoded_size(first) + _encoded_size(second) - 1

    append_jsonl_durable(path, first, rotate_bytes=boundary)
    result = append_jsonl_durable(path, second, rotate_bytes=boundary)

    assert result.rotated_path is not None
    assert json.loads(result.rotated_path.read_text()) == first
    assert json.loads(path.read_text()) == second
    inventory = evidence_file_inventory(path, rotate_bytes=boundary)
    assert inventory["segment_count"] == 1
    assert inventory["retention_policy"] == "preserve_all_pending_approval"
    assert inventory["destructive_cleanup"] == "approval_required"


def test_repeated_rotation_never_overwrites_segments(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    for event_id in range(3):
        append_jsonl_durable(
            path,
            {"id": event_id, "payload": "x" * 64},
            rotate_bytes=80,
        )

    segments = evidence_mod.segment_paths(path)
    assert len(segments) == 2
    assert len({segment.name for segment in segments}) == 2
    readback = list(iter_jsonl_family_reverse(path, max_bytes=10_000))
    assert [record["id"] for record in readback] == [2, 1, 0]


def test_rotation_name_collision_preserves_every_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "events.jsonl"
    monkeypatch.setattr(evidence_mod.time, "time_ns", lambda: 7)
    for event_id in range(3):
        append_jsonl_durable(
            path,
            {"id": event_id, "payload": "x" * 64},
            rotate_bytes=80,
        )

    segments = evidence_mod.segment_paths(path)
    assert len(segments) == 2
    assert {json.loads(segment.read_text())["id"] for segment in segments} == {0, 1}
    assert [
        record["id"] for record in iter_jsonl_family_reverse(path, max_bytes=10_000)
    ] == [2, 1, 0]


def test_crash_after_atomic_rotation_preserves_prior_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "events.jsonl"
    first = {"id": 1, "payload": "x" * 64}
    second = {"id": 2, "payload": "y" * 64}
    append_jsonl_durable(path, first, rotate_bytes=100)
    original_open = evidence_mod.os.open

    def fail_active_append(target: Any, flags: int, mode: int = 0o777) -> int:
        if Path(target) == path:
            raise OSError("simulated crash after rotate")
        return original_open(target, flags, mode)

    monkeypatch.setattr(evidence_mod.os, "open", fail_active_append)
    with pytest.raises(OSError, match="simulated crash"):
        append_jsonl_durable(path, second, rotate_bytes=100)

    segments = evidence_mod.segment_paths(path)
    assert len(segments) == 1
    assert json.loads(segments[0].read_text()) == first
    assert not path.exists()

    monkeypatch.setattr(evidence_mod.os, "open", original_open)
    append_jsonl_durable(path, second, rotate_bytes=100)
    assert [record["id"] for record in iter_jsonl_family_reverse(path, max_bytes=10_000)] == [
        2,
        1,
    ]


def test_audit_primary_failure_continues_with_durable_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("file", encoding="utf-8")
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", blocked_parent / "audit.jsonl")
    receipt_path = tmp_path / "audit_failures.jsonl"
    monkeypatch.setattr(config, "AUDIT_FAILURE_LOG_PATH", receipt_path)

    result = audit.log_audit(
        "record_disposition", "codex", "Project", ok=True, detail="sensitive-ref"
    )

    assert result["audit_degraded"] is True
    receipt = json.loads(receipt_path.read_text())
    assert receipt["kind"] == "audit_write_failure"
    assert receipt["tool"] == "record_disposition"
    assert receipt["status"] == "open"
    assert "sensitive-ref" not in receipt_path.read_text()


def test_audit_raises_when_primary_and_failure_receipt_are_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("file", encoding="utf-8")
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", blocked_parent / "audit.jsonl")
    monkeypatch.setattr(
        config, "AUDIT_FAILURE_LOG_PATH", blocked_parent / "failures.jsonl"
    )

    with pytest.raises(AuditUnavailableError, match="must not claim"):
        audit.log_audit("auth.mismatch", "cc", None, ok=False)


async def test_mutation_continues_only_with_durable_audit_failure_evidence(
    db: aiosqlite.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("file", encoding="utf-8")
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", blocked_parent / "audit.jsonl")
    receipt_path = tmp_path / "audit_failures.jsonl"
    monkeypatch.setattr(config, "AUDIT_FAILURE_LOG_PATH", receipt_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    (tmp_path / "test.db").touch()
    bridge_path = tmp_path / "bridge.md"
    bridge_path.write_text("# BridgeDB\n", encoding="utf-8")
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge_path)
    await db.execute(
        "INSERT INTO bridge_file_export_state "
        "(singleton, exported_content_sha256) VALUES (1, 'test')"
    )
    await db.commit()
    cap = CaptureMCP()
    activity_mod.register(cap)
    health_mod.register(cap)

    result = await cap.fns["log_activity"](
        caller="cc",
        project_name="audit-degraded",
        summary="canonical mutation still commits",
        ctx=make_ctx(db, principal="cc"),
    )

    assert result["ok"] is True
    row = await (
        await db.execute(
            "SELECT summary FROM activity_log WHERE project_name = 'audit-degraded'"
        )
    ).fetchone()
    assert row is not None and row["summary"] == "canonical mutation still commits"
    receipts = [
        json.loads(line) for line in receipt_path.read_text().splitlines()
    ]
    assert any(receipt["tool"] == "log_activity" for receipt in receipts)
    health = await cap.fns["health"](ctx=make_ctx(db))
    assert health["storage_ok"] is False
    assert health["evidence_lifecycle"]["audit_degraded"] is True


def test_legacy_query_inventory_never_rediscloses_query_text(tmp_path: Path) -> None:
    path = tmp_path / "recall.jsonl"
    sentinel = "private historical query"
    append_jsonl_durable(
        path,
        {"ts": "2026-01-01T00:00:00Z", "query": sentinel},
        rotate_bytes=1,
    )
    append_jsonl_durable(
        path,
        {"ts": "2026-01-02T00:00:00Z", "query_empty": False},
        rotate_bytes=1,
    )

    inventory = legacy_raw_query_inventory(path)

    assert inventory["raw_query_records"] == 1
    assert inventory["cleanup"] == "approval_required"
    assert sentinel not in json.dumps(inventory)


def test_disposition_inventory_flags_open_prepared_transaction(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evidence_dispositions.jsonl"
    append_jsonl_durable(
        path,
        {"transaction_id": "tx-open", "status": "prepared"},
        rotate_bytes=1,
    )

    inventory = evidence_disposition_inventory(path)

    assert inventory["transaction_count"] == 1
    assert inventory["open_count"] == 1
    assert inventory["completed_count"] == 0
    assert inventory["state"] == "degraded"
    assert inventory["destructive_cleanup"] == "approval_required"


def test_disposition_inventory_uses_latest_transaction_state_across_rotation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evidence_dispositions.jsonl"
    append_jsonl_durable(
        path,
        {"transaction_id": "tx-complete", "status": "prepared"},
        rotate_bytes=1,
    )
    append_jsonl_durable(
        path,
        {"transaction_id": "tx-complete", "status": "completed"},
        rotate_bytes=1,
    )

    inventory = evidence_disposition_inventory(path)

    assert inventory["transaction_count"] == 1
    assert inventory["open_count"] == 0
    assert inventory["completed_count"] == 1
    assert inventory["state"] == "clear"


def test_disposition_inventory_never_claims_clear_from_truncated_scan(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evidence_dispositions.jsonl"
    append_jsonl_durable(
        path,
        {"transaction_id": "tx-complete", "status": "completed"},
        rotate_bytes=1024,
    )

    inventory = evidence_disposition_inventory(path, scan_bytes=1)

    assert inventory["scan_truncated"] is True
    assert inventory["state"] == "degraded"
