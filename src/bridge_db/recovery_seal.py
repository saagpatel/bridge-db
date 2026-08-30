"""Owned, exactly-once lifecycle receipts for RecoveryAnchorV1 sealing."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from bridge_db import clock
from bridge_db.recovery import (
    LEGACY_RECOVERY_SOURCE_FINGERPRINT_SCHEMA,
    RECOVERY_SOURCE_FINGERPRINT_SCHEMA,
    recovery_anchor_inventory,
    recovery_source_fingerprint,
    recovery_source_fingerprint_schema,
    rotate_recovery_anchor,
)

RECOVERY_SEAL_ATTEMPT_SCHEMA = "RecoverySealAttemptV1"
RECOVERY_SEAL_RECEIPT_SCHEMA = "RecoverySealReceiptV1"
RECOVERY_SEAL_SUFFIX = ".recovery-seals-v1"
RECOVERY_SEAL_ATTEMPT_NAME = "attempt.json"
RECOVERY_SEAL_RECEIPT_NAME = "terminal.json"
RECOVERY_SEAL_AUTHORIZATION = "channel_bound_principal_scope"
RECOVERY_SEAL_OWNERS = frozenset({"cc", "codex"})
RECOVERY_SEAL_MAX_RECORD_BYTES = 64 * 1024
RECOVERY_SEAL_MAX_BATCHES = 1024

_BATCH_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
_HEX_64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ATTEMPT_KEYS = frozenset(
    {
        "schema",
        "batch_id",
        "seal_owner",
        "authorization",
        "started_at",
        "source_fingerprint_sha256",
        "retention_policy",
        "cleanup",
        "attempt_sha256",
    }
)
_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "batch_id",
        "seal_owner",
        "authorization",
        "started_at",
        "completed_at",
        "outcome",
        "reason_code",
        "source_fingerprint_sha256",
        "anchor_state",
        "anchor_sha256",
        "disposition",
        "rotated",
        "superseded_path",
        "superseded_sha256",
        "digest_ok",
        "integrity_ok",
        "semantic_readback_ok",
        "source_current",
        "ready",
        "retention_policy",
        "cleanup",
        "receipt_sha256",
    }
)


class RecoverySealProtocolError(RuntimeError):
    """Fail-closed recovery-seal state-machine error with a stable reason."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def recovery_seal_path(db_path: Path) -> Path:
    """Return the sibling directory holding append-only batch attempts."""
    return db_path.with_name(f"{db_path.name}{RECOVERY_SEAL_SUFFIX}")


def _lock_path(db_path: Path) -> Path:
    return db_path.with_name(f".{db_path.name}{RECOVERY_SEAL_SUFFIX}.lock")


def _batch_key(batch_id: str) -> str:
    return hashlib.sha256(batch_id.encode("utf-8")).hexdigest()


def _timestamp() -> str:
    return clock.now().isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _validate_batch_id(batch_id: str) -> None:
    if not _BATCH_ID_PATTERN.fullmatch(batch_id):
        raise RecoverySealProtocolError("batch_id_invalid")


def _validate_owner(owner: str) -> None:
    if owner not in RECOVERY_SEAL_OWNERS:
        raise RecoverySealProtocolError("seal_owner_unauthorized")


def _canonical_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sign_record(payload: dict[str, Any], digest_key: str) -> dict[str, Any]:
    return {**payload, digest_key: _canonical_digest(payload)}


