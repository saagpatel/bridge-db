"""Non-destructive lifecycle helpers for local durable-evidence files."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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


def sha256_file(path: Path) -> str:
    """Hash a file without materializing it."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def migration_backup_inventory(db_path: Path) -> dict[str, Any]:
    """Inventory and verify sibling migration backups without deleting them."""
    backups: list[dict[str, Any]] = []
    for backup in sorted(db_path.parent.glob(f"{db_path.name}.*.bak")):
        manifest = Path(f"{backup}.sha256")
        metadata = Path(f"{backup}.meta.json")
        errors: list[str] = []
        schema_version: int | None = None
        integrity_ok = False
        digest_ok = False
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
        backups.append(
            {
                "path": str(backup),
                "bytes": backup.stat().st_size,
                "manifest_path": str(manifest),
                "metadata_path": str(metadata),
                "metadata_exists": metadata.exists(),
                "digest_ok": digest_ok,
                "integrity_ok": integrity_ok,
                "schema_version": schema_version,
                "recovery_readback": "verified" if not errors else "unverified",
                "retention_policy": "operator_acknowledgement_required",
                "cleanup": "approval_required",
                "errors": sorted(set(errors)),
            }
        )
    return {
        "count": len(backups),
        "verified_count": sum(
            item["recovery_readback"] == "verified" for item in backups
        ),
        "cleanup": "approval_required",
        "backups": backups,
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
