"""Exact-resource, append-only owner delegation for exceptional lifecycle work.

Delegation never changes the owner stored on the source row. An operator grants
one named principal authority over one exact resource image, and the successful
write appends a separate one-time consumption receipt in the same transaction.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Literal, cast

import aiosqlite

from bridge_db.models import CALLER_IDS

DELEGATION_MANIFEST_SCHEMA = "BridgeOwnerDelegationManifestV1"
DELEGATION_RECEIPT_SCHEMA = "BridgeOwnerDelegationReceiptV1"
ResourceType = Literal["activity_disposition", "snapshot_refusal"]
RESOURCE_TYPES: frozenset[str] = frozenset(
    {"activity_disposition", "snapshot_refusal"}
)
_MAX_MANIFEST_BYTES = 1024 * 1024


class OwnerDelegationError(RuntimeError):
    """Fail-closed delegation error with a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _assert_resource_actionable(image: dict[str, Any]) -> None:
    """Refuse grants for resources whose owner obligation is already closed."""
    resource_type = image["resource_type"]
    if resource_type == "activity_disposition":
        tags = cast(list[str], image["tags"])
        if image["sync_disposition"] is not None or "PROCESSED" in tags:
            raise OwnerDelegationError("delegation.resource_not_actionable")
        return
    if resource_type == "snapshot_refusal":
        if image["acknowledgement_state"] is not None:
            raise OwnerDelegationError("delegation.resource_not_actionable")
        return
    raise OwnerDelegationError("delegation.resource_type_invalid")