def _stable_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_private_json(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise RecoverySealProtocolError("receipt_symlink")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise RecoverySealProtocolError("receipt_missing") from exc
    except OSError as exc:
        raise RecoverySealProtocolError("receipt_unreadable") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RecoverySealProtocolError("receipt_not_regular")
        if before.st_mode & 0o077:
            raise RecoverySealProtocolError("receipt_permissions_not_private")
        if before.st_size > RECOVERY_SEAL_MAX_RECORD_BYTES:
            raise RecoverySealProtocolError("receipt_oversized")
        chunks: list[bytes] = []
        remaining = RECOVERY_SEAL_MAX_RECORD_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining <= 0 and os.read(fd, 1):
            raise RecoverySealProtocolError("receipt_oversized")
        after = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        path_after = path.lstat()
    except OSError as exc:
        raise RecoverySealProtocolError("receipt_changed_during_read") from exc
    signature = _stable_signature(before)
    if (
        _stable_signature(after) != signature
        or _stable_signature(path_after) != signature
    ):
        raise RecoverySealProtocolError("receipt_changed_during_read")
    try:
        loaded = cast(object, json.loads(b"".join(chunks).decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RecoverySealProtocolError("receipt_invalid_json") from exc
    if not isinstance(loaded, dict):
        raise RecoverySealProtocolError("receipt_invalid_shape")
    raw = cast(dict[object, object], loaded)
    if not all(isinstance(key, str) for key in raw):
        raise RecoverySealProtocolError("receipt_invalid_shape")
    return cast(dict[str, Any], raw)


def _verify_digest(
    record: dict[str, Any],
    *,
    expected_keys: frozenset[str],
    digest_key: str,
) -> None:
    if frozenset(record) != expected_keys:
        raise RecoverySealProtocolError("receipt_field_set_invalid")
    stored = record.get(digest_key)
    if not isinstance(stored, str) or not _HEX_64_PATTERN.fullmatch(stored):
        raise RecoverySealProtocolError("receipt_digest_invalid")
    unsigned = {key: value for key, value in record.items() if key != digest_key}
    if _canonical_digest(unsigned) != stored:
        raise RecoverySealProtocolError("receipt_digest_mismatch")


def verify_recovery_seal_attempt(path: Path) -> dict[str, Any]:
    """Verify one private, content-bound RecoverySealAttemptV1 record."""
    record = _read_private_json(path)
    _verify_digest(
        record,
        expected_keys=_ATTEMPT_KEYS,
        digest_key="attempt_sha256",
    )
    if record["schema"] != RECOVERY_SEAL_ATTEMPT_SCHEMA:
        raise RecoverySealProtocolError("attempt_schema_invalid")
    batch_id = record["batch_id"]
    owner = record["seal_owner"]
    if not isinstance(batch_id, str):
        raise RecoverySealProtocolError("batch_id_invalid")
    _validate_batch_id(batch_id)
    if not isinstance(owner, str):
        raise RecoverySealProtocolError("seal_owner_unauthorized")
    _validate_owner(owner)
    if record["authorization"] != RECOVERY_SEAL_AUTHORIZATION:
        raise RecoverySealProtocolError("authorization_invalid")
    if _parse_timestamp(record["started_at"]) is None:
        raise RecoverySealProtocolError("attempt_timestamp_invalid")
    fingerprint = record["source_fingerprint_sha256"]
    if not isinstance(fingerprint, str) or not _HEX_64_PATTERN.fullmatch(fingerprint):
        raise RecoverySealProtocolError("source_fingerprint_invalid")
    if (
        record["retention_policy"] != "preserve_all_pending_approval"
        or record["cleanup"] != "approval_required"
    ):
        raise RecoverySealProtocolError("retention_contract_invalid")
    return record


def verify_recovery_seal_receipt(path: Path) -> dict[str, Any]:
    """Verify one terminal RecoverySealReceiptV1 without trusting its claims."""
    record = _read_private_json(path)
    _verify_digest(
        record,
        expected_keys=_RECEIPT_KEYS,
        digest_key="receipt_sha256",
    )
    if record["schema"] != RECOVERY_SEAL_RECEIPT_SCHEMA:
        raise RecoverySealProtocolError("receipt_schema_invalid")
    batch_id = record["batch_id"]
    owner = record["seal_owner"]
    if not isinstance(batch_id, str):
        raise RecoverySealProtocolError("batch_id_invalid")
    _validate_batch_id(batch_id)
    if not isinstance(owner, str):
        raise RecoverySealProtocolError("seal_owner_unauthorized")
    _validate_owner(owner)
    if record["authorization"] != RECOVERY_SEAL_AUTHORIZATION:
        raise RecoverySealProtocolError("authorization_invalid")
    if (
        _parse_timestamp(record["started_at"]) is None
        or _parse_timestamp(record["completed_at"]) is None
    ):
        raise RecoverySealProtocolError("receipt_timestamp_invalid")
    fingerprint = record["source_fingerprint_sha256"]
    if not isinstance(fingerprint, str) or not _HEX_64_PATTERN.fullmatch(fingerprint):
        raise RecoverySealProtocolError("source_fingerprint_invalid")
    if record["outcome"] not in {"recovery_sealed", "recovery_unsealed"}:
        raise RecoverySealProtocolError("receipt_outcome_invalid")
    if not isinstance(record["reason_code"], str) or not record["reason_code"]:
        raise RecoverySealProtocolError("receipt_reason_invalid")
    if type(record["rotated"]) is not bool or type(record["ready"]) is not bool:
        raise RecoverySealProtocolError("receipt_boolean_invalid")
    for key in (
        "digest_ok",
        "integrity_ok",
        "semantic_readback_ok",
        "source_current",
    ):
        if record[key] is not None and type(record[key]) is not bool:
            raise RecoverySealProtocolError("receipt_boolean_invalid")
    for key in ("anchor_sha256", "superseded_sha256"):
        value = record[key]
        if value is not None and (
            not isinstance(value, str) or not _HEX_64_PATTERN.fullmatch(value)
        ):
            raise RecoverySealProtocolError("receipt_anchor_digest_invalid")
    for key in ("anchor_state", "disposition", "superseded_path"):
        value = record[key]
        if value is not None and not isinstance(value, str):
            raise RecoverySealProtocolError("receipt_field_type_invalid")
    if record["outcome"] == "recovery_sealed":
        if (
            record["reason_code"] != "verified_current_anchor"
            or record["ready"] is not True
            or record["digest_ok"] is not True
            or record["integrity_ok"] is not True
            or record["semantic_readback_ok"] is not True
            or record["source_current"] is not True
            or record["anchor_state"] != "verified"
            or record["anchor_sha256"] is None
            or record["disposition"] not in {"rotated", "preserved_current"}
        ):
            raise RecoverySealProtocolError("sealed_receipt_claim_invalid")
    elif record["ready"] is not False:
        raise RecoverySealProtocolError("unsealed_receipt_claim_invalid")
    if (
        record["retention_policy"] != "preserve_all_pending_approval"
        or record["cleanup"] != "approval_required"
    ):
        raise RecoverySealProtocolError("retention_contract_invalid")
    return record


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _validate_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise RecoverySealProtocolError("receipt_directory_symlink")
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise RecoverySealProtocolError("receipt_directory_missing") from exc
    except OSError as exc:
        raise RecoverySealProtocolError("receipt_directory_unreadable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise RecoverySealProtocolError("receipt_directory_not_directory")
    if metadata.st_mode & 0o077:
        raise RecoverySealProtocolError("receipt_directory_permissions_not_private")


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
        _fsync_directory(path.parent)
    except FileExistsError:
        pass
    _validate_private_directory(path)


def _list_recovery_seal_candidates(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for candidate in root.iterdir():
        candidates.append(candidate)
        if len(candidates) > RECOVERY_SEAL_MAX_BATCHES:
            raise RecoverySealProtocolError("recovery_seal_capacity_exceeded")
    return candidates


def _assert_batch_capacity(root: Path, batch_path: Path) -> None:
    if batch_path.exists() or batch_path.is_symlink():
        return
    for count, _candidate in enumerate(root.iterdir(), start=1):
        if count >= RECOVERY_SEAL_MAX_BATCHES:
            raise RecoverySealProtocolError("recovery_seal_capacity_exceeded")


def _publish_record(path: Path, record: dict[str, Any]) -> None:
    encoded = (
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")
    if len(encoded) > RECOVERY_SEAL_MAX_RECORD_BYTES:
        raise RecoverySealProtocolError("receipt_oversized")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short recovery-seal receipt write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    linked = False
    try:
        os.link(temporary, path, follow_symlinks=False)
        linked = True
        temporary.unlink()
        _fsync_directory(path.parent)
    except FileExistsError:
        raise
    except BaseException:
        if linked:
            try:
                if path.exists() and not path.is_symlink():
                    path.unlink()
                    _fsync_directory(path.parent)
            except BaseException:
                pass
        raise
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


@contextmanager
def _seal_lock(db_path: Path) -> Generator[None]:
    path = _lock_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise RecoverySealProtocolError("seal_lock_unavailable") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise RecoverySealProtocolError("seal_lock_invalid")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _attempt_record(
    *,
    batch_id: str,
    owner: str,
    source_fingerprint: str,
) -> dict[str, Any]:
    return _sign_record(
        {
            "schema": RECOVERY_SEAL_ATTEMPT_SCHEMA,
            "batch_id": batch_id,
            "seal_owner": owner,
            "authorization": RECOVERY_SEAL_AUTHORIZATION,
            "started_at": _timestamp(),
            "source_fingerprint_sha256": source_fingerprint,
            "retention_policy": "preserve_all_pending_approval",
            "cleanup": "approval_required",
        },
        "attempt_sha256",
    )


def _receipt_record(
    attempt: dict[str, Any],
    *,
    outcome: str,
    reason_code: str,
    anchor: dict[str, Any] | None,
) -> dict[str, Any]:
    anchor = anchor or {}
    return _sign_record(
        {
            "schema": RECOVERY_SEAL_RECEIPT_SCHEMA,
            "batch_id": attempt["batch_id"],
            "seal_owner": attempt["seal_owner"],
            "authorization": RECOVERY_SEAL_AUTHORIZATION,
            "started_at": attempt["started_at"],
            "completed_at": _timestamp(),
            "outcome": outcome,
            "reason_code": reason_code,
            "source_fingerprint_sha256": attempt["source_fingerprint_sha256"],
            "anchor_state": anchor.get("state"),
            "anchor_sha256": anchor.get("sha256"),
            "disposition": anchor.get("disposition"),
            "rotated": bool(anchor.get("rotated", False)),
            "superseded_path": anchor.get("superseded_path"),
            "superseded_sha256": anchor.get("superseded_sha256"),
            "digest_ok": anchor.get("digest_ok"),
            "integrity_ok": anchor.get("integrity_ok"),
            "semantic_readback_ok": anchor.get("semantic_readback_ok"),
            "source_current": anchor.get("source_current"),
            "ready": outcome == "recovery_sealed",
            "retention_policy": "preserve_all_pending_approval",
            "cleanup": "approval_required",
        },
        "receipt_sha256",
    )


def _receipt_reason(exc: BaseException) -> str:
    if isinstance(exc, RecoverySealProtocolError):
        return exc.reason_code
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return "seal_interrupted"
    if isinstance(exc, OSError):
        return "recovery_io_failed"
    return "recovery_rotation_failed"


def _best_effort_anchor(
    db_path: Path,
    *,
    expected_schema_version: int,
) -> dict[str, Any] | None:
    try:
        return recovery_anchor_inventory(
            db_path,
            expected_schema_version=expected_schema_version,
        )
    except BaseException:
        return None


def _assert_attempt_identity(
    attempt: dict[str, Any],
    *,
    batch_id: str,
    owner: str,
) -> None:
    if attempt["batch_id"] != batch_id:
        raise RecoverySealProtocolError("batch_identity_conflict")
    if attempt["seal_owner"] != owner:
        raise RecoverySealProtocolError("batch_owner_mismatch")


def _assert_receipt_identity(
    receipt: dict[str, Any],
    *,
    batch_id: str,
    owner: str,
) -> None:
    if receipt["batch_id"] != batch_id:
        raise RecoverySealProtocolError("batch_identity_conflict")
    if receipt["seal_owner"] != owner:
        raise RecoverySealProtocolError("batch_owner_mismatch")


def _assert_current_sealed_receipt(
    db_path: Path,
    receipt: dict[str, Any],
    *,
    expected_schema_version: int,
) -> None:
    if receipt["outcome"] != "recovery_sealed":
        return
    try:
        fingerprint_schema = recovery_source_fingerprint_schema(
            db_path,
            cast(str, receipt["source_fingerprint_sha256"]),
            expected_schema_version=expected_schema_version,
        )
    except BaseException as exc:
        raise RecoverySealProtocolError("source_fingerprint_unavailable") from exc
    if fingerprint_schema is None:
        raise RecoverySealProtocolError("source_changed_since_recovery_seal")
    anchor = _best_effort_anchor(
        db_path,
        expected_schema_version=expected_schema_version,
    )
    if anchor is None or anchor.get("sha256") != receipt["anchor_sha256"]:
        raise RecoverySealProtocolError("current_anchor_receipt_mismatch")
    if anchor.get("ready") is not True:
        raise RecoverySealProtocolError("current_recovery_anchor_not_ready")


def seal_recovery_batch(
    db_path: Path,
    *,
    expected_schema_version: int,
    batch_id: str,
    owner: str,
) -> dict[str, Any]:
    """Seal one completed write batch under a pre-authorized owner identity.

    Authentication happens before this function. The filesystem protocol is
    then preservation-only: one immutable attempt is followed by at most one
    immutable terminal receipt. Repeated and concurrent calls replay that
    terminal receipt instead of rotating or appending evidence again.
    """
    _validate_batch_id(batch_id)
    _validate_owner(owner)

    with _seal_lock(db_path):
        root = recovery_seal_path(db_path)
        _ensure_private_directory(root)
        batch_path = root / _batch_key(batch_id)
        _assert_batch_capacity(root, batch_path)
        _ensure_private_directory(batch_path)
        attempt_path = batch_path / RECOVERY_SEAL_ATTEMPT_NAME
        receipt_path = batch_path / RECOVERY_SEAL_RECEIPT_NAME
        artifact_names = {path.name for path in batch_path.iterdir()}
        if not artifact_names.issubset(
            {RECOVERY_SEAL_ATTEMPT_NAME, RECOVERY_SEAL_RECEIPT_NAME}
        ):
            raise RecoverySealProtocolError("receipt_artifact_set_invalid")

        if receipt_path.exists() or receipt_path.is_symlink():
            receipt = verify_recovery_seal_receipt(receipt_path)
            _assert_receipt_identity(receipt, batch_id=batch_id, owner=owner)
            _assert_current_sealed_receipt(
                db_path,
                receipt,
                expected_schema_version=expected_schema_version,
            )
            return {**receipt, "replayed": True}

        if attempt_path.exists() or attempt_path.is_symlink():
            attempt = verify_recovery_seal_attempt(attempt_path)
            _assert_attempt_identity(attempt, batch_id=batch_id, owner=owner)
        else:
            source_fingerprint = recovery_source_fingerprint(db_path)
            attempt = _attempt_record(
                batch_id=batch_id,
                owner=owner,
                source_fingerprint=source_fingerprint,
            )
            _publish_record(attempt_path, attempt)

        attempt_fingerprint_schema: str | None = None
        try:
            attempt_fingerprint_schema = recovery_source_fingerprint_schema(
                db_path,
                cast(str, attempt["source_fingerprint_sha256"]),
                expected_schema_version=expected_schema_version,
            )
        except BaseException as exc:
            initial_error: BaseException | None = exc
        else:
            initial_error = None

        if (
            initial_error is not None
            or attempt_fingerprint_schema != RECOVERY_SOURCE_FINGERPRINT_SCHEMA
        ):
            reason = (
                "source_fingerprint_unavailable"
                if initial_error is not None
                else "source_fingerprint_schema_legacy"
                if attempt_fingerprint_schema
                == LEGACY_RECOVERY_SOURCE_FINGERPRINT_SCHEMA
                else "source_changed_since_seal_attempt"
            )
            receipt = _receipt_record(
                attempt,
                outcome="recovery_unsealed",
                reason_code=reason,
                anchor=_best_effort_anchor(
                    db_path,
                    expected_schema_version=expected_schema_version,
                ),
            )
            _publish_record(receipt_path, receipt)
            return {**verify_recovery_seal_receipt(receipt_path), "replayed": False}

        def publish_verified_receipt(anchor: dict[str, Any]) -> None:
            if (
                anchor.get("ready") is not True
                or anchor.get("state") != "verified"
                or anchor.get("digest_ok") is not True
                or anchor.get("integrity_ok") is not True
                or anchor.get("semantic_readback_ok") is not True
                or anchor.get("source_current") is not True
            ):
                raise RecoverySealProtocolError("anchor_readback_failed")
            if (
                recovery_source_fingerprint(db_path)
                != attempt["source_fingerprint_sha256"]
            ):
                raise RecoverySealProtocolError("source_changed_during_seal")
            receipt = _receipt_record(
                attempt,
                outcome="recovery_sealed",
                reason_code="verified_current_anchor",
                anchor=anchor,
            )
            _publish_record(receipt_path, receipt)

        try:
            rotate_recovery_anchor(
                db_path,
                expected_schema_version=expected_schema_version,
                on_verified=publish_verified_receipt,
            )
        except BaseException as exc:
            if receipt_path.exists() or receipt_path.is_symlink():
                receipt = verify_recovery_seal_receipt(receipt_path)
                _assert_receipt_identity(receipt, batch_id=batch_id, owner=owner)
                _assert_current_sealed_receipt(
                    db_path,
                    receipt,
                    expected_schema_version=expected_schema_version,
                )
            else:
                receipt = _receipt_record(
                    attempt,
                    outcome="recovery_unsealed",
                    reason_code=_receipt_reason(exc),
                    anchor=_best_effort_anchor(
                        db_path,
                        expected_schema_version=expected_schema_version,
                    ),
                )
                try:
                    _publish_record(receipt_path, receipt)
                except BaseException as receipt_exc:
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise exc from receipt_exc
                    raise RecoverySealProtocolError(
                        "terminal_receipt_unavailable"
                    ) from receipt_exc
                receipt = verify_recovery_seal_receipt(receipt_path)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return {**receipt, "replayed": False}

        receipt = verify_recovery_seal_receipt(receipt_path)
        _assert_receipt_identity(receipt, batch_id=batch_id, owner=owner)
        _assert_current_sealed_receipt(
            db_path,
            receipt,
            expected_schema_version=expected_schema_version,
        )
        return {**receipt, "replayed": False}


def _inventory_base(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "state": "missing",
        "ready": False,
        "attempt_count": 0,
        "sealed_count": 0,
        "unsealed_count": 0,
        "open_count": 0,
        "invalid_count": 0,
        "latest": None,
        "retention_policy": "preserve_all_pending_approval",
        "cleanup": "approval_required",
        "errors": [],
    }


def recovery_seal_inventory(
    db_path: Path,
    *,
    expected_schema_version: int,
    current_anchor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarize recovery-seal receipts without mutating or repairing them."""
    root = recovery_seal_path(db_path)
    base = _inventory_base(root)
    if not root.exists() and not root.is_symlink():
        return {
            **base,
            "errors": ["recovery_seal_receipt_missing"],
        }
    try:
        _validate_private_directory(root)
        candidates = _list_recovery_seal_candidates(root)
    except (OSError, RecoverySealProtocolError) as exc:
        reason = (
            exc.reason_code
            if isinstance(exc, RecoverySealProtocolError)
            else "receipt_directory_unreadable"
        )
        return {
            **base,
            "state": "invalid",
            "invalid_count": 1,
            "errors": [reason],
        }

    attempts: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    invalid_count = 0
    errors: list[str] = []
    for candidate in candidates:
        try:
            if (
                candidate.is_symlink()
                or not candidate.is_dir()
                or not _HEX_64_PATTERN.fullmatch(candidate.name)
            ):
                raise RecoverySealProtocolError("receipt_batch_directory_invalid")
            metadata = candidate.lstat()
            if metadata.st_mode & 0o077:
                raise RecoverySealProtocolError(
                    "receipt_directory_permissions_not_private"
                )
            artifact_names = {path.name for path in candidate.iterdir()}
            if not artifact_names.issubset(
                {RECOVERY_SEAL_ATTEMPT_NAME, RECOVERY_SEAL_RECEIPT_NAME}
            ):
                raise RecoverySealProtocolError("receipt_artifact_set_invalid")
            attempt = verify_recovery_seal_attempt(
                candidate / RECOVERY_SEAL_ATTEMPT_NAME
            )
            if candidate.name != _batch_key(cast(str, attempt["batch_id"])):
                raise RecoverySealProtocolError("batch_identity_conflict")
            receipt: dict[str, Any] | None = None
            terminal = candidate / RECOVERY_SEAL_RECEIPT_NAME
            if terminal.exists() or terminal.is_symlink():
                receipt = verify_recovery_seal_receipt(terminal)
                _assert_receipt_identity(
                    receipt,
                    batch_id=cast(str, attempt["batch_id"]),
                    owner=cast(str, attempt["seal_owner"]),
                )
                if receipt["started_at"] != attempt["started_at"]:
                    raise RecoverySealProtocolError("receipt_attempt_mismatch")
                if (
                    receipt["source_fingerprint_sha256"]
                    != attempt["source_fingerprint_sha256"]
                ):
                    raise RecoverySealProtocolError("receipt_attempt_mismatch")
            attempts.append((attempt, receipt))
        except (OSError, RecoverySealProtocolError) as exc:
            invalid_count += 1
            errors.append(
                exc.reason_code
                if isinstance(exc, RecoverySealProtocolError)
                else "receipt_unreadable"
            )

    sealed_count = sum(
        receipt is not None and receipt["outcome"] == "recovery_sealed"
        for _, receipt in attempts
    )
    unsealed_count = sum(
        receipt is not None and receipt["outcome"] == "recovery_unsealed"
        for _, receipt in attempts
    )
    open_count = sum(receipt is None for _, receipt in attempts)
    result = {
        **base,
        "attempt_count": len(attempts),
        "sealed_count": sealed_count,
        "unsealed_count": unsealed_count,
        "open_count": open_count,
        "invalid_count": invalid_count,
    }
    if invalid_count:
        return {
            **result,
            "state": "invalid",
            "errors": sorted(set(errors)),
        }
    if not attempts:
        return {
            **result,
            "errors": ["recovery_seal_receipt_missing"],
        }

    def attempt_sort_key(
        item: tuple[dict[str, Any], dict[str, Any] | None],
    ) -> tuple[datetime, str]:
        started_at = _parse_timestamp(item[0]["started_at"])
        if started_at is None:
            raise RecoverySealProtocolError("attempt_timestamp_invalid")
        return (
            started_at.astimezone(UTC),
            _batch_key(cast(str, item[0]["batch_id"])),
        )

    attempt, receipt = max(attempts, key=attempt_sort_key)
    latest = {
        "batch_id": attempt["batch_id"],
        "seal_owner": attempt["seal_owner"],
        "started_at": attempt["started_at"],
        "outcome": (
            receipt["outcome"] if receipt is not None else "recovery_unsealed"
        ),
        "reason_code": (
            receipt["reason_code"] if receipt is not None else "seal_attempt_incomplete"
        ),
        "completed_at": receipt["completed_at"] if receipt is not None else None,
        "receipt_sha256": (
            receipt["receipt_sha256"] if receipt is not None else None
        ),
        "anchor_sha256": receipt["anchor_sha256"] if receipt is not None else None,
    }
    result = {**result, "latest": latest}
    if receipt is None or receipt["outcome"] == "recovery_unsealed":
        return {
            **result,
            "state": "recovery_unsealed",
            "errors": [cast(str, latest["reason_code"])],
        }

    anchor = current_anchor
    if anchor is None:
        anchor = _best_effort_anchor(
            db_path,
            expected_schema_version=expected_schema_version,
        )
    try:
        fingerprint_schema = recovery_source_fingerprint_schema(
            db_path,
            cast(str, receipt["source_fingerprint_sha256"]),
            expected_schema_version=expected_schema_version,
        )
    except BaseException:
        fingerprint_schema = None
    readiness_errors: list[str] = []
    if anchor is None or anchor.get("ready") is not True:
        readiness_errors.append("current_recovery_anchor_not_ready")
    if anchor is None or anchor.get("sha256") != receipt["anchor_sha256"]:
        readiness_errors.append("current_anchor_receipt_mismatch")
    if fingerprint_schema is None:
        readiness_errors.append("source_changed_since_recovery_seal")
    if readiness_errors:
        return {
            **result,
            "state": "stale",
            "errors": sorted(readiness_errors),
        }
    return {
        **result,
        "state": "verified",
        "ready": True,
        "errors": [],
    }
