"""Atomic, content-bound recovery anchors for the current BridgeDB database."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, cast

from bridge_db import clock

RECOVERY_ANCHOR_SCHEMA = "RecoveryAnchorV1"
RECOVERY_ANCHOR_SUFFIX = ".recovery-anchor-v1"
RECOVERY_DATABASE_NAME = "anchor.sqlite"
RECOVERY_MANIFEST_NAME = "manifest.json"
SEMANTIC_READBACK_TABLES = (
    "context_sections",
    "activity_log",
    "pending_handoffs",
    "system_snapshots",
    "cost_records",
)


def recovery_anchor_path(db_path: Path) -> Path:
    """Return the stable, non-legacy path for the current recovery anchor."""
    return db_path.with_name(f"{db_path.name}{RECOVERY_ANCHOR_SUFFIX}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_readback(path: Path) -> tuple[int, dict[str, int], bool]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as check:
        integrity_row = check.execute("PRAGMA integrity_check").fetchone()
        integrity_ok = integrity_row is not None and integrity_row[0] == "ok"
        version_row = check.execute("PRAGMA user_version").fetchone()
        schema_version = int(version_row[0]) if version_row is not None else -1
        row_counts: dict[str, int] = {}
        for table in SEMANTIC_READBACK_TABLES:
            table_row = check.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if table_row is None:
                raise sqlite3.DatabaseError(f"required table missing: {table}")
            count_row = check.execute(
                f'SELECT COUNT(*) FROM "{table}"'  # noqa: S608
            ).fetchone()
            if count_row is None:
                raise sqlite3.DatabaseError(f"row count unavailable: {table}")
            row_counts[table] = int(count_row[0])
    return schema_version, row_counts, integrity_ok


def _write_private_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short recovery-anchor write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_to_private_file(path: Path, source: BinaryIO) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(path, flags, 0o600)
    try:
        while chunk := source.read(1024 * 1024):
            view = memoryview(chunk)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short recovery-copy write")
                view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_recovery_anchor(
    anchor_path: Path,
    *,
    expected_schema_version: int,
) -> dict[str, Any]:
    """Verify one anchor using a disposable recovery copy and bounded reads."""
    database_path = anchor_path / RECOVERY_DATABASE_NAME
    manifest_path = anchor_path / RECOVERY_MANIFEST_NAME
    errors: list[str] = []
    manifest: dict[str, object] | None = None

    if not anchor_path.is_dir():
        return {
            "state": "missing" if not anchor_path.exists() else "invalid",
            "ready": False,
            "path": str(anchor_path),
            "database_path": str(database_path),
            "manifest_path": str(manifest_path),
            "errors": [
                "anchor_missing" if not anchor_path.exists() else "anchor_not_directory"
            ],
        }

    try:
        if anchor_path.stat().st_mode & 0o077:
            errors.append("anchor_permissions_not_private")
    except OSError:
        errors.append("anchor_unreadable")

    try:
        loaded = cast(
            object,
            json.loads(manifest_path.read_text(encoding="utf-8")),
        )
        if isinstance(loaded, dict):
            loaded_map = cast(dict[object, object], loaded)
            manifest = {
                key: value for key, value in loaded_map.items() if isinstance(key, str)
            }
        else:
            errors.append("metadata_invalid")
    except FileNotFoundError:
        errors.append("metadata_missing")
    except (OSError, json.JSONDecodeError, UnicodeError):
        errors.append("metadata_unreadable")

    backup_bytes: int | None = None
    actual_digest: str | None = None
    try:
        backup_bytes = database_path.stat().st_size
        if database_path.stat().st_mode & 0o077:
            errors.append("backup_permissions_not_private")
        actual_digest = _sha256_file(database_path)
    except FileNotFoundError:
        errors.append("backup_missing")
    except OSError:
        errors.append("backup_unreadable")

    manifest_counts: dict[str, int] | None = None
    if manifest is not None:
        try:
            if manifest_path.stat().st_mode & 0o077:
                errors.append("metadata_permissions_not_private")
        except OSError:
            errors.append("metadata_unreadable")
        required_types = {
            "schema": str,
            "created_at": str,
            "source_schema_version": int,
            "backup_bytes": int,
            "sha256": str,
            "semantic_readback": dict,
        }
        for key, expected_type in required_types.items():
            if not isinstance(manifest.get(key), expected_type):
                errors.append(f"metadata_{key}_invalid")
        if manifest.get("schema") != RECOVERY_ANCHOR_SCHEMA:
            errors.append("metadata_schema_invalid")
        created_at = manifest.get("created_at")
        if isinstance(created_at, str):
            try:
                datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except ValueError:
                errors.append("metadata_created_at_invalid")
        if (
            isinstance(manifest.get("source_schema_version"), int)
            and manifest["source_schema_version"] != expected_schema_version
        ):
            errors.append("schema_incompatible")
        if (
            backup_bytes is not None
            and isinstance(manifest.get("backup_bytes"), int)
            and manifest["backup_bytes"] != backup_bytes
        ):
            errors.append("byte_size_mismatch")
        if (
            actual_digest is not None
            and isinstance(manifest.get("sha256"), str)
            and manifest["sha256"] != actual_digest
        ):
            errors.append("digest_mismatch")
        semantic = manifest.get("semantic_readback")
        raw_counts: object = None
        if isinstance(semantic, dict):
            semantic_map = cast(dict[object, object], semantic)
            raw_counts = semantic_map.get("row_counts")
        parsed_counts: dict[str, int] = {}
        if isinstance(raw_counts, dict):
            raw_counts_map = cast(dict[object, object], raw_counts)
            for key, value in raw_counts_map.items():
                if isinstance(key, str) and isinstance(value, int) and value >= 0:
                    parsed_counts[key] = value
        if set(parsed_counts) == set(SEMANTIC_READBACK_TABLES):
            manifest_counts = parsed_counts
        else:
            errors.append("semantic_metadata_invalid")

    restored_schema_version: int | None = None
    integrity_ok = False
    semantic_readback_ok = False
    if database_path.is_file():
        try:
            with tempfile.TemporaryDirectory(prefix="bridge-recovery-verify-") as temp:
                disposable = Path(temp) / "restored.sqlite"
                with database_path.open("rb") as source:
                    _copy_to_private_file(disposable, source)
                (
                    restored_schema_version,
                    restored_counts,
                    integrity_ok,
                ) = _sqlite_readback(disposable)
            if not integrity_ok:
                errors.append("integrity_check_failed")
            if restored_schema_version != expected_schema_version:
                errors.append("schema_incompatible")
            if manifest_counts is not None:
                semantic_readback_ok = restored_counts == manifest_counts
                if not semantic_readback_ok:
                    errors.append("semantic_readback_mismatch")
        except (OSError, sqlite3.Error, TypeError, ValueError):
            errors.append("disposable_recovery_failed")

    errors = sorted(set(errors))
    return {
        "state": "verified" if not errors else "invalid",
        "ready": not errors,
        "path": str(anchor_path),
        "database_path": str(database_path),
        "manifest_path": str(manifest_path),
        "created_at": manifest.get("created_at") if manifest is not None else None,
        "schema_version": restored_schema_version,
        "expected_schema_version": expected_schema_version,
        "backup_bytes": backup_bytes,
        "sha256": actual_digest,
        "digest_ok": actual_digest is not None
        and manifest is not None
        and manifest.get("sha256") == actual_digest,
        "integrity_ok": integrity_ok,
        "semantic_readback_ok": semantic_readback_ok,
        "recovery_readback": "verified" if not errors else "unverified",
        "permissions": "private",
        "cleanup": "approval_required",
        "errors": errors,
    }


def recovery_anchor_inventory(
    db_path: Path,
    *,
    expected_schema_version: int,
) -> dict[str, Any]:
    """Return non-sensitive readiness for the stable current anchor."""
    return verify_recovery_anchor(
        recovery_anchor_path(db_path),
        expected_schema_version=expected_schema_version,
    )


def create_recovery_anchor(
    db_path: Path,
    *,
    expected_schema_version: int,
) -> dict[str, Any]:
    """Create one atomically published RecoveryAnchorV1 bundle.

    Existing anchors are never overwritten. Callers may verify an existing
    bundle, but replacing or deleting recovery evidence requires separate
    operator authority.
    """
    if not db_path.is_file():
        raise FileNotFoundError(f"database does not exist: {db_path}")
    anchor_path = recovery_anchor_path(db_path)
    if anchor_path.exists():
        raise FileExistsError(f"recovery anchor already exists: {anchor_path}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{anchor_path.name}.tmp-",
            dir=db_path.parent,
        )
    )
    os.chmod(temporary, 0o700)
    published = False
    try:
        database_path = temporary / RECOVERY_DATABASE_NAME
        source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        target = sqlite3.connect(database_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        os.chmod(database_path, 0o600)
        _fsync_file(database_path)

        schema_version, row_counts, integrity_ok = _sqlite_readback(database_path)
        if not integrity_ok:
            raise RuntimeError("online backup failed SQLite integrity verification")
        if schema_version != expected_schema_version:
            raise RuntimeError(
                "online backup schema is incompatible "
                f"(found v{schema_version}, expected v{expected_schema_version})"
            )

        digest = _sha256_file(database_path)
        manifest = {
            "schema": RECOVERY_ANCHOR_SCHEMA,
            "created_at": clock.now().isoformat().replace("+00:00", "Z"),
            "source_schema_version": schema_version,
            "backup_bytes": database_path.stat().st_size,
            "sha256": digest,
            "sqlite_integrity": "ok",
            "recovery_readback": "verified",
            "semantic_readback": {
                "tables": list(SEMANTIC_READBACK_TABLES),
                "row_counts": row_counts,
            },
            "publication": "atomic_directory_replace",
            "retention_policy": "preserve_pending_operator_approval",
            "cleanup": "approval_required",
        }
        _write_private_file(
            temporary / RECOVERY_MANIFEST_NAME,
            (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
        )
        _fsync_directory(temporary)

        staged = verify_recovery_anchor(
            temporary,
            expected_schema_version=expected_schema_version,
        )
        if not staged["ready"]:
            raise RuntimeError(
                "staged recovery anchor failed verification: "
                + ",".join(staged["errors"])
            )

        os.replace(temporary, anchor_path)
        published = True
        _fsync_directory(db_path.parent)
    finally:
        if not published:
            shutil.rmtree(temporary, ignore_errors=True)

    verified = verify_recovery_anchor(
        anchor_path,
        expected_schema_version=expected_schema_version,
    )
    if not verified["ready"]:
        raise RuntimeError(
            "published recovery anchor failed verification: "
            + ",".join(verified["errors"])
        )
    return verified
