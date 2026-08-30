"""Non-destructive lifecycle helpers for local durable-evidence files."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast


@dataclass(frozen=True)
class AppendResult:
    """Receipt for one durable JSONL append."""

    path: Path
    bytes_written: int
    rotated_path: Path | None


def segment_paths(path: Path) -> list[Path]:
    """Return preserved rotation segments, newest first."""
    return sorted(
        path.parent.glob(f"{path.name}.*.segment"),
        key=lambda candidate: candidate.name,
        reverse=True,
    )


def append_jsonl_durable(
    path: Path,
    event: dict[str, Any],
    *,
    rotate_bytes: int,
) -> AppendResult:
    """Append one fsync'd JSONL record with lossless, serialized rotation.

    Rotation renames the complete active file to a unique immutable segment.
    No segment is deleted or overwritten. The lock covers size inspection,
    rename, append, and fsync so cooperating bridge-db processes cannot race
    the boundary.
    """
    if rotate_bytes <= 0:
        raise ValueError("rotate_bytes must be positive")
    encoded = (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    rotated_path: Path | None = None

    lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        active_size = path.stat().st_size if path.exists() else 0
        if active_size and active_size + len(encoded) > rotate_bytes:
            stem = f"{path.name}.{time.time_ns():020d}.{os.getpid()}"
            collision = 0
            rotated_path = path.with_name(f"{stem}.{collision:06d}.segment")
            while rotated_path.exists():
                collision += 1
                rotated_path = path.with_name(f"{stem}.{collision:06d}.segment")
            os.replace(path, rotated_path)

        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        fd = os.open(path, flags, 0o600)
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short JSONL append")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    return AppendResult(
        path=path,
        bytes_written=len(encoded),
        rotated_path=rotated_path,
    )


def jsonl_family_paths(path: Path) -> list[Path]:
    """Return active JSONL followed by preserved segments, newest first."""
    active = [path] if path.exists() else []
    return [*active, *segment_paths(path)]


def jsonl_family_size(path: Path) -> int:
    """Return active plus preserved-segment bytes."""
    total = 0
    for candidate in jsonl_family_paths(path):
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


def evidence_file_inventory(path: Path, *, rotate_bytes: int) -> dict[str, Any]:
    """Return non-sensitive lifecycle state for one JSONL family."""
    segments = segment_paths(path)
    try:
        active_bytes = path.stat().st_size
    except OSError:
        active_bytes = 0
    segment_bytes = 0
    for segment in segments:
        try:
            segment_bytes += segment.stat().st_size
        except OSError:
            continue
    return {
        "path": str(path),
        "active_bytes": active_bytes,
        "segment_count": len(segments),
        "segment_bytes": segment_bytes,
        "total_bytes": active_bytes + segment_bytes,
        "rotation_bytes": rotate_bytes,
        "rotation_policy": "lossless_size_boundary",
        "retention_policy": "preserve_all_pending_approval",
        "destructive_cleanup": "approval_required",
    }


def legacy_raw_query_inventory(
    path: Path, *, scan_bytes: int = 1024 * 1024
) -> dict[str, Any]:
    """Count legacy raw-query records without returning their contents."""
    family_bytes = jsonl_family_size(path)
    raw_query_records = 0
    scanned_records = 0
    for record in iter_jsonl_family_reverse(path, max_bytes=scan_bytes):
        scanned_records += 1
        if isinstance(record.get("query"), str):
            raw_query_records += 1
    return {
        "raw_query_records": raw_query_records,
        "scanned_records": scanned_records,
        "scan_bytes": scan_bytes,
        "scan_truncated": family_bytes > scan_bytes,
        "external_copies": "unknown",
        "cleanup": (
            "approval_required" if raw_query_records else "none_in_scanned_horizon"
        ),
    }


def evidence_disposition_inventory(
    path: Path, *, scan_bytes: int = 1024 * 1024
) -> dict[str, Any]:
    """Summarize latest disposition transaction states without payload content."""
    family_bytes = jsonl_family_size(path)
    latest_states: dict[str, str] = {}
    scanned_records = 0
    invalid_records = 0
    for record in iter_jsonl_family_reverse(path, max_bytes=scan_bytes):
        scanned_records += 1
        transaction_id = record.get("transaction_id")
        status = record.get("status")
        if not isinstance(transaction_id, str) or status not in {
            "prepared",
            "completed",
            "aborted",
        }:
            invalid_records += 1
            continue
        latest_states.setdefault(transaction_id, status)
    open_count = sum(status == "prepared" for status in latest_states.values())
    scan_truncated = family_bytes > scan_bytes
    return {
        "transaction_count": len(latest_states),
        "open_count": open_count,
        "completed_count": sum(
            status == "completed" for status in latest_states.values()
        ),
        "aborted_count": sum(status == "aborted" for status in latest_states.values()),
        "invalid_records": invalid_records,
        "scanned_records": scanned_records,
        "scan_bytes": scan_bytes,
        "scan_truncated": scan_truncated,
        "state": (
            "degraded" if open_count or invalid_records or scan_truncated else "clear"
        ),
        "destructive_cleanup": "approval_required",
    }


def sha256_file(path: Path) -> str:
    """Hash a file without materializing it."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def migration_backup_inventory(db_path: Path) -> dict[str, Any]:
    """Inventory sibling migration backups without inventing provenance."""
    backups: list[dict[str, Any]] = []
    for backup in sorted(db_path.parent.glob(f"{db_path.name}.*.bak")):
        manifest = Path(f"{backup}.sha256")
        metadata = Path(f"{backup}.meta.json")
        errors: list[str] = []
        schema_version: int | None = None
        integrity_ok = False
        digest_ok = False
        metadata_ok = False
        try:
            expected = manifest.read_text(encoding="utf-8").strip()
            digest_ok = bool(expected) and expected == sha256_file(backup)
        except OSError:
            errors.append("manifest_unreadable")
        if not digest_ok:
            errors.append("digest_unverified")
        try:
            with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as check:
                integrity = check.execute("PRAGMA integrity_check").fetchone()
                integrity_ok = integrity is not None and integrity[0] == "ok"
                version_row = check.execute("PRAGMA user_version").fetchone()
                schema_version = int(version_row[0]) if version_row else None
        except (OSError, sqlite3.Error, TypeError, ValueError):
            errors.append("sqlite_unreadable")
        if not integrity_ok:
            errors.append("integrity_unverified")
        try:
            loaded_metadata = cast(
                object,
                json.loads(metadata.read_text(encoding="utf-8")),
            )
            if isinstance(loaded_metadata, dict):
                metadata_record = cast(dict[object, object], loaded_metadata)
                created_at = metadata_record.get("created_at")
                created_at_ok = False
                if isinstance(created_at, str):
                    try:
                        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        created_at_ok = True
                    except ValueError:
                        pass
                metadata_ok = (
                    metadata_record.get("schema") == "MigrationBackupEvidenceV1"
                    and created_at_ok
                    and metadata_record.get("source_schema_version") == schema_version
                    and metadata_record.get("backup_bytes") == backup.stat().st_size
                    and metadata_record.get("sha256") == sha256_file(backup)
                    and metadata_record.get("recovery_readback") == "verified"
                )
        except (OSError, json.JSONDecodeError, UnicodeError):
            errors.append("metadata_unreadable")
        if not metadata_ok:
            errors.append("metadata_unverified")
        creation_time_verified = digest_ok and integrity_ok and metadata_ok
        backups.append(
            {
                "path": str(backup),
                "bytes": backup.stat().st_size,
                "manifest_path": str(manifest),
                "metadata_path": str(metadata),
                "metadata_exists": metadata.exists(),
                "metadata_ok": metadata_ok,
                "digest_ok": digest_ok,
                "integrity_ok": integrity_ok,
                "schema_version": schema_version,
                "recovery_readback": "verified" if not errors else "unverified",
                "sqlite_readable": integrity_ok,
                "provenance": (
                    "creation_time_verified"
                    if creation_time_verified
                    else "historical_unverified"
                ),
                "retention_policy": "operator_acknowledgement_required",
                "cleanup": "approval_required",
                "errors": sorted(set(errors)),
            }
        )
    readable_count = sum(item["sqlite_readable"] for item in backups)
    provenance_unverified_count = sum(
        item["provenance"] == "historical_unverified" for item in backups
    )
    companion_paths = sorted(
        {
            *db_path.parent.glob(f"{db_path.name}.*.bak-wal"),
            *db_path.parent.glob(f"{db_path.name}.*.bak-shm"),
        }
    )
    companions: list[dict[str, Any]] = []
    for companion in companion_paths:
        primary = Path(str(companion).removesuffix("-wal").removesuffix("-shm"))
        item: dict[str, Any] = {
            "path": str(companion),
            "bytes": None,
            "kind": "wal" if companion.name.endswith("-wal") else "shm",
            "primary_path": str(primary),
            "primary_exists": None,
            "state": "unverified",
            "retention_policy": "operator_acknowledgement_required",
            "cleanup": "approval_required",
        }
        try:
            companion_metadata = companion.lstat()
        except FileNotFoundError:
            item["errors"] = ["companion_disappeared_during_inventory"]
        except OSError:
            item["errors"] = ["companion_unreadable"]
        else:
            if stat.S_ISLNK(companion_metadata.st_mode):
                item["errors"] = ["companion_symlink"]
            elif not stat.S_ISREG(companion_metadata.st_mode):
                item["errors"] = ["companion_not_regular"]
            else:
                item["bytes"] = companion_metadata.st_size
                try:
                    primary_metadata = primary.lstat()
                except FileNotFoundError:
                    primary_exists: bool | None = False
                except OSError:
                    primary_exists = None
                    item["errors"] = ["companion_primary_unreadable"]
                else:
                    primary_exists = stat.S_ISREG(primary_metadata.st_mode)
                item["primary_exists"] = primary_exists
                if primary_exists is not None:
                    item["state"] = (
                        "attached_to_live_primary"
                        if primary_exists
                        else "retained_without_live_primary"
                    )
        companions.append(item)
    orphaned_companion_count = sum(
        item["state"] == "retained_without_live_primary" for item in companions
    )
    missing_primary_paths = sorted(
        {
            item["primary_path"]
            for item in companions
            if item["state"] == "retained_without_live_primary"
        }
    )
    return {
        "count": len(backups),
        "verified_count": sum(
            item["recovery_readback"] == "verified" for item in backups
        ),
        "readable_count": readable_count,
        "provenance_unverified_count": provenance_unverified_count,
        "provenance_state": (
            "none"
            if not backups
            else "verified"
            if not provenance_unverified_count
            else "readable_but_unknown"
            if readable_count == len(backups)
            else "mixed_or_unreadable"
        ),
        "companion_count": len(companions),
        "orphaned_companion_count": orphaned_companion_count,
        "missing_primary_count": len(missing_primary_paths),
        "missing_primary_paths": missing_primary_paths,
        "companion_state": (
            "none"
            if not companions
            else "unverified"
            if any(item["state"] == "unverified" for item in companions)
            else "attached"
            if not orphaned_companion_count
            else "retained_without_live_primary"
            if orphaned_companion_count == len(companions)
            else "mixed"
        ),
        "cleanup": "approval_required",
        "backups": backups,
        "companions": companions,
    }


def iter_jsonl_family_reverse(
    path: Path, *, max_bytes: int
) -> Iterator[dict[str, Any]]:
    """Read newest records across active and rotated files within one budget."""
    from bridge_db.audit import iter_jsonl_reverse

    remaining = max_bytes
    if remaining <= 0:
        return
    for candidate in jsonl_family_paths(path):
        if remaining <= 0:
            break
        try:
            size = candidate.stat().st_size
        except OSError:
            continue
        budget = min(size, remaining)
        yield from iter_jsonl_reverse(candidate, max_bytes=budget)
        remaining -= budget
