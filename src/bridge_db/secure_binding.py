"""No-secret-output principal enrollment and client binding.

The secret is accepted only on an explicitly supplied descriptor greater than
stderr.  It is never accepted in argv or an environment variable and is never
included in the result, logs, exceptions, or receipts.  Registry and binding
updates are staged privately, replace atomically per file, and fail closed if
the pair cannot be read back as one current grant.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import tempfile
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any, cast

from bridge_db import clock
from bridge_db.auth import (
    GRANT_TTL_DAYS,
    PRINCIPALS_VERSION,
    hash_token,
    load_principal_grants,
    scopes_for_caller,
)
from bridge_db.models import CALLER_IDS

_MAX_SECRET_BYTES = 4096


class SecureBindingError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _guard_target(path: Path, *, allow_missing: bool) -> Path:
    if not path.is_absolute() or path in (Path("/"), Path.home()):
        raise SecureBindingError("binding.target_invalid")
    parent = path.parent
    if not parent.exists() or not parent.is_dir() or parent.is_symlink():
        raise SecureBindingError("binding.parent_invalid")
    if parent.resolve(strict=True) != parent:
        raise SecureBindingError("binding.parent_symlink_refused")
    parent_stat = parent.stat()
    if parent_stat.st_uid != os.getuid() or stat.S_IMODE(parent_stat.st_mode) & 0o022:
        raise SecureBindingError("binding.parent_not_private")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise SecureBindingError("binding.target_symlink_or_special_refused")
        target_stat = path.stat()
        if target_stat.st_uid != os.getuid():
            raise SecureBindingError("binding.target_owner_mismatch")
        if stat.S_IMODE(target_stat.st_mode) != 0o600:
            raise SecureBindingError("binding.target_mode_invalid")
    elif not allow_missing:
        raise SecureBindingError("binding.target_missing")
    return path


def _read_secret_fd(secret_fd: int) -> str:
    if secret_fd < 3:
        raise SecureBindingError("binding.secret_fd_stdio_refused")
    try:
        descriptor_stat = os.fstat(secret_fd)
    except OSError as exc:
        raise SecureBindingError("binding.secret_fd_invalid") from exc
    if not (
        stat.S_ISREG(descriptor_stat.st_mode)
        or stat.S_ISFIFO(descriptor_stat.st_mode)
        or stat.S_ISSOCK(descriptor_stat.st_mode)
    ):
        raise SecureBindingError("binding.secret_fd_type_refused")
    if stat.S_ISREG(descriptor_stat.st_mode):
        if descriptor_stat.st_uid != os.getuid():
            raise SecureBindingError("binding.secret_fd_owner_mismatch")
        if stat.S_IMODE(descriptor_stat.st_mode) & 0o077:
            raise SecureBindingError("binding.secret_fd_mode_invalid")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(secret_fd, min(1024, _MAX_SECRET_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_SECRET_BYTES:
            raise SecureBindingError("binding.secret_too_large")
    raw = b"".join(chunks).rstrip(b"\r\n")
    if len(raw) < 32 or b"\x00" in raw or b"\n" in raw or b"\r" in raw:
        raise SecureBindingError("binding.secret_invalid")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecureBindingError("binding.secret_invalid") from exc


def _read_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": PRINCIPALS_VERSION, "principals": {}}
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise SecureBindingError("binding.registry_invalid") from exc
    if not isinstance(raw, dict):
        raise SecureBindingError("binding.registry_invalid")
    data = {str(key): value for key, value in cast(dict[object, Any], raw).items()}
    if data.get("version") != PRINCIPALS_VERSION or not isinstance(
        data.get("principals"), dict
    ):
        raise SecureBindingError("binding.registry_version_unsupported")
    return data


def _stage_private(path: Path, content: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.pending-")
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _restore_exact(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
        return
    temporary = _stage_private(path, previous)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def bind_principal_from_fd(
    *,
    caller: str,
    secret_fd: int,
    principals_path: Path,
    binding_path: Path,
    auth_mode: str = "warn",
) -> dict[str, Any]:
    """Rotate one grant and bind its same secret without returning secret material."""
    if caller not in CALLER_IDS:
        raise SecureBindingError("binding.caller_invalid")
    if caller != "codex":
        raise SecureBindingError("binding.caller_not_authorized_for_local_binding")
    if auth_mode not in ("warn", "enforce"):
        raise SecureBindingError("binding.auth_mode_invalid")
    principals_path = _guard_target(principals_path, allow_missing=True)
    binding_path = _guard_target(binding_path, allow_missing=True)
    if principals_path == binding_path:
        raise SecureBindingError("binding.targets_collide")
    secret = _read_secret_fd(secret_fd)

    lock_path = principals_path.parent / ".bridge-db-secure-binding.lock"
    lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    registry_temporary: Path | None = None
    binding_temporary: Path | None = None
    try:
        os.fchmod(lock_descriptor, 0o600)
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        # Revalidate after acquiring the lock so a target swap cannot race the
        # preflight checks.
        _guard_target(principals_path, allow_missing=True)
        _guard_target(binding_path, allow_missing=True)
        registry = _read_registry(principals_path)
        principals = cast(dict[str, Any], registry["principals"])
        valid_existing = load_principal_grants(principals_path)
        if principals and len(valid_existing) != len(principals):
            raise SecureBindingError("binding.registry_grant_invalid")
        old_entry = principals.get(caller)
        old_generation = (
            cast(dict[str, Any], old_entry).get("generation", 0)
            if isinstance(old_entry, dict)
            else 0
        )
        generation = old_generation + 1 if isinstance(old_generation, int) else 1
        if isinstance(old_generation, bool) or generation < 1:
            raise SecureBindingError("binding.registry_generation_invalid")
        issued_at = clock.now().astimezone(UTC)
        expires_at = issued_at + timedelta(days=GRANT_TTL_DAYS)
        principals[caller] = {
            "token_sha256": hash_token(secret),
            "issued_at": issued_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generation": generation,
            "scopes": scopes_for_caller(caller),
        }
        registry_bytes = (
            json.dumps(registry, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        binding_bytes = (
            f"BRIDGE_DB_PRINCIPAL_TOKEN={secret}\n"
            f"BRIDGE_DB_AUTH_MODE={auth_mode}\n"
        ).encode("utf-8")
        previous_registry = (
            principals_path.read_bytes() if principals_path.exists() else None
        )
        previous_binding = binding_path.read_bytes() if binding_path.exists() else None
        registry_temporary = _stage_private(principals_path, registry_bytes)
        binding_temporary = _stage_private(binding_path, binding_bytes)
        try:
            os.replace(registry_temporary, principals_path)
            registry_temporary = None
            _fsync_directory(principals_path.parent)
            os.replace(binding_temporary, binding_path)
            binding_temporary = None
            _fsync_directory(binding_path.parent)
        except Exception as exc:
            _restore_exact(principals_path, previous_registry)
            _restore_exact(binding_path, previous_binding)
            raise SecureBindingError("binding.atomic_replace_failed") from exc

        if stat.S_IMODE(principals_path.stat().st_mode) != 0o600 or stat.S_IMODE(
            binding_path.stat().st_mode
        ) != 0o600:
            _restore_exact(principals_path, previous_registry)
            _restore_exact(binding_path, previous_binding)
            raise SecureBindingError("binding.readback_mode_mismatch")
        grants = load_principal_grants(principals_path)
        grant = grants.get(hash_token(secret))
        binding_lines = binding_path.read_text(encoding="utf-8").splitlines()
        if (
            grant is None
            or grant.caller != caller
            or grant.generation != generation
            or binding_lines
            != [
                "BRIDGE_DB_PRINCIPAL_TOKEN=" + secret,
                "BRIDGE_DB_AUTH_MODE=" + auth_mode,
            ]
        ):
            _restore_exact(principals_path, previous_registry)
            _restore_exact(binding_path, previous_binding)
            raise SecureBindingError("binding.readback_identity_mismatch")
    finally:
        if registry_temporary is not None:
            registry_temporary.unlink(missing_ok=True)
        if binding_temporary is not None:
            binding_temporary.unlink(missing_ok=True)
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)

    return {
        "schema": "BridgeSecurePrincipalBindingReceiptV1",
        "ok": True,
        "caller": caller,
        "generation": generation,
        "auth_mode": auth_mode,
        "principals_path": str(principals_path),
        "binding_path": str(binding_path),
        "registry_readback": "verified",
        "binding_readback": "verified",
        "mode_readback": "0600",
        "secret_output": "none",
        "rollback": "in_memory_exact_restore_on_partial_replace",
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bridge_db.secure_binding")
    parser.add_argument("--caller", required=True)
    parser.add_argument("--secret-fd", type=int, required=True)
    parser.add_argument("--principals-path", type=Path, required=True)
    parser.add_argument("--binding-path", type=Path, required=True)
    parser.add_argument("--auth-mode", choices=("warn", "enforce"), default="warn")
    args = parser.parse_args()
    try:
        result = bind_principal_from_fd(
            caller=args.caller,
            secret_fd=args.secret_fd,
            principals_path=args.principals_path,
            binding_path=args.binding_path,
            auth_mode=args.auth_mode,
        )
    except SecureBindingError as exc:
        print(json.dumps({"ok": False, "reason_code": exc.reason_code}, sort_keys=True))
        raise SystemExit(1) from exc
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
