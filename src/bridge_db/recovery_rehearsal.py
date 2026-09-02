"""Bounded, disposable recovery rehearsal for the current BridgeDB anchor."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path
from typing import Any

from bridge_db import config
from bridge_db.db import collect_fts_index_metrics, content_sha256, open_db
from bridge_db.project_resolver import resolve as resolve_project
from bridge_db.recovery import (
    RECOVERY_DATABASE_NAME,
    recovery_anchor_inventory,
    recovery_anchor_path,
    recovery_source_fingerprint,
)
from bridge_db.tools.context import parse_owned_sections
from bridge_db.tools.export import ContextExportSnapshot, build_markdown

RECOVERY_REHEARSAL_SCHEMA = "RecoveryRehearsalEvidenceV1"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _copy_regular_file(source: Path, destination: Path, *, label: str) -> None:
    """Copy one stable regular file without following path replacements."""
    source_before = source.lstat()
    if not stat.S_ISREG(source_before.st_mode) or stat.S_ISLNK(source_before.st_mode):
        raise RuntimeError(f"{label}_not_regular")
    source_flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        source_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        destination_flags |= os.O_CLOEXEC
    source_fd = os.open(source, source_flags)
    destination_fd: int | None = None
    try:
        destination_fd = os.open(destination, destination_flags, 0o600)
        opened_before = os.fstat(source_fd)
        if _stat_signature(opened_before) != _stat_signature(source_before):
            raise RuntimeError(f"{label}_changed_before_copy")
        while chunk := os.read(source_fd, 1024 * 1024):
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError("short recovery rehearsal copy")
                view = view[written:]
        os.fsync(destination_fd)
        opened_after = os.fstat(source_fd)
        path_after = source.lstat()
        if any(
            _stat_signature(observed) != _stat_signature(source_before)
            for observed in (opened_after, path_after)
        ):
            raise RuntimeError(f"{label}_changed_during_copy")
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(source_fd)


async def _grouped_counts(
    db: Any, table: str, columns: tuple[str, ...]
) -> dict[str, int]:
    selected = ", ".join(columns)
    cursor = await db.execute(
        f"SELECT {selected}, COUNT(*) AS row_count "  # noqa: S608
        f"FROM {table} GROUP BY {selected} ORDER BY {selected}"  # noqa: S608
    )
    counts: dict[str, int] = {}
    for row in await cursor.fetchall():
        key = "|".join(str(row[column]) if row[column] is not None else "null" for column in columns)
        counts[key] = int(row["row_count"])
    return counts


async def _ownership_inventory(db: Any) -> dict[str, Any]:
    counts = {
        "context_sections": await _grouped_counts(db, "context_sections", ("owner",)),
        "activity_log": await _grouped_counts(db, "activity_log", ("source",)),
        "system_snapshots": await _grouped_counts(db, "system_snapshots", ("system",)),
        "snapshot_refusals": await _grouped_counts(
            db,
            "snapshot_refusals",
            ("caller", "system", "acknowledged_by"),
        ),
        "owner_delegations": await _grouped_counts(
            db,
            "owner_delegations",
            ("resource_type", "original_owner", "delegated_to"),
        ),
        "owner_delegation_consumptions": await _grouped_counts(
            db,
            "owner_delegation_consumptions",
            ("actor", "action"),
        ),
        "pending_handoffs": await _grouped_counts(
            db,
            "pending_handoffs",
            ("dispatched_from", "claimed_by", "status"),
        ),
        "cost_records": await _grouped_counts(db, "cost_records", ("system",)),
    }
    encoded = json.dumps(counts, sort_keys=True, separators=(",", ":")).encode()
    return {"counts": counts, "sha256": hashlib.sha256(encoded).hexdigest()}


async def _source_mapping_inventory(
    db: Any, *, registry_path: Path, registry_snapshot: Path
) -> dict[str, Any]:
    registry_digest = _sha256_file(registry_snapshot)
    tables: dict[str, dict[str, int]] = {}
    registry_present: bool | None = None
    for table in ("activity_log", "pending_handoffs"):
        cursor = await db.execute(
            f"SELECT project_name, canonical_key FROM {table}"  # noqa: S608
        )
        metrics = {
            "total": 0,
            "stored": 0,
            "resolvable": 0,
            "aligned": 0,
            "expected_mapping_missing": 0,
            "stored_mapping_mismatch": 0,
            "stored_mapping_unresolvable": 0,
            "unmatched_without_stored_mapping": 0,
        }
        for row in await cursor.fetchall():
            metrics["total"] += 1
            stored = row["canonical_key"]
            if stored is not None:
                metrics["stored"] += 1
            resolution = resolve_project(
                str(row["project_name"]), registry_path=registry_snapshot
            )
            if registry_present is None:
                registry_present = resolution.registry_present
            else:
                registry_present = registry_present and resolution.registry_present
            expected = resolution.canonical_key
            if expected is not None:
                metrics["resolvable"] += 1
                if stored is None:
                    metrics["expected_mapping_missing"] += 1
                elif stored == expected:
                    metrics["aligned"] += 1
                else:
                    metrics["stored_mapping_mismatch"] += 1
            elif stored is not None:
                metrics["stored_mapping_unresolvable"] += 1
            else:
                metrics["unmatched_without_stored_mapping"] += 1
        tables[table] = metrics
    drift = sum(
        metrics[category]
        for metrics in tables.values()
        for category in (
            "expected_mapping_missing",
            "stored_mapping_mismatch",
            "stored_mapping_unresolvable",
        )
    )
    return {
        "registry_present": bool(registry_present),
        "registry_path": str(registry_path),
        "registry_sha256": registry_digest,
        "registry_stable": True,
        "registry_read_scope": "stable_private_snapshot",
        "tables": tables,
        "drift_count": drift,
        "ready": bool(registry_present) and drift == 0,
    }


async def _reconstruction_inventory(db: Any, export_path: Path) -> dict[str, Any]:
    context_snapshot: list[ContextExportSnapshot] = []
    content = await build_markdown(db, context_snapshot=context_snapshot)
    export_path.write_text(content, encoding="utf-8", newline="")
    rendered_digest = _sha256_file(export_path)
    state_cursor = await db.execute(
        "SELECT exported_content_sha256 FROM bridge_file_export_state WHERE singleton = 1"
    )
    state = await state_cursor.fetchone()
    expected_digest = state["exported_content_sha256"] if state is not None else None
    parsed_sections = parse_owned_sections(content)
    owned_sections_match = all(
        content_sha256(parsed_sections.get(row.section_name, "")) == row.content_sha256
        for row in context_snapshot
    )
    pending_cursor = await db.execute(
        "SELECT COUNT(*) FROM bridge_projection_jobs WHERE status = 'pending'"
    )
    pending_row = await pending_cursor.fetchone()
    pending_jobs = int(pending_row[0]) if pending_row is not None else 0
    return {
        "path_scope": "disposable_temporary_file",
        "byte_count": len(content.encode("utf-8")),
        "sha256": rendered_digest,
        "database_export_sha256": expected_digest,
        "database_export_matches": expected_digest == content_sha256(content),
        "context_sections_rendered": len(context_snapshot),
        "owned_sections_parseable": owned_sections_match,
        "pending_projection_jobs": pending_jobs,
        "ready": (
            expected_digest == rendered_digest
            and owned_sections_match
            and pending_jobs == 0
        ),
    }


async def rehearse_recovery(
    db_path: Path, *, expected_schema_version: int
) -> dict[str, Any]:
    """Restore and validate the current anchor without mutating live state."""
    anchor = recovery_anchor_path(db_path)
    anchor_state = recovery_anchor_inventory(
        db_path,
        expected_schema_version=expected_schema_version,
    )
    result: dict[str, Any] = {
        "schema": RECOVERY_REHEARSAL_SCHEMA,
        "ready": False,
        "live_mutation_performed": False,
        "backup_selection": {
            "anchor_path": str(anchor),
            "state": anchor_state["state"],
            "ready": anchor_state["ready"],
            "source_current": anchor_state.get("source_current"),
            "sha256": anchor_state.get("sha256"),
        },
        "errors": [],
        "cleanup": "temporary_artifacts_removed",
    }
    if not anchor_state["ready"] or anchor_state.get("source_current") is not True:
        result["errors"] = ["current_verified_anchor_required"]
        return result

    backup = anchor / RECOVERY_DATABASE_NAME
    expected_digest = anchor_state.get("sha256")
    try:
        with tempfile.TemporaryDirectory(prefix="bridge-recovery-rehearsal-") as temp:
            temp_root = Path(temp)
            restored_path = temp_root / "restored.sqlite"
            _copy_regular_file(backup, restored_path, label="anchor_database")
            restored_digest = _sha256_file(restored_path)
            if restored_digest != expected_digest:
                raise RuntimeError("restored_backup_digest_mismatch")

            registry_snapshot = temp_root / "project-registry.json"
            _copy_regular_file(
                config.PROJECT_REGISTRY_PATH,
                registry_snapshot,
                label="project_registry",
            )

            restored = await open_db(restored_path)
            try:
                version_row = await (await restored.execute("PRAGMA user_version")).fetchone()
                integrity_row = await (
                    await restored.execute("PRAGMA integrity_check")
                ).fetchone()
                schema_version = int(version_row[0]) if version_row is not None else -1
                integrity_ok = integrity_row is not None and integrity_row[0] == "ok"
                fts = await collect_fts_index_metrics(restored)
                ownership_before = await _ownership_inventory(restored)
                source_mappings = await _source_mapping_inventory(
                    restored,
                    registry_path=config.PROJECT_REGISTRY_PATH,
                    registry_snapshot=registry_snapshot,
                )
                reconstruction = await _reconstruction_inventory(
                    restored, temp_root / "reconstructed-bridge.md"
                )
            finally:
                await restored.close()

            baseline_fingerprint = recovery_source_fingerprint(restored_path)
            working_path = temp_root / "working.sqlite"
            rollback_path = temp_root / "rollback.sqlite"
            shutil.copyfile(restored_path, working_path)
            shutil.copyfile(restored_path, rollback_path)
            with sqlite3.connect(working_path) as working:
                working.execute("CREATE TABLE rehearsal_only_change (id INTEGER)")
            changed_fingerprint = recovery_source_fingerprint(working_path)
            removed_sidecars: list[str] = []
            for suffix in ("-wal", "-shm"):
                sidecar = working_path.with_name(f"{working_path.name}{suffix}")
                if sidecar.exists():
                    sidecar.unlink()
                    removed_sidecars.append(suffix)
            os.replace(rollback_path, working_path)
            rollback_fingerprint = recovery_source_fingerprint(working_path)
            rolled_back = (
                changed_fingerprint != baseline_fingerprint
                and rollback_fingerprint == baseline_fingerprint
            )

            rollback_db = await open_db(working_path)
            try:
                ownership_after = await _ownership_inventory(rollback_db)
                rollback_fts = await collect_fts_index_metrics(rollback_db)
            finally:
                await rollback_db.close()
            ownership_preserved = ownership_before["sha256"] == ownership_after["sha256"]

            result.update(
                {
                    "restore": {
                        "state": "verified" if integrity_ok else "invalid",
                        "sha256": restored_digest,
                        "schema_version": schema_version,
                        "schema_compatible": schema_version == expected_schema_version,
                        "integrity_ok": integrity_ok,
                    },
                    "fts": fts,
                    "ownership": {
                        **ownership_before,
                        "preserved_after_rollback": ownership_preserved,
                    },
                    "source_mappings": source_mappings,
                    "projection_export_reconstruction": reconstruction,
                    "rollback": {
                        "temporary_change_observed": (
                            changed_fingerprint != baseline_fingerprint
                        ),
                        "temporary_sidecars_removed": removed_sidecars,
                        "baseline_fingerprint_restored": rolled_back,
                        "fts_ready_after_rollback": rollback_fts["ok"],
                        "ownership_preserved": ownership_preserved,
                        "ready": (
                            rolled_back
                            and rollback_fts["ok"]
                            and ownership_preserved
                        ),
                    },
                }
            )
            result["ready"] = bool(
                integrity_ok
                and schema_version == expected_schema_version
                and fts["ok"]
                and source_mappings["ready"]
                and reconstruction["ready"]
                and result["rollback"]["ready"]
            )
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        result["errors"] = [str(exc) or type(exc).__name__]
    return result
