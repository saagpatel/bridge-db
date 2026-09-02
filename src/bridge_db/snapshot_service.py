"""Shared snapshot admission, refusal, and acknowledgement lifecycle.

Every repository-owned snapshot writer goes through this module.  It keeps the
capacity decision and write under the same SQLite writer slot, preserves the
default no-prune policy, and records a durable refusal before returning a
non-success result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal, cast

import aiosqlite

from bridge_db import config
from bridge_db.capacity import encode_bounded_json
from bridge_db.db import (
    fts_text_for_snapshot,
    gc_fts_orphans,
    upsert_fts_entry,
)
from bridge_db.owner_delegation import (
    OwnerDelegationError,
    consume_owner_delegation,
    owner_resource_snapshot,
    resolve_owner_delegation,
)

SnapshotRetentionPolicy = Literal["preserve_existing", "prune_oldest"]
SnapshotRefusalDecision = Literal[
    "preserve_history",
    "retry_after_owner_action",
    "superseded",
]

_DECISION_NEXT_STATE: dict[SnapshotRefusalDecision, str] = {
    "preserve_history": "capacity_blocked_owner_decision_required",
    "retry_after_owner_action": "retry_after_owner_capacity_change",
    "superseded": "no_retry",
}


@dataclass(frozen=True)
class SnapshotCapacity:
    system: str
    snapshot_family: str
    retained_count: int
    retention_limit: int

    @property
    def available_slots(self) -> int:
        return max(self.retention_limit - self.retained_count, 0)

    @property
    def state(self) -> str:
        return "available" if self.available_slots else "full"

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "available_slots": self.available_slots,
            "state": self.state,
            "next_state": (
                "write_allowed"
                if self.available_slots
                else "capacity_blocked_owner_decision_required"
            ),
        }


def snapshot_family(system: str, data: dict[str, Any]) -> str:
    if system != "codex":
        return "default"
    if {"infrastructure", "automation_digest", "active_projects"}.issubset(data):
        return "operating"
    if "consulted_node" in data:
        return "consulted_node"
    return "other"


def decode_snapshot_data(raw_data: str) -> dict[str, Any]:
    try:
        parsed_data = cast(object, json.loads(raw_data))
    except json.JSONDecodeError:
        parsed_data = {}
    if not isinstance(parsed_data, dict):
        return {}
    return {
        str(key): value for key, value in cast(dict[object, Any], parsed_data).items()
    }


async def snapshot_capacity(
    db: aiosqlite.Connection,
    *,
    system: str,
    family: str,
) -> SnapshotCapacity:
    cursor = await db.execute(
        "SELECT data FROM system_snapshots WHERE system = ?",
        (system,),
    )
    rows = await cursor.fetchall()
    retained_count = sum(
        1
        for row in rows
        if snapshot_family(system, decode_snapshot_data(row["data"])) == family
    )
    return SnapshotCapacity(
        system=system,
        snapshot_family=family,
        retained_count=retained_count,
        retention_limit=config.SNAPSHOT_RETENTION_PER_SYSTEM,
    )


async def _prune_snapshots(
    db: aiosqlite.Connection, *, system: str
) -> list[tuple[int, str]]:
    cursor = await db.execute(
        """
        SELECT id, data
        FROM system_snapshots
        WHERE system = ?
        ORDER BY created_at DESC, id DESC
        """,
        (system,),
    )
    rows = await cursor.fetchall()

    seen_by_family: dict[str, int] = {}
    pruned: list[tuple[int, str]] = []
    for row in rows:
        family = snapshot_family(system, decode_snapshot_data(row["data"]))
        seen_by_family[family] = seen_by_family.get(family, 0) + 1
        if seen_by_family[family] > config.SNAPSHOT_RETENTION_PER_SYSTEM:
            pruned.append((int(row["id"]), family))

    if pruned:
        placeholders = ",".join("?" for _ in pruned)
        await db.execute(
            f"DELETE FROM system_snapshots WHERE id IN ({placeholders})",  # noqa: S608 -- placeholders are generated from exact row count
            [row_id for row_id, _ in pruned],
        )
    return pruned


async def save_snapshot_record(
    db: aiosqlite.Connection,
    *,
    caller: str,
    system: str,
    data: dict[str, Any],
    snapshot_date: str,
    source_trust: str = "agent",
    retention_policy: SnapshotRetentionPolicy = "preserve_existing",
    initial_seed: bool = False,
) -> dict[str, Any]:
    """Admit one snapshot or durably refuse it under a serialized capacity check.

    ``initial_seed`` is the narrow migration exemption.  It is accepted only
    when the entire target system has no rows; it never prunes and cannot be
    used as a general writer bypass.
    """
    if retention_policy not in ("preserve_existing", "prune_oldest"):
        raise ValueError("snapshot.invalid_retention_policy")

    snapshot_json = encode_bounded_json(
        data,
        maximum_bytes=config.SNAPSHOT_JSON_MAX_BYTES,
        maximum_depth=config.SNAPSHOT_JSON_MAX_DEPTH,
        maximum_nodes=config.SNAPSHOT_JSON_MAX_NODES,
        code_prefix="snapshot",
    )
    family = snapshot_family(system, data)
    owns_transaction = not db.in_transaction
    if owns_transaction:
        await db.execute("BEGIN IMMEDIATE")

    try:
        capacity = await snapshot_capacity(db, system=system, family=family)
        if initial_seed:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM system_snapshots WHERE system = ?", (system,)
            )
            row = await cursor.fetchone()
            system_count = int(row[0]) if row is not None else 0
            if system_count:
                if owns_transaction:
                    await db.rollback()
                return {
                    "ok": True,
                    "mutation_performed": False,
                    "snapshot_id": None,
                    "system": system,
                    "snapshot_family": family,
                    "snapshot_date": snapshot_date,
                    "source_trust": source_trust,
                    "retention_policy": "initial_seed_exemption",
                    "initial_seed_exemption": True,
                    "write_state": "skipped_existing_system",
                    "capacity": capacity.to_dict(),
                    "pruned_count": 0,
                }

        preserve_existing = retention_policy == "preserve_existing" or initial_seed
        if preserve_existing and capacity.available_slots == 0:
            payload_sha256 = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
            cursor = await db.execute(
                """
                INSERT INTO snapshot_refusals (
                    caller, system, snapshot_family, snapshot_date, reason_code,
                    retained_count, retention_limit, payload_sha256, next_state
                ) VALUES (?, ?, ?, ?, 'snapshot.retention_would_prune', ?, ?, ?, ?)
                """,
                (
                    caller,
                    system,
                    family,
                    snapshot_date,
                    capacity.retained_count,
                    capacity.retention_limit,
                    payload_sha256,
                    "capacity_blocked_acknowledgement_required",
                ),
            )
            refusal_id = cursor.lastrowid
            if owns_transaction:
                await db.commit()
            return {
                "ok": False,
                "reason_code": "snapshot.retention_would_prune",
                "mutation_performed": False,
                "evidence_mutation_performed": True,
                "snapshot_id": None,
                "refusal_id": refusal_id,
                "acknowledgement_required": True,
                "next_state": "capacity_blocked_acknowledgement_required",
                "system": system,
                "snapshot_family": family,
                "snapshot_date": snapshot_date,
                "source_trust": source_trust,
                "retention_policy": retention_policy,
                "retention_limit": capacity.retention_limit,
                "retained_count": capacity.retained_count,
                "available_slots": capacity.available_slots,
                "would_prune_count": capacity.retained_count
                + 1
                - capacity.retention_limit,
                "pruned_count": 0,
            }

        cursor = await db.execute(
            """
            INSERT INTO system_snapshots (system, snapshot_date, data, source_trust)
            VALUES (?, ?, ?, ?)
            """,
            (system, snapshot_date, snapshot_json, source_trust),
        )
        snapshot_id = cursor.lastrowid
        if snapshot_id is not None:
            await upsert_fts_entry(
                db,
                "snapshot",
                str(snapshot_id),
                fts_text_for_snapshot(snapshot_json),
            )

        if preserve_existing:
            pruned: list[tuple[int, str]] = []
        else:
            pruned = await _prune_snapshots(db, system=system)
            await gc_fts_orphans(db, "snapshot")
        if owns_transaction:
            await db.commit()
    except Exception:
        if owns_transaction:
            await db.rollback()
        raise

    return {
        "ok": True,
        "mutation_performed": True,
        "snapshot_id": snapshot_id,
        "system": system,
        "snapshot_family": family,
        "snapshot_date": snapshot_date,
        "source_trust": source_trust,
        "retention_policy": (
            "initial_seed_exemption" if initial_seed else retention_policy
        ),
        "initial_seed_exemption": initial_seed,
        "write_state": "inserted",
        "retention_limit": capacity.retention_limit,
        "retained_count_before": capacity.retained_count,
        "available_slots_before": capacity.available_slots,
        "pruned_count": len(pruned),
        "pruned_ids": [row_id for row_id, _ in pruned],
        "pruned_families": sorted({family_name for _, family_name in pruned}),
    }


async def acknowledge_snapshot_refusal_record(
    db: aiosqlite.Connection,
    *,
    caller: str,
    refusal_id: int,
    decision: SnapshotRefusalDecision,
) -> dict[str, Any]:
    """Acknowledge one exact owner refusal without granting deletion authority."""
    if decision not in _DECISION_NEXT_STATE:
        raise ValueError("snapshot.invalid_refusal_decision")
    await db.execute("BEGIN IMMEDIATE")
    try:
        cursor = await db.execute(
            "SELECT * FROM snapshot_refusals WHERE id = ?", (refusal_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            await db.rollback()
            return {
                "ok": False,
                "reason_code": "snapshot.refusal_not_found",
                "refusal_id": refusal_id,
                "mutation_performed": False,
            }
        delegation: dict[str, Any] | None = None
        if row["caller"] != caller:
            try:
                delegation = await resolve_owner_delegation(
                    db,
                    resource_type="snapshot_refusal",
                    resource_id=refusal_id,
                    original_owner=str(row["caller"]),
                    delegated_to=caller,
                )
            except OwnerDelegationError as exc:
                await db.rollback()
                return {
                    "ok": False,
                    "reason_code": exc.reason_code,
                    "refusal_id": refusal_id,
                    "mutation_performed": False,
                }
            if delegation is None:
                await db.rollback()
                return {
                    "ok": False,
                    "reason_code": "snapshot.refusal_owner_mismatch",
                    "refusal_id": refusal_id,
                    "mutation_performed": False,
                }
            if delegation["state"] != "active":
                await db.rollback()
                return {
                    "ok": False,
                    "reason_code": "delegation.already_consumed",
                    "refusal_id": refusal_id,
                    "mutation_performed": False,
                }

        existing = row["acknowledgement_state"]
        next_state = _DECISION_NEXT_STATE[decision]
        if existing is not None:
            await db.rollback()
            return {
                "ok": existing == decision,
                "reason_code": (
                    "snapshot.refusal_acknowledgement_replayed"
                    if existing == decision
                    else "snapshot.refusal_already_acknowledged"
                ),
                "refusal_id": refusal_id,
                "acknowledgement_state": existing,
                "next_state": row["next_state"],
                "mutation_performed": False,
                "deletion_authorized": False,
            }

        await db.execute(
            """
            UPDATE snapshot_refusals
            SET acknowledgement_state = ?, acknowledged_by = ?, next_state = ?,
                acknowledged_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE id = ? AND acknowledgement_state IS NULL
            """,
            (decision, caller, next_state, refusal_id),
        )
        if delegation is not None:
            result_snapshot = await owner_resource_snapshot(
                db,
                resource_type="snapshot_refusal",
                resource_id=refusal_id,
            )
            try:
                await consume_owner_delegation(
                    db,
                    delegation_id=int(delegation["delegation_id"]),
                    actor=caller,
                    action=f"acknowledge_snapshot_refusal:{decision}",
                    result_sha256=str(result_snapshot["resource_sha256"]),
                )
            except OwnerDelegationError as exc:
                await db.rollback()
                return {
                    "ok": False,
                    "reason_code": exc.reason_code,
                    "refusal_id": refusal_id,
                    "mutation_performed": False,
                }
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return {
        "ok": True,
        "refusal_id": refusal_id,
        "acknowledgement_state": decision,
        "next_state": next_state,
        "mutation_performed": True,
        "deletion_authorized": False,
        **(
            {
                "delegation_id": int(delegation["delegation_id"]),
                "original_owner": row["caller"],
            }
            if delegation is not None
            else {}
        ),
    }
