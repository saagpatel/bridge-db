"""RecoverySealReceiptV1 lifecycle, interruption, and concurrency coverage."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from bridge_db import recovery, recovery_seal
from bridge_db.db import SCHEMA_VERSION, open_db


async def _source_database(tmp_path: Path) -> Path:
    db_path = tmp_path / "bridge.db"
    db = await open_db(db_path)
    await db.execute(
        "INSERT INTO context_sections (section_name, owner, content) "
        "VALUES ('career', 'codex', 'recovery seal fixture')"
    )
    await db.commit()
    await db.close()
    return db_path


def _change_source(db_path: Path, summary: str) -> None:
    with sqlite3.connect(db_path) as changed:
        changed.execute(
            "INSERT INTO activity_log "
            "(source, timestamp, project_name, summary) VALUES (?, ?, ?, ?)",
            ("codex", "2026-07-30T12:00:00Z", "bridge-db", summary),
        )


async def test_successful_seal_rotates_once_and_publishes_verified_receipt(
    tmp_path: Path,
) -> None:
    db_path = await _source_database(tmp_path)
    original = recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    _change_source(db_path, "completed batch")

    result = recovery_seal.seal_recovery_batch(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
        batch_id="codex-20260730-001",
        owner="codex",
    )

    assert result["outcome"] == "recovery_sealed"
    assert result["reason_code"] == "verified_current_anchor"
    assert result["seal_owner"] == "codex"
    assert result["rotated"] is True
    assert result["ready"] is True
    assert result["digest_ok"] is True
    assert result["integrity_ok"] is True
    assert result["semantic_readback_ok"] is True
    assert result["source_current"] is True
    assert result["replayed"] is False
    assert result["superseded_sha256"] == original["sha256"]

    root = recovery_seal.recovery_seal_path(db_path)
    batch = root / recovery_seal._batch_key(  # pyright: ignore[reportPrivateUsage]
        "codex-20260730-001"
    )
    receipt = recovery_seal.verify_recovery_seal_receipt(
        batch / recovery_seal.RECOVERY_SEAL_RECEIPT_NAME
    )
    assert receipt["receipt_sha256"] == result["receipt_sha256"]
    assert root.stat().st_mode & 0o777 == 0o700
    assert batch.stat().st_mode & 0o777 == 0o700
    assert (
        batch.joinpath(recovery_seal.RECOVERY_SEAL_ATTEMPT_NAME).stat().st_mode
        & 0o777
        == 0o600
    )
    assert (
        batch.joinpath(recovery_seal.RECOVERY_SEAL_RECEIPT_NAME).stat().st_mode
        & 0o777
        == 0o600
    )

    inventory = recovery_seal.recovery_seal_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    assert inventory["state"] == "verified"
    assert inventory["ready"] is True
    assert inventory["attempt_count"] == 1
    assert inventory["sealed_count"] == 1
    assert inventory["unsealed_count"] == 0
    assert inventory["open_count"] == 0
    assert inventory["latest"]["batch_id"] == "codex-20260730-001"


async def test_successful_seal_replaces_verified_older_schema_anchor(
    tmp_path: Path,
) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    anchor = recovery.recovery_anchor_path(db_path)
    database = anchor / recovery.RECOVERY_DATABASE_NAME
    prior_schema_version = SCHEMA_VERSION - 1
    with sqlite3.connect(database) as changed:
        changed.execute(f"PRAGMA user_version = {prior_schema_version}")
    manifest_path = anchor / recovery.RECOVERY_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_schema_version"] = prior_schema_version
    manifest["backup_bytes"] = database.stat().st_size
    manifest["sha256"] = hashlib.sha256(database.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = recovery_seal.seal_recovery_batch(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
        batch_id="schema-transition-001",
        owner="codex",
    )

    assert result["outcome"] == "recovery_sealed"
    assert result["reason_code"] == "verified_current_anchor"
    assert result["rotated"] is True
    assert result["ready"] is True
    assert result["source_current"] is True
    assert result["superseded_sha256"] == manifest["sha256"]
    inventory = recovery_seal.recovery_seal_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    assert inventory["state"] == "verified"
    assert inventory["ready"] is True


async def test_repeated_seal_replays_one_terminal_receipt(
    tmp_path: Path,
) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    first = recovery_seal.seal_recovery_batch(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
        batch_id="repeat-001",
        owner="codex",
    )
    second = recovery_seal.seal_recovery_batch(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
        batch_id="repeat-001",
        owner="codex",
    )

    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["receipt_sha256"] == first["receipt_sha256"]
    assert second["anchor_sha256"] == first["anchor_sha256"]
    receipt_paths = list(
        recovery_seal.recovery_seal_path(db_path).glob(
            f"*/{recovery_seal.RECOVERY_SEAL_RECEIPT_NAME}"
        )
    )
    assert len(receipt_paths) == 1
    assert list(tmp_path.glob("bridge.db.recovery-anchor-v1.superseded-*")) == []


async def test_replayed_sealed_receipt_rechecks_current_source(
    tmp_path: Path,
) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    first = recovery_seal.seal_recovery_batch(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
        batch_id="stale-replay-001",
        owner="codex",
    )
    _change_source(db_path, "source advanced after terminal receipt")

    with pytest.raises(
        recovery_seal.RecoverySealProtocolError,
        match="source_changed_since_recovery_seal",
    ):
        recovery_seal.seal_recovery_batch(
            db_path,
            expected_schema_version=SCHEMA_VERSION,
            batch_id="stale-replay-001",
            owner="codex",
        )

    inventory = recovery_seal.recovery_seal_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    assert inventory["state"] == "stale"
    assert inventory["ready"] is False
    assert inventory["latest"]["receipt_sha256"] == first["receipt_sha256"]


async def test_concurrent_seals_serialize_and_rotate_once(tmp_path: Path) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    _change_source(db_path, "concurrent completed batch")
    barrier = Barrier(2)

    def run() -> dict[str, Any]:
        barrier.wait()
        return recovery_seal.seal_recovery_batch(
            db_path,
            expected_schema_version=SCHEMA_VERSION,
            batch_id="concurrent-001",
            owner="codex",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result() for future in (executor.submit(run), executor.submit(run))]

    assert sorted(result["replayed"] for result in results) == [False, True]
    assert len({result["receipt_sha256"] for result in results}) == 1
    assert len(
        list(tmp_path.glob("bridge.db.recovery-anchor-v1.superseded-*"))
    ) == 1
    inventory = recovery_seal.recovery_seal_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    assert inventory["attempt_count"] == 1
    assert inventory["sealed_count"] == 1


async def test_interrupted_seal_publishes_recovery_unsealed_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    def interrupt_rotation(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        recovery_seal,
        "rotate_recovery_anchor",
        interrupt_rotation,
    )

    with pytest.raises(KeyboardInterrupt):
        recovery_seal.seal_recovery_batch(
            db_path,
            expected_schema_version=SCHEMA_VERSION,
            batch_id="interrupted-001",
            owner="codex",
        )

    inventory = recovery_seal.recovery_seal_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    assert inventory["state"] == "recovery_unsealed"
    assert inventory["ready"] is False
    assert inventory["open_count"] == 0
    assert inventory["unsealed_count"] == 1
    assert inventory["latest"]["reason_code"] == "seal_interrupted"


async def test_incomplete_attempt_fails_closed_if_source_changes_before_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    original_publish = (
        recovery_seal._publish_record  # pyright: ignore[reportPrivateUsage]
    )

    def fail_rotation(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulated rotation interruption")

    def refuse_terminal(path: Path, record: dict[str, Any]) -> None:
        if path.name == recovery_seal.RECOVERY_SEAL_RECEIPT_NAME:
            raise OSError("simulated terminal receipt outage")
        original_publish(path, record)

    with monkeypatch.context() as patch:
        patch.setattr(recovery_seal, "rotate_recovery_anchor", fail_rotation)
        patch.setattr(recovery_seal, "_publish_record", refuse_terminal)
        with pytest.raises(
            recovery_seal.RecoverySealProtocolError,
            match="terminal_receipt_unavailable",
        ):
            recovery_seal.seal_recovery_batch(
                db_path,
                expected_schema_version=SCHEMA_VERSION,
                batch_id="stale-retry-001",
                owner="codex",
            )

    open_inventory = recovery_seal.recovery_seal_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    assert open_inventory["state"] == "recovery_unsealed"
    assert open_inventory["open_count"] == 1
    assert open_inventory["latest"]["reason_code"] == "seal_attempt_incomplete"

    _change_source(db_path, "source advanced after interrupted seal")
    result = recovery_seal.seal_recovery_batch(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
        batch_id="stale-retry-001",
        owner="codex",
    )

    assert result["outcome"] == "recovery_unsealed"
    assert result["reason_code"] == "source_changed_since_seal_attempt"
    assert result["ready"] is False
    assert result["replayed"] is False


async def test_failed_readback_returns_content_bound_unsealed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    def fail_readback(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise recovery_seal.RecoverySealProtocolError("anchor_readback_failed")

    monkeypatch.setattr(
        recovery_seal,
        "rotate_recovery_anchor",
        fail_readback,
    )

    result = recovery_seal.seal_recovery_batch(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
        batch_id="readback-failed-001",
        owner="codex",
    )

    assert result["outcome"] == "recovery_unsealed"
    assert result["reason_code"] == "anchor_readback_failed"
    assert result["ready"] is False
    receipt_paths = list(
        recovery_seal.recovery_seal_path(db_path).glob(
            f"*/{recovery_seal.RECOVERY_SEAL_RECEIPT_NAME}"
        )
    )
    assert len(receipt_paths) == 1
    assert (
        recovery_seal.verify_recovery_seal_receipt(receipt_paths[0])[
            "receipt_sha256"
        ]
        == result["receipt_sha256"]
    )


async def test_visible_receipt_after_post_link_failure_returns_unsealed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    _change_source(db_path, "source advanced before rollback window")
    original_publish = (
        recovery_seal._publish_record  # pyright: ignore[reportPrivateUsage]
    )
    original_fsync = (
        recovery_seal._fsync_directory  # pyright: ignore[reportPrivateUsage]
    )
    batch_name = recovery_seal._batch_key(  # pyright: ignore[reportPrivateUsage]
        "rollback-terminal-001"
    )
    failed_terminal_sync = False

    def publish_with_terminal_fsync_failure(
        path: Path,
        record: dict[str, Any],
    ) -> None:
        nonlocal failed_terminal_sync
        if (
            path.name == recovery_seal.RECOVERY_SEAL_RECEIPT_NAME
            and record.get("outcome") == "recovery_sealed"
            and not failed_terminal_sync
        ):

            def fail_terminal_batch_fsync(directory: Path) -> None:
                nonlocal failed_terminal_sync
                if not failed_terminal_sync and directory.name == batch_name:
                    failed_terminal_sync = True
                    raise OSError("simulated post-link terminal fsync failure")
                original_fsync(directory)

            monkeypatch.setattr(
                recovery_seal,
                "_fsync_directory",
                fail_terminal_batch_fsync,
            )
            try:
                original_publish(path, record)
            finally:
                monkeypatch.setattr(
                    recovery_seal,
                    "_fsync_directory",
                    original_fsync,
                )
            return
        original_publish(path, record)

    monkeypatch.setattr(
        recovery_seal,
        "_publish_record",
        publish_with_terminal_fsync_failure,
    )

    result = recovery_seal.seal_recovery_batch(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
        batch_id="rollback-terminal-001",
        owner="codex",
    )

    assert failed_terminal_sync is True
    assert result["outcome"] == "recovery_unsealed"
    assert result["reason_code"] == "recovery_io_failed"
    assert result["ready"] is False
    terminal = (
        recovery_seal.recovery_seal_path(db_path)
        / batch_name
        / recovery_seal.RECOVERY_SEAL_RECEIPT_NAME
    )
    assert recovery_seal.verify_recovery_seal_receipt(terminal)["outcome"] == (
        "recovery_unsealed"
    )
    inventory = recovery_seal.recovery_seal_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    assert inventory["state"] == "recovery_unsealed"
    assert inventory["sealed_count"] == 0
    assert inventory["unsealed_count"] == 1


async def test_unauthorized_owner_cannot_create_attempt(tmp_path: Path) -> None:
    db_path = await _source_database(tmp_path)

    with pytest.raises(
        recovery_seal.RecoverySealProtocolError,
        match="seal_owner_unauthorized",
    ):
        recovery_seal.seal_recovery_batch(
            db_path,
            expected_schema_version=SCHEMA_VERSION,
            batch_id="unauthorized-001",
            owner="claude_ai",
        )

    assert not recovery_seal.recovery_seal_path(db_path).exists()


async def test_existing_batch_cannot_be_reclaimed_by_other_owner(
    tmp_path: Path,
) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    first = recovery_seal.seal_recovery_batch(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
        batch_id="owned-001",
        owner="codex",
    )

    with pytest.raises(
        recovery_seal.RecoverySealProtocolError,
        match="batch_owner_mismatch",
    ):
        recovery_seal.seal_recovery_batch(
            db_path,
            expected_schema_version=SCHEMA_VERSION,
            batch_id="owned-001",
            owner="cc",
        )

    inventory = recovery_seal.recovery_seal_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    assert inventory["attempt_count"] == 1
    assert inventory["sealed_count"] == 1
    assert inventory["latest"]["receipt_sha256"] == first["receipt_sha256"]


async def test_capacity_refuses_new_batch_without_deleting_existing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recovery_seal, "RECOVERY_SEAL_MAX_BATCHES", 2)
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    first = recovery_seal.seal_recovery_batch(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
        batch_id="capacity-001",
        owner="codex",
    )
    second = recovery_seal.seal_recovery_batch(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
        batch_id="capacity-002",
        owner="codex",
    )

    replay = recovery_seal.seal_recovery_batch(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
        batch_id="capacity-001",
        owner="codex",
    )
    with pytest.raises(
        recovery_seal.RecoverySealProtocolError,
        match="recovery_seal_capacity_exceeded",
    ):
        recovery_seal.seal_recovery_batch(
            db_path,
            expected_schema_version=SCHEMA_VERSION,
            batch_id="capacity-003",
            owner="codex",
        )

    root = recovery_seal.recovery_seal_path(db_path)
    assert len([path for path in root.iterdir() if path.is_dir()]) == 2
    assert replay["replayed"] is True
    assert replay["receipt_sha256"] == first["receipt_sha256"]
    inventory = recovery_seal.recovery_seal_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    assert inventory["state"] == "verified"
    assert inventory["attempt_count"] == 2
    assert inventory["sealed_count"] == 2
    assert inventory["latest"]["receipt_sha256"] in {
        first["receipt_sha256"],
        second["receipt_sha256"],
    }


async def test_inventory_fails_closed_when_retained_batches_exceed_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recovery_seal, "RECOVERY_SEAL_MAX_BATCHES", 2)
    db_path = await _source_database(tmp_path)
    root = recovery_seal.recovery_seal_path(db_path)
    root.mkdir(mode=0o700)
    for prefix in ("a", "b", "c"):
        root.joinpath(prefix * 64).mkdir(mode=0o700)

    inventory = recovery_seal.recovery_seal_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert inventory["state"] == "invalid"
    assert inventory["ready"] is False
    assert inventory["invalid_count"] == 1
    assert inventory["errors"] == ["recovery_seal_capacity_exceeded"]


async def test_inventory_race_does_not_recreate_seal_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = await _source_database(tmp_path)
    root = recovery_seal.recovery_seal_path(db_path)
    moved_root = tmp_path / "moved-recovery-seals"
    root.mkdir(mode=0o700)
    root.joinpath("marker.txt").write_text("preserved evidence", encoding="utf-8")
    original_validate = (
        recovery_seal._validate_private_directory  # pyright: ignore[reportPrivateUsage]
    )

    def move_before_validation(path: Path) -> None:
        if path == root and root.exists():
            root.rename(moved_root)
        original_validate(path)

    monkeypatch.setattr(
        recovery_seal,
        "_validate_private_directory",
        move_before_validation,
    )

    inventory = recovery_seal.recovery_seal_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    assert inventory["state"] == "invalid"
    assert inventory["ready"] is False
    assert inventory["errors"] == ["receipt_directory_missing"]
    assert not root.exists()
    assert moved_root.joinpath("marker.txt").read_text(encoding="utf-8") == (
        "preserved evidence"
    )


async def test_tampered_terminal_receipt_invalidates_inventory(
    tmp_path: Path,
) -> None:
    db_path = await _source_database(tmp_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    recovery_seal.seal_recovery_batch(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
        batch_id="tampered-001",
        owner="codex",
    )
    terminal = (
        recovery_seal.recovery_seal_path(db_path)
        / recovery_seal._batch_key(  # pyright: ignore[reportPrivateUsage]
            "tampered-001"
        )
        / recovery_seal.RECOVERY_SEAL_RECEIPT_NAME
    )
    terminal.write_bytes(
        terminal.read_bytes().replace(
            b'"outcome":"recovery_sealed"',
            b'"outcome":"recovery_unsealed"',
            1,
        )
    )

    with pytest.raises(
        recovery_seal.RecoverySealProtocolError,
        match="receipt_digest_mismatch",
    ):
        recovery_seal.verify_recovery_seal_receipt(terminal)

    inventory = recovery_seal.recovery_seal_inventory(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    assert inventory["state"] == "invalid"
    assert inventory["ready"] is False
    assert inventory["invalid_count"] == 1
    assert inventory["errors"] == ["receipt_digest_mismatch"]
