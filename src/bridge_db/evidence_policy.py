"""Operator-reviewed, non-destructive evidence lifecycle workflows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, cast

from bridge_db import clock, config
from bridge_db.evidence import (
    append_jsonl_durable,
    jsonl_family_paths,
    legacy_raw_query_inventory,
    migration_backup_inventory,
)
from bridge_db.tools.recall import RECALL_LOG_PATH

PLAN_SCHEMA = "EvidenceLifecyclePlanV1"
ARCHIVE_SCHEMA = "EvidenceArchiveV1"
ACK_SCHEMA = "EvidenceAcknowledgementV1"
_COPY_CHUNK_BYTES = 1024 * 1024


class EvidencePolicyError(RuntimeError):
    """Raised when an evidence policy operation cannot prove its safety."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _regular_file_stat(path: Path) -> os.stat_result:
    try:
        result = path.lstat()
    except OSError as exc:
        raise EvidencePolicyError(f"evidence file unavailable: {path}") from exc
    if not stat.S_ISREG(result.st_mode):
        raise EvidencePolicyError(f"evidence path is not a regular file: {path}")
    return result


def _sha256_regular_file(path: Path) -> tuple[int, str]:
    before = _regular_file_stat(path)
    digest = hashlib.sha256()
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise EvidencePolicyError(f"evidence file could not be opened: {path}") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise EvidencePolicyError(f"evidence path is not a regular file: {path}")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise EvidencePolicyError(f"evidence file changed during open: {path}")
        total = 0
        while True:
            chunk = os.read(fd, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or total != opened.st_size:
        raise EvidencePolicyError(f"evidence file changed during hashing: {path}")
    return total, digest.hexdigest()


def _artifact_record(path: Path, *, kind: str) -> dict[str, Any]:
    size, digest = _sha256_regular_file(path)
    identity = hashlib.sha256(f"{kind}\0{path.absolute()}".encode()).hexdigest()
    return {
        "artifact_id": identity,
        "kind": kind,
        "source_path": str(path),
        "bytes": size,
        "sha256": digest,
        "retention": "preserve",
        "destructive_action": "blocked",
    }


def _family_artifacts(path: Path, *, kind: str) -> list[dict[str, Any]]:
    return [
        _artifact_record(candidate, kind=kind) for candidate in jsonl_family_paths(path)
    ]


def _validate_distinct_family_paths() -> None:
    families = {
        "audit": config.AUDIT_LOG_PATH.absolute(),
        "recall": RECALL_LOG_PATH.absolute(),
        "audit_failure": config.AUDIT_FAILURE_LOG_PATH.absolute(),
        "lifecycle_acknowledgement": config.EVIDENCE_ACK_LOG_PATH.absolute(),
    }
    if len(set(families.values())) != len(families):
        raise EvidencePolicyError("configured evidence family paths must be distinct")


def collect_evidence_plan() -> dict[str, Any]:
    """Build a content-bound lifecycle plan without exposing evidence contents."""
    _validate_distinct_family_paths()
    artifacts: list[dict[str, Any]] = []
    artifacts.extend(_family_artifacts(config.AUDIT_LOG_PATH, kind="audit"))
    artifacts.extend(_family_artifacts(RECALL_LOG_PATH, kind="recall"))
    artifacts.extend(
        _family_artifacts(config.AUDIT_FAILURE_LOG_PATH, kind="audit_failure")
    )
    artifacts.extend(
        _family_artifacts(
            config.EVIDENCE_ACK_LOG_PATH,
            kind="lifecycle_acknowledgement",
        )
    )
    for backup in sorted(config.DB_PATH.parent.glob(f"{config.DB_PATH.name}.*.bak")):
        artifacts.append(_artifact_record(backup, kind="migration_backup"))
        for suffix, kind in (
            (".sha256", "migration_backup_manifest"),
            (".meta.json", "migration_backup_metadata"),
        ):
            sidecar = Path(f"{backup}{suffix}")
            if sidecar.exists():
                artifacts.append(_artifact_record(sidecar, kind=kind))

    artifacts.sort(key=lambda item: (item["kind"], item["source_path"]))
    snapshot = {
        "schema": PLAN_SCHEMA,
        "policy": {
            "retention": "preserve_all_pending_explicit_disposition",
            "archive": "verified_copy_source_preserved",
            "acknowledgement": "review_only_no_cleanup_authority",
            "destructive_actions": "blocked",
        },
        "artifacts": artifacts,
        "historical_raw_queries": legacy_raw_query_inventory(RECALL_LOG_PATH),
        "migration_backups": migration_backup_inventory(config.DB_PATH),
    }
    snapshot_sha256 = hashlib.sha256(_canonical_json(snapshot)).hexdigest()
    return {
        **snapshot,
        "generated_at": clock.now().isoformat(),
        "snapshot_sha256": snapshot_sha256,
    }


def _copy_artifact(
    source: Path,
    destination: Path,
    *,
    expected_bytes: int,
    expected_sha256: str,
) -> None:
    source_size, source_sha256 = _sha256_regular_file(source)
    if (source_size, source_sha256) != (expected_bytes, expected_sha256):
        raise EvidencePolicyError(f"evidence changed after plan: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        source_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
    source_fd = os.open(source, source_flags)
    try:
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        digest = hashlib.sha256()
        total = 0
        try:
            while True:
                chunk = os.read(source_fd, _COPY_CHUNK_BYTES)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    if written <= 0:
                        raise EvidencePolicyError("short evidence archive write")
                    view = view[written:]
                digest.update(chunk)
                total += len(chunk)
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)
    if (total, digest.hexdigest()) != (expected_bytes, expected_sha256):
        raise EvidencePolicyError(f"evidence changed during archive copy: {source}")


def _safe_archive_relative_path(kind: str, artifact_id: str, source: Path) -> Path:
    return Path("artifacts") / kind / f"{artifact_id[:16]}-{source.name}"


def _write_durable_file(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise EvidencePolicyError("short manifest write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _secure_and_fsync_archive_directories(root: Path) -> None:
    directories = [root]
    directories.extend(path for path in root.rglob("*") if path.is_dir())
    for directory in reversed(directories):
        os.chmod(directory, 0o700)
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def verify_evidence_archive(
    archive_path: Path,
    *,
    expected_snapshot_sha256: str,
) -> dict[str, Any]:
    """Verify archive contents against an independently retained plan digest."""
    manifest_path = archive_path / "manifest.json"
    digest_path = archive_path / "manifest.sha256"
    try:
        manifest_bytes = manifest_path.read_bytes()
        expected_manifest_sha = digest_path.read_text(encoding="ascii").strip()
        manifest_raw: object = json.loads(manifest_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidencePolicyError("archive manifest is unreadable") from exc
    if not isinstance(manifest_raw, dict):
        raise EvidencePolicyError("archive manifest shape is invalid")
    manifest = cast(dict[str, Any], manifest_raw)
    actual_manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_manifest_sha != expected_manifest_sha:
        raise EvidencePolicyError("archive manifest digest mismatch")
    if manifest.get("schema") != ARCHIVE_SCHEMA:
        raise EvidencePolicyError("unsupported archive schema")
    snapshot_sha256 = manifest.get("snapshot_sha256")
    if (
        not isinstance(snapshot_sha256, str)
        or len(snapshot_sha256) != 64
        or any(character not in "0123456789abcdef" for character in snapshot_sha256)
    ):
        raise EvidencePolicyError("archive snapshot digest is invalid")
    if snapshot_sha256 != expected_snapshot_sha256:
        raise EvidencePolicyError("archive does not match expected evidence plan")

    artifacts_raw = manifest.get("artifacts")
    if not isinstance(artifacts_raw, list):
        raise EvidencePolicyError("archive artifact inventory is invalid")
    artifacts = cast(list[object], artifacts_raw)
    verified = 0
    for artifact_raw in artifacts:
        if not isinstance(artifact_raw, dict):
            raise EvidencePolicyError("archive artifact record is invalid")
        artifact = cast(dict[str, Any], artifact_raw)
        try:
            relative = Path(artifact["archive_path"])
            expected_bytes = int(artifact["bytes"])
            expected_sha256 = str(artifact["sha256"])
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidencePolicyError("archive artifact record is invalid") from exc
        if relative.is_absolute() or ".." in relative.parts:
            raise EvidencePolicyError("archive artifact path escapes archive root")
        candidate = archive_path / relative
        size, digest = _sha256_regular_file(candidate)
        if (size, digest) != (expected_bytes, expected_sha256):
            raise EvidencePolicyError(f"archive artifact digest mismatch: {relative}")
        verified += 1
    return {
        "ok": True,
        "schema": ARCHIVE_SCHEMA,
        "snapshot_sha256": snapshot_sha256,
        "artifact_count": verified,
        "source_preserved": True,
        "destructive_authority": False,
    }


def create_evidence_archive(
    archive_path: Path,
    *,
    expected_snapshot_sha256: str,
) -> dict[str, Any]:
    """Create and read-verify an atomic evidence copy; never alter sources."""
    plan = collect_evidence_plan()
    if plan["snapshot_sha256"] != expected_snapshot_sha256:
        raise EvidencePolicyError("evidence plan is stale; generate a fresh plan")
    if archive_path.exists() or archive_path.is_symlink():
        raise EvidencePolicyError("archive destination already exists")
    archive_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = archive_path.with_name(
        f".{archive_path.name}.tmp.{os.getpid()}.{secrets.token_hex(6)}"
    )
    temporary.mkdir(mode=0o700)
    manifest_artifacts: list[dict[str, Any]] = []
    try:
        for artifact in plan["artifacts"]:
            source = Path(artifact["source_path"])
            relative = _safe_archive_relative_path(
                artifact["kind"], artifact["artifact_id"], source
            )
            destination = temporary / relative
            _copy_artifact(
                source,
                destination,
                expected_bytes=artifact["bytes"],
                expected_sha256=artifact["sha256"],
            )
            manifest_artifacts.append(
                {
                    **artifact,
                    "archive_path": str(relative),
                }
            )
        manifest = {
            "schema": ARCHIVE_SCHEMA,
            "created_at": clock.now().isoformat(),
            "snapshot_sha256": plan["snapshot_sha256"],
            "policy": plan["policy"],
            "historical_raw_queries": plan["historical_raw_queries"],
            "migration_backups": plan["migration_backups"],
            "artifacts": manifest_artifacts,
            "source_preserved": True,
            "destructive_authority": False,
        }
        manifest_bytes = _canonical_json(manifest) + b"\n"
        _write_durable_file(temporary / "manifest.json", manifest_bytes)
        _write_durable_file(
            temporary / "manifest.sha256",
            hashlib.sha256(manifest_bytes).hexdigest().encode("ascii") + b"\n",
        )
        _secure_and_fsync_archive_directories(temporary)
        os.replace(temporary, archive_path)
        parent_fd = os.open(archive_path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return verify_evidence_archive(
            archive_path,
            expected_snapshot_sha256=expected_snapshot_sha256,
        )
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def acknowledge_evidence_plan(
    *,
    expected_snapshot_sha256: str,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Record review of an exact plan without granting cleanup authority."""
    if not actor.strip() or not reason.strip():
        raise EvidencePolicyError("actor and reason are required")
    actor_bytes = len(actor.strip().encode("utf-8"))
    reason_bytes = len(reason.strip().encode("utf-8"))
    if actor_bytes > config.EVIDENCE_ACK_ACTOR_MAX_BYTES:
        raise EvidencePolicyError("acknowledgement actor exceeds UTF-8 byte limit")
    if reason_bytes > config.EVIDENCE_ACK_REASON_MAX_BYTES:
        raise EvidencePolicyError("acknowledgement reason exceeds UTF-8 byte limit")
    plan = collect_evidence_plan()
    if plan["snapshot_sha256"] != expected_snapshot_sha256:
        raise EvidencePolicyError("evidence plan is stale; generate a fresh plan")
    receipt = {
        "schema": ACK_SCHEMA,
        "timestamp": clock.now().isoformat(),
        "snapshot_sha256": expected_snapshot_sha256,
        "actor": actor.strip(),
        "reason": reason.strip(),
        "status": "acknowledged",
        "authority": "review_only",
        "destructive_authority": False,
        "source_rewrite_authority": False,
    }
    append_jsonl_durable(
        config.EVIDENCE_ACK_LOG_PATH,
        receipt,
        rotate_bytes=config.AUDIT_LOG_ROTATE_BYTES,
    )
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bridge_db.evidence_policy")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("plan", help="Print a read-only, content-bound evidence plan")
    archive = subparsers.add_parser(
        "archive",
        help="Create a verified evidence copy without altering source evidence",
    )
    archive.add_argument("--destination", type=Path, required=True)
    archive.add_argument("--expected-snapshot-sha256", required=True)
    verify = subparsers.add_parser(
        "verify",
        help="Verify an archive against an independently retained plan digest",
    )
    verify.add_argument("--archive", type=Path, required=True)
    verify.add_argument("--expected-snapshot-sha256", required=True)
    acknowledge = subparsers.add_parser(
        "acknowledge",
        help="Record review of an exact plan; does not authorize cleanup",
    )
    acknowledge.add_argument("--expected-snapshot-sha256", required=True)
    acknowledge.add_argument("--actor", required=True)
    acknowledge.add_argument("--reason", required=True)
    args = parser.parse_args()

    try:
        if args.command == "plan":
            result = collect_evidence_plan()
        elif args.command == "archive":
            result = create_evidence_archive(
                args.destination,
                expected_snapshot_sha256=args.expected_snapshot_sha256,
            )
        elif args.command == "verify":
            result = verify_evidence_archive(
                args.archive,
                expected_snapshot_sha256=args.expected_snapshot_sha256,
            )
        else:
            result = acknowledge_evidence_plan(
                expected_snapshot_sha256=args.expected_snapshot_sha256,
                actor=args.actor,
                reason=args.reason,
            )
    except EvidencePolicyError as exc:
        print(f"evidence lifecycle refused: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
