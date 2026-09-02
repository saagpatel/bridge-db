"""Exact-resource owner-delegation contract tests."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

from bridge_db.owner_delegation import (
    DELEGATION_MANIFEST_SCHEMA,
    OwnerDelegationError,
    apply_owner_delegation_manifest,
    consume_owner_delegation,
    load_owner_delegation_manifest,
    owner_resource_snapshot,
    resolve_owner_delegation,
)


async def _activity(db: aiosqlite.Connection, project: str = "Delegated") -> int:
    cursor = await db.execute(
        """
        INSERT INTO activity_log (source, timestamp, project_name, summary, tags)
        VALUES ('cc', '2026-08-23', ?, 'delegation fixture', '["SHIPPED"]')
        """,
        (project,),
    )
    assert cursor.lastrowid is not None
    await db.commit()
    return int(cursor.lastrowid)


def _write_manifest(
    path: Path,
    *,
    resource_type: str,
    resource_id: int,
    resource_sha256: str,
    original_owner: str = "cc",
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": DELEGATION_MANIFEST_SCHEMA,
                "delegated_to": "codex",
                "authorization_reason": "Operator approved exact lifecycle takeover",
                "authorization_ref": "codex-task:test",
                "resources": [
                    {
                        "resource_type": resource_type,
                        "resource_id": resource_id,
                        "original_owner": original_owner,
                        "resource_sha256": resource_sha256,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


async def test_delegation_manifest_preserves_original_custody_and_replays(
    db: aiosqlite.Connection,
    tmp_path: Path,
) -> None:
    activity_id = await _activity(db)
    snapshot = await owner_resource_snapshot(
        db, resource_type="activity_disposition", resource_id=activity_id
    )
    path = tmp_path / "delegation.json"
    _write_manifest(
        path,
        resource_type="activity_disposition",
        resource_id=activity_id,
        resource_sha256=snapshot["resource_sha256"],
    )
    manifest = load_owner_delegation_manifest(path)

    first = await apply_owner_delegation_manifest(db, manifest)
    replay = await apply_owner_delegation_manifest(db, manifest)

    assert first["original_custody_preserved"] is True
    assert first["inserted_count"] == 1
    assert first["replayed_count"] == 0
    assert replay["mutation_performed"] is False
    assert replay["replayed_count"] == 1
    row = await (
        await db.execute(
            "SELECT source, sync_disposition FROM activity_log WHERE id = ?",
            (activity_id,),
        )
    ).fetchone()
    assert row is not None
    assert row["source"] == "cc"
    assert row["sync_disposition"] is None


async def test_delegation_manifest_fails_closed_when_resource_changed(
    db: aiosqlite.Connection,
    tmp_path: Path,
) -> None:
    activity_id = await _activity(db)
    snapshot = await owner_resource_snapshot(
        db, resource_type="activity_disposition", resource_id=activity_id
    )
    path = tmp_path / "delegation.json"
    _write_manifest(
        path,
        resource_type="activity_disposition",
        resource_id=activity_id,
        resource_sha256=snapshot["resource_sha256"],
    )
    manifest = load_owner_delegation_manifest(path)
    await db.execute(
        "UPDATE activity_log SET canonical_key = 'changed/key' WHERE id = ?",
        (activity_id,),
    )
    await db.commit()

    with pytest.raises(OwnerDelegationError, match="delegation.resource_changed"):
        await apply_owner_delegation_manifest(db, manifest)

    count = await (await db.execute("SELECT COUNT(*) FROM owner_delegations")).fetchone()
    assert count is not None
    assert count[0] == 0


async def test_delegation_manifest_rejects_resolved_activity(
    db: aiosqlite.Connection,
    tmp_path: Path,
) -> None:
    activity_id = await _activity(db)
    await db.execute(
        "UPDATE activity_log SET sync_disposition = 'no_durable_target', "
        "sync_disposition_by = 'cc' WHERE id = ?",
        (activity_id,),
    )
    await db.commit()
    snapshot = await owner_resource_snapshot(
        db, resource_type="activity_disposition", resource_id=activity_id
    )
    path = tmp_path / "resolved-activity.json"
    _write_manifest(
        path,
        resource_type="activity_disposition",
        resource_id=activity_id,
        resource_sha256=snapshot["resource_sha256"],
    )

    with pytest.raises(
        OwnerDelegationError, match="delegation.resource_not_actionable"
    ):
        await apply_owner_delegation_manifest(
            db, load_owner_delegation_manifest(path)
        )


async def test_delegation_manifest_rejects_acknowledged_refusal(
    db: aiosqlite.Connection,
    tmp_path: Path,
) -> None:
    cursor = await db.execute(
        """
        INSERT INTO snapshot_refusals (
            caller, system, snapshot_family, snapshot_date, reason_code,
            retained_count, retention_limit, payload_sha256,
            acknowledgement_state, acknowledged_by, next_state, acknowledged_at
        ) VALUES (
            'cc', 'codex', 'default', '2026-08-23',
            'snapshot.retention_would_prune', 10, 10, ?,
            'preserve_history', 'cc', 'preserved',
            '2026-08-23T00:00:00Z'
        )
        """,
        ("a" * 64,),
    )
    assert cursor.lastrowid is not None
    refusal_id = int(cursor.lastrowid)
    await db.commit()
    snapshot = await owner_resource_snapshot(
        db, resource_type="snapshot_refusal", resource_id=refusal_id
    )
    path = tmp_path / "acknowledged-refusal.json"
    _write_manifest(
        path,
        resource_type="snapshot_refusal",
        resource_id=refusal_id,
        resource_sha256=snapshot["resource_sha256"],
    )

    with pytest.raises(
        OwnerDelegationError, match="delegation.resource_not_actionable"
    ):
        await apply_owner_delegation_manifest(
            db, load_owner_delegation_manifest(path)
        )


async def test_delegation_consumption_is_append_only_and_one_time(
    db: aiosqlite.Connection,
    tmp_path: Path,
) -> None:
    activity_id = await _activity(db)
    snapshot = await owner_resource_snapshot(
        db, resource_type="activity_disposition", resource_id=activity_id
    )
    path = tmp_path / "delegation.json"
    _write_manifest(
        path,
        resource_type="activity_disposition",
        resource_id=activity_id,
        resource_sha256=snapshot["resource_sha256"],
    )
    receipt = await apply_owner_delegation_manifest(
        db, load_owner_delegation_manifest(path)
    )
    resolved = await resolve_owner_delegation(
        db,
        resource_type="activity_disposition",
        resource_id=activity_id,
        original_owner="cc",
        delegated_to="codex",
    )
    assert resolved is not None
    assert resolved["state"] == "active"
    delegation_id = int(receipt["delegation_ids"][0])
    await consume_owner_delegation(
        db,
        delegation_id=delegation_id,
        actor="codex",
        action="record_disposition:no_durable_target",
        result_sha256="a" * 64,
    )
    await db.commit()

    consumed = await resolve_owner_delegation(
        db,
        resource_type="activity_disposition",
        resource_id=activity_id,
        original_owner="cc",
        delegated_to="codex",
    )
    assert consumed is not None
    assert consumed["state"] == "consumed"
    with pytest.raises(OwnerDelegationError, match="delegation.already_consumed"):
        await consume_owner_delegation(
            db,
            delegation_id=delegation_id,
            actor="codex",
            action="record_disposition:no_durable_target",
            result_sha256="a" * 64,
        )


async def test_delegation_manifest_rejects_symlink(
    db: aiosqlite.Connection,
    tmp_path: Path,
) -> None:
    activity_id = await _activity(db)
    snapshot = await owner_resource_snapshot(
        db, resource_type="activity_disposition", resource_id=activity_id
    )
    real = tmp_path / "real.json"
    linked = tmp_path / "linked.json"
    _write_manifest(
        real,
        resource_type="activity_disposition",
        resource_id=activity_id,
        resource_sha256=snapshot["resource_sha256"],
    )
    linked.symlink_to(real)

    with pytest.raises(
        OwnerDelegationError, match="delegation.manifest_path_invalid"
    ):
        load_owner_delegation_manifest(linked)