async def owner_resource_snapshot(
    db: aiosqlite.Connection,
    *,
    resource_type: ResourceType,
    resource_id: int,
) -> dict[str, Any]:
    """Return a metadata-only image and digest for one delegable resource."""
    image: dict[str, Any]
    if resource_type == "activity_disposition":
        cursor = await db.execute(
            """
            SELECT id, source, created_at, project_name, summary, tags,
                   canonical_key, sync_disposition, sync_disposition_by,
                   synced_at, sync_downstream_system, sync_downstream_ref,
                   sync_policy_ref, sync_reason, sync_note
            FROM activity_log WHERE id = ?
            """,
            (resource_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise OwnerDelegationError("delegation.resource_not_found")
        try:
            parsed_tags = cast(object, json.loads(row["tags"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise OwnerDelegationError("delegation.resource_invalid") from exc
        if not isinstance(parsed_tags, list):
            raise OwnerDelegationError("delegation.resource_invalid")
        parsed_tag_list = cast(list[object], parsed_tags)
        if not all(isinstance(tag, str) for tag in parsed_tag_list):
            raise OwnerDelegationError("delegation.resource_invalid")
        tags = cast(list[str], parsed_tag_list)
        # Same case rule as record_disposition: protected tags are
        # uppercased on write, but rows that predate that still count.
        if "SHIPPED" not in {tag.upper() for tag in tags}:
            raise OwnerDelegationError("delegation.resource_not_shipped")
        image = {
            "resource_type": resource_type,
            "resource_id": int(row["id"]),
            "original_owner": str(row["source"]),
            "created_at": row["created_at"],
            "project_name": row["project_name"],
            "summary_sha256": hashlib.sha256(
                str(row["summary"]).encode("utf-8")
            ).hexdigest(),
            "tags": tags,
            "canonical_key": row["canonical_key"],
            "sync_disposition": row["sync_disposition"],
            "sync_disposition_by": row["sync_disposition_by"],
            "synced_at": row["synced_at"],
            "sync_downstream_system": row["sync_downstream_system"],
            "sync_downstream_ref": row["sync_downstream_ref"],
            "sync_policy_ref": row["sync_policy_ref"],
            "sync_reason": row["sync_reason"],
            "sync_note": row["sync_note"],
        }
    elif resource_type == "snapshot_refusal":
        cursor = await db.execute(
            """
            SELECT id, caller, system, snapshot_family, snapshot_date,
                   reason_code, retained_count, retention_limit, payload_sha256,
                   acknowledgement_state, acknowledged_by, next_state,
                   created_at, acknowledged_at
            FROM snapshot_refusals WHERE id = ?
            """,
            (resource_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise OwnerDelegationError("delegation.resource_not_found")
        image = {
            "resource_type": resource_type,
            "resource_id": int(row["id"]),
            "original_owner": str(row["caller"]),
            "system": row["system"],
            "snapshot_family": row["snapshot_family"],
            "snapshot_date": row["snapshot_date"],
            "reason_code": row["reason_code"],
            "retained_count": int(row["retained_count"]),
            "retention_limit": int(row["retention_limit"]),
            "payload_sha256": row["payload_sha256"],
            "acknowledgement_state": row["acknowledgement_state"],
            "acknowledged_by": row["acknowledged_by"],
            "next_state": row["next_state"],
            "created_at": row["created_at"],
            "acknowledged_at": row["acknowledged_at"],
        }
    else:  # pragma: no cover - closed type plus runtime guard
        raise OwnerDelegationError("delegation.resource_type_invalid")
    return {"image": image, "resource_sha256": _sha256_json(image)}


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink():
        raise OwnerDelegationError("delegation.manifest_path_invalid")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise OwnerDelegationError("delegation.manifest_unreadable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_size > _MAX_MANIFEST_BYTES
    ):
        raise OwnerDelegationError("delegation.manifest_file_invalid")
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OwnerDelegationError("delegation.manifest_invalid") from exc
    if not isinstance(raw, dict):
        raise OwnerDelegationError("delegation.manifest_invalid")
    return {str(key): value for key, value in cast(dict[object, Any], raw).items()}


def load_owner_delegation_manifest(path: Path) -> dict[str, Any]:
    """Load and strictly validate one no-secret delegation manifest."""
    manifest = _read_manifest(path)
    expected_keys = {
        "schema",
        "delegated_to",
        "authorization_reason",
        "authorization_ref",
        "resources",
    }
    if set(manifest) != expected_keys or manifest.get("schema") != DELEGATION_MANIFEST_SCHEMA:
        raise OwnerDelegationError("delegation.manifest_shape_invalid")
    delegated_to = manifest.get("delegated_to")
    if delegated_to not in CALLER_IDS:
        raise OwnerDelegationError("delegation.delegate_invalid")
    reason = manifest.get("authorization_reason")
    reference = manifest.get("authorization_ref")
    if not isinstance(reason, str) or not reason.strip():
        raise OwnerDelegationError("delegation.authorization_reason_invalid")
    if not isinstance(reference, str) or not reference.strip():
        raise OwnerDelegationError("delegation.authorization_ref_invalid")
    raw_resources = manifest.get("resources")
    if not isinstance(raw_resources, list):
        raise OwnerDelegationError("delegation.resources_invalid")
    resources = cast(list[object], raw_resources)
    if not 1 <= len(resources) <= 1000:
        raise OwnerDelegationError("delegation.resources_invalid")
    normalized: list[dict[str, Any]] = []
    identities: set[tuple[str, int]] = set()
    for item in resources:
        if not isinstance(item, dict):
            raise OwnerDelegationError("delegation.resource_shape_invalid")
        resource = {
            str(key): value
            for key, value in cast(dict[object, object], item).items()
        }
        if set(resource) != {
            "resource_type",
            "resource_id",
            "original_owner",
            "resource_sha256",
        }:
            raise OwnerDelegationError("delegation.resource_shape_invalid")
        resource_type = resource.get("resource_type")
        resource_id = resource.get("resource_id")
        original_owner = resource.get("original_owner")
        resource_sha256 = resource.get("resource_sha256")
        if resource_type not in RESOURCE_TYPES:
            raise OwnerDelegationError("delegation.resource_type_invalid")
        if not isinstance(resource_id, int) or isinstance(resource_id, bool) or resource_id < 1:
            raise OwnerDelegationError("delegation.resource_id_invalid")
        if original_owner not in CALLER_IDS or original_owner == delegated_to:
            raise OwnerDelegationError("delegation.original_owner_invalid")
        if not _is_sha256(resource_sha256):
            raise OwnerDelegationError("delegation.resource_sha256_invalid")
        identity = (str(resource_type), resource_id)
        if identity in identities:
            raise OwnerDelegationError("delegation.resource_duplicate")
        identities.add(identity)
        normalized.append(
            {
                "resource_type": resource_type,
                "resource_id": resource_id,
                "original_owner": original_owner,
                "resource_sha256": resource_sha256,
            }
        )
    normalized.sort(key=lambda item: (item["resource_type"], item["resource_id"]))
    result = {
        "schema": DELEGATION_MANIFEST_SCHEMA,
        "delegated_to": delegated_to,
        "authorization_reason": reason.strip(),
        "authorization_ref": reference.strip(),
        "resources": normalized,
    }
    return {**result, "manifest_sha256": _sha256_json(result)}


async def apply_owner_delegation_manifest(
    db: aiosqlite.Connection, manifest: dict[str, Any]
) -> dict[str, Any]:
    """Atomically grant every exact resource in a validated manifest."""
    delegated_to = str(manifest["delegated_to"])
    reason = str(manifest["authorization_reason"])
    reference = str(manifest["authorization_ref"])
    resources = cast(list[dict[str, Any]], manifest["resources"])
    inserted: list[int] = []
    replayed: list[int] = []
    await db.execute("BEGIN IMMEDIATE")
    try:
        for item in resources:
            resource_type = cast(ResourceType, item["resource_type"])
            resource_id = int(item["resource_id"])
            observed = await owner_resource_snapshot(
                db, resource_type=resource_type, resource_id=resource_id
            )
            if (
                observed["image"]["original_owner"] != item["original_owner"]
                or observed["resource_sha256"] != item["resource_sha256"]
            ):
                raise OwnerDelegationError("delegation.resource_changed")
            _assert_resource_actionable(cast(dict[str, Any], observed["image"]))
            existing = await (
                await db.execute(
                    "SELECT * FROM owner_delegations "
                    "WHERE resource_type = ? AND resource_id = ?",
                    (resource_type, resource_id),
                )
            ).fetchone()
            expected = {
                "original_owner": item["original_owner"],
                "delegated_to": delegated_to,
                "resource_sha256": item["resource_sha256"],
                "authorization_reason": reason,
                "authorization_ref": reference,
                "delegated_by": "operator-cli",
            }
            if existing is not None:
                if any(existing[key] != value for key, value in expected.items()):
                    raise OwnerDelegationError("delegation.resource_already_delegated")
                replayed.append(int(existing["id"]))
                continue
            cursor = await db.execute(
                """
                INSERT INTO owner_delegations (
                    resource_type, resource_id, original_owner, delegated_to,
                    resource_sha256, authorization_reason, authorization_ref,
                    delegated_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'operator-cli')
                """,
                (
                    resource_type,
                    resource_id,
                    item["original_owner"],
                    delegated_to,
                    item["resource_sha256"],
                    reason,
                    reference,
                ),
            )
            if cursor.lastrowid is None:
                raise OwnerDelegationError("delegation.insert_failed")
            inserted.append(int(cursor.lastrowid))
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return {
        "schema": DELEGATION_RECEIPT_SCHEMA,
        "ok": True,
        "manifest_sha256": manifest["manifest_sha256"],
        "delegated_to": delegated_to,
        "resource_count": len(resources),
        "inserted_count": len(inserted),
        "replayed_count": len(replayed),
        "delegation_ids": [*inserted, *replayed],
        "original_custody_preserved": True,
        "mutation_performed": bool(inserted),
    }


async def resolve_owner_delegation(
    db: aiosqlite.Connection,
    *,
    resource_type: ResourceType,
    resource_id: int,
    original_owner: str,
    delegated_to: str,
) -> dict[str, Any] | None:
    """Resolve an exact current grant and its one-time consumption state."""
    observed = await owner_resource_snapshot(
        db, resource_type=resource_type, resource_id=resource_id
    )
    row = await (
        await db.execute(
            """
            SELECT delegation.*, consumption.actor AS consumed_by,
                   consumption.action AS consumed_action,
                   consumption.result_sha256, consumption.consumed_at
            FROM owner_delegations AS delegation
            LEFT JOIN owner_delegation_consumptions AS consumption
              ON consumption.delegation_id = delegation.id
            WHERE delegation.resource_type = ? AND delegation.resource_id = ?
            """,
            (resource_type, resource_id),
        )
    ).fetchone()
    if row is None:
        return None
    if (
        row["original_owner"] != original_owner
        or observed["image"]["original_owner"] != original_owner
        or row["delegated_to"] != delegated_to
    ):
        raise OwnerDelegationError("delegation.resource_changed")
    # An active grant must still see the exact image it was issued against.
    # A consumed grant has normally changed that image by design (the
    # delegated write is part of the resource), so it may also match the
    # result image its consumption receipt recorded. That keeps an exact
    # retry after a lost response resolvable, while any third state of the
    # resource still fails closed.
    accepted = {row["resource_sha256"]}
    if row["consumed_at"] is not None:
        accepted.add(row["result_sha256"])
    if observed["resource_sha256"] not in accepted:
        raise OwnerDelegationError("delegation.resource_changed")
    return {
        "delegation_id": int(row["id"]),
        "state": "consumed" if row["consumed_at"] is not None else "active",
        "resource_sha256": row["resource_sha256"],
        "authorization_ref": row["authorization_ref"],
        "consumed_by": row["consumed_by"],
        "consumed_action": row["consumed_action"],
        "result_sha256": row["result_sha256"],
        "consumed_at": row["consumed_at"],
    }


async def consume_owner_delegation(
    db: aiosqlite.Connection,
    *,
    delegation_id: int,
    actor: str,
    action: str,
    result_sha256: str,
) -> None:
    """Append the single consumption receipt inside the resource transaction."""
    if actor not in CALLER_IDS or not action.strip() or not _is_sha256(result_sha256):
        raise OwnerDelegationError("delegation.consumption_invalid")
    try:
        await db.execute(
            """
            INSERT INTO owner_delegation_consumptions (
                delegation_id, actor, action, result_sha256
            ) VALUES (?, ?, ?, ?)
            """,
            (delegation_id, actor, action.strip(), result_sha256),
        )
    except aiosqlite.IntegrityError as exc:
        raise OwnerDelegationError("delegation.already_consumed") from exc
