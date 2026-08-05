"""Exact, no-secret-output rebinding for the two Claude JSON MCP configs."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, cast

CLIENT_REBIND_SCHEMA = "BridgeClaudeLauncherRebindReceiptV1"
CLIENT_RESTORE_SCHEMA = "BridgeClaudeLauncherRestoreReceiptV1"
IMMUTABLE_LAUNCHER = Path(
    "/Users/d/.local/state/bridge-db/current/bin/bridge-db-mcp"
)
LEGACY_SOURCE_ROOT = Path("/Users/d/Projects/bridge-db")
_CLIENT_BASENAMES = {
    "claude-code": ".claude.json",
    "claude-desktop": "claude_desktop_config.json",
}
_MCP_SERVER_NAME = "bridge-db"


class ClientRebindingError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _guard_parent(path: Path) -> None:
    if not path.exists() or not path.is_dir() or path.is_symlink():
        raise ClientRebindingError("client_rebind.parent_invalid")
    if path.resolve(strict=True) != path:
        raise ClientRebindingError("client_rebind.parent_symlink_refused")
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise ClientRebindingError("client_rebind.parent_not_owned_safe")


def _guard_config(path: Path, *, client: str) -> Path:
    expected_basename = _CLIENT_BASENAMES.get(client)
    if expected_basename is None:
        raise ClientRebindingError("client_rebind.client_invalid")
    if (
        not path.is_absolute()
        or path in (Path("/"), Path.home())
        or path.name != expected_basename
    ):
        raise ClientRebindingError("client_rebind.target_invalid")
    _guard_parent(path.parent)
    if path.is_symlink() or not path.is_file():
        raise ClientRebindingError("client_rebind.target_missing_or_special")
    metadata = path.stat()
    if metadata.st_uid != os.getuid():
        raise ClientRebindingError("client_rebind.target_owner_mismatch")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ClientRebindingError("client_rebind.target_mode_invalid")
    return path


def _guard_backup_root(path: Path, *, create: bool) -> Path:
    if not path.is_absolute() or path in (Path("/"), Path.home()):
        raise ClientRebindingError("client_rebind.backup_root_invalid")
    _guard_parent(path.parent)
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ClientRebindingError("client_rebind.backup_root_invalid")
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ClientRebindingError("client_rebind.backup_root_not_private")
    return path


def _stage_private(path: Path, content: bytes, *, mode: int) -> Path:
    descriptor, name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.pending-"
    )
    temporary = Path(name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _replace_exact(path: Path, content: bytes, *, mode: int) -> None:
    temporary = _stage_private(path, content, mode=mode)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


@contextmanager
def _target_lock(path: Path) -> Generator[None, None, None]:
    lock_path = path.parent / ".bridge-db-client-rebind.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ClientRebindingError("client_rebind.lock_invalid") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ClientRebindingError("client_rebind.lock_invalid")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientRebindingError("client_rebind.json_invalid") from exc
    if not isinstance(raw, dict):
        raise ClientRebindingError("client_rebind.json_invalid")
    return {str(key): value for key, value in cast(dict[object, Any], raw).items()}


def _bridge_entry(document: dict[str, Any]) -> dict[str, Any]:
    servers = document.get("mcpServers")
    if not isinstance(servers, dict):
        raise ClientRebindingError("client_rebind.mcp_servers_invalid")
    entry = cast(dict[object, Any], servers).get(_MCP_SERVER_NAME)
    if not isinstance(entry, dict):
        raise ClientRebindingError("client_rebind.bridge_entry_invalid")
    return {str(key): value for key, value in cast(dict[object, Any], entry).items()}


def _validated_environment(entry: dict[str, Any]) -> dict[str, str]:
    environment = entry.get("env")
    if not isinstance(environment, dict):
        raise ClientRebindingError("client_rebind.environment_invalid")
    values = {str(key): value for key, value in cast(dict[object, Any], environment).items()}
    expected = {"BRIDGE_DB_AUTH_MODE", "BRIDGE_DB_PRINCIPAL_TOKEN"}
    if set(values) != expected or any(not isinstance(value, str) for value in values.values()):
        raise ClientRebindingError("client_rebind.environment_invalid")
    auth_mode = values["BRIDGE_DB_AUTH_MODE"]
    token = values["BRIDGE_DB_PRINCIPAL_TOKEN"]
    if auth_mode not in ("off", "warn", "enforce") or len(token.encode("utf-8")) < 32:
        raise ClientRebindingError("client_rebind.environment_invalid")
    return cast(dict[str, str], values)


def _legacy_args() -> list[str]:
    return [
        "run",
        "--directory",
        str(LEGACY_SOURCE_ROOT),
        "python",
        "-m",
        "bridge_db",
    ]


def _write_backup(
    *, backup_root: Path, client: str, original: bytes
) -> tuple[Path, str]:
    digest = _sha256_bytes(original)
    backup = backup_root / f"{client}-{digest}.json"
    if backup.exists() or backup.is_symlink():
        if (
            backup.is_symlink()
            or not backup.is_file()
            or backup.stat().st_uid != os.getuid()
            or stat.S_IMODE(backup.stat().st_mode) != 0o400
            or backup.read_bytes() != original
        ):
            raise ClientRebindingError("client_rebind.backup_collision")
        return backup, digest
    temporary = _stage_private(backup, original, mode=0o400)
    try:
        os.link(temporary, backup, follow_symlinks=False)
        temporary.unlink()
        _fsync_directory(backup_root)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return backup, digest


def rebind_claude_launcher(
    *,
    client: Literal["claude-code", "claude-desktop"],
    config_path: Path,
    backup_root: Path,
) -> dict[str, Any]:
    """Replace only the exact legacy bridge-db command/args, preserving env values."""
    config_path = _guard_config(config_path, client=client)
    if not backup_root.is_absolute() or backup_root in (Path("/"), Path.home()):
        raise ClientRebindingError("client_rebind.backup_root_invalid")
    _guard_parent(backup_root.parent)
    if backup_root.exists() or backup_root.is_symlink():
        _guard_backup_root(backup_root, create=False)
    with _target_lock(config_path):
        config_path = _guard_config(config_path, client=client)
        original = config_path.read_bytes()
        document = _read_json_object(config_path)
        entry = _bridge_entry(document)
        environment = _validated_environment(entry)
        if entry.get("command") != "uv" or entry.get("args") != _legacy_args():
            raise ClientRebindingError("client_rebind.legacy_launcher_mismatch")
        backup_root = _guard_backup_root(backup_root, create=True)
        backup_path, original_sha256 = _write_backup(
            backup_root=backup_root,
            client=client,
            original=original,
        )
        entry["command"] = str(IMMUTABLE_LAUNCHER)
        entry["args"] = []
        servers = cast(dict[str, Any], document["mcpServers"])
        servers[_MCP_SERVER_NAME] = entry
        encoded = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        )
        current_sha256 = _sha256_bytes(encoded)
        try:
            _replace_exact(config_path, encoded, mode=0o600)
            readback = _read_json_object(config_path)
            readback_entry = _bridge_entry(readback)
            readback_environment = _validated_environment(readback_entry)
            if (
                config_path.read_bytes() != encoded
                or readback_entry.get("command") != str(IMMUTABLE_LAUNCHER)
                or readback_entry.get("args") != []
                or readback_environment != environment
            ):
                raise ClientRebindingError("client_rebind.readback_mismatch")
        except Exception as exc:
            try:
                _replace_exact(config_path, original, mode=0o600)
            except Exception as rollback_exc:
                raise ClientRebindingError("client_rebind.rollback_failed") from rollback_exc
            if isinstance(exc, ClientRebindingError):
                raise
            raise ClientRebindingError("client_rebind.replace_failed") from exc

    return {
        "schema": CLIENT_REBIND_SCHEMA,
        "ok": True,
        "client": client,
        "config_path": str(config_path),
        "backup_path": str(backup_path),
        "backup_sha256": original_sha256,
        "previous_config_sha256": original_sha256,
        "current_config_sha256": current_sha256,
        "launcher": str(IMMUTABLE_LAUNCHER),
        "args": [],
        "environment_preservation": "exact_value_readback_verified",
        "environment_value_output": "none",
        "mode_readback": "0600",
        "rollback": "exact_backup_with_current_digest_compare_and_swap",
    }


def restore_claude_launcher(
    *,
    client: Literal["claude-code", "claude-desktop"],
    config_path: Path,
    backup_path: Path,
    expected_current_sha256: str,
) -> dict[str, Any]:
    """Restore one exact private backup only while the current digest still matches."""
    config_path = _guard_config(config_path, client=client)
    if len(expected_current_sha256) != 64:
        raise ClientRebindingError("client_restore.digest_invalid")
    backup_root = _guard_backup_root(backup_path.parent, create=False)
    if (
        backup_path.parent != backup_root
        or backup_path.is_symlink()
        or not backup_path.is_file()
        or backup_path.stat().st_uid != os.getuid()
        or stat.S_IMODE(backup_path.stat().st_mode) != 0o400
    ):
        raise ClientRebindingError("client_restore.backup_invalid")
    backup = backup_path.read_bytes()
    backup_sha256 = _sha256_bytes(backup)
    expected_name = f"{client}-{backup_sha256}.json"
    if backup_path.name != expected_name:
        raise ClientRebindingError("client_restore.backup_identity_mismatch")
    with _target_lock(config_path):
        config_path = _guard_config(config_path, client=client)
        current = config_path.read_bytes()
        if _sha256_bytes(current) != expected_current_sha256:
            raise ClientRebindingError("client_restore.current_digest_mismatch")
        current_entry = _bridge_entry(_read_json_object(config_path))
        _validated_environment(current_entry)
        if (
            current_entry.get("command") != str(IMMUTABLE_LAUNCHER)
            or current_entry.get("args") != []
        ):
            raise ClientRebindingError("client_restore.current_launcher_mismatch")
        backup_entry = _bridge_entry(_read_json_object(backup_path))
        _validated_environment(backup_entry)
        if (
            backup_entry.get("command") != "uv"
            or backup_entry.get("args") != _legacy_args()
        ):
            raise ClientRebindingError("client_restore.backup_launcher_mismatch")
        try:
            _replace_exact(config_path, backup, mode=0o600)
            if config_path.read_bytes() != backup:
                raise ClientRebindingError("client_restore.readback_mismatch")
        except Exception as exc:
            try:
                _replace_exact(config_path, current, mode=0o600)
            except Exception as rollback_exc:
                raise ClientRebindingError("client_restore.rollback_failed") from rollback_exc
            if isinstance(exc, ClientRebindingError):
                raise
            raise ClientRebindingError("client_restore.replace_failed") from exc
    return {
        "schema": CLIENT_RESTORE_SCHEMA,
        "ok": True,
        "client": client,
        "config_path": str(config_path),
        "backup_path": str(backup_path),
        "restored_config_sha256": backup_sha256,
        "environment_value_output": "none",
        "mode_readback": "0600",
        "outcome": "exact_backup_restored",
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bridge_db.client_rebinding")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    rebind = subparsers.add_parser("rebind")
    rebind.add_argument("--client", choices=tuple(_CLIENT_BASENAMES), required=True)
    rebind.add_argument("--config-path", type=Path, required=True)
    rebind.add_argument("--backup-root", type=Path, required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--client", choices=tuple(_CLIENT_BASENAMES), required=True)
    restore.add_argument("--config-path", type=Path, required=True)
    restore.add_argument("--backup-path", type=Path, required=True)
    restore.add_argument("--expected-current-sha256", required=True)
    args = parser.parse_args()
    try:
        if args.operation == "rebind":
            result = rebind_claude_launcher(
                client=args.client,
                config_path=args.config_path,
                backup_root=args.backup_root,
            )
        else:
            result = restore_claude_launcher(
                client=args.client,
                config_path=args.config_path,
                backup_path=args.backup_path,
                expected_current_sha256=args.expected_current_sha256,
            )
    except ClientRebindingError as exc:
        print(json.dumps({"ok": False, "reason_code": exc.reason_code}, sort_keys=True))
        raise SystemExit(1) from exc
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
