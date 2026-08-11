"""Private shared-runtime lifecycle for BridgeDB MCP clients.

The normal client contract remains stdio.  A small shell relay can opt into one
generation- and credential-bound Streamable HTTP broker over a private Unix
domain socket.  This module owns only broker/client leases and cooperative idle
shutdown; it never signals another process.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import http.client
import json
import os
import re
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from bridge_db import clock, config
from bridge_db.tenancy import probe_process, process_identity, tenancy_root

CLIENT_SCHEMA = "BridgeSharedRuntimeClientLeaseV1"
BROKER_SCHEMA = "BridgeSharedRuntimeBrokerReceiptV1"
INVENTORY_SCHEMA = "BridgeSharedRuntimeInventoryV1"
READINESS_SCHEMA = "BridgeSharedRuntimeReadinessV1"
LAUNCH_CONTRACT_SCHEMA = "BridgeSharedRuntimeLaunchContractV1"
_KEY_RE = re.compile(r"^[0-9a-f]{16}-[0-9a-f]{12}$")
_LEASE_RE = re.compile(r"^[0-9a-f]{24}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CAPABILITY_RE = re.compile(r"^([0-9a-f]{24})\.([0-9a-f]{64})$")
_CAPABILITY_HEADER = b"x-bridge-relay-capability"
_CAPABILITY_TTL_DEFAULT_SECONDS = 30.0


class SharedRuntimeContractError(RuntimeError):
    """Fail-closed shared-runtime error with a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class SharedRuntimePaths:
    root: Path
    group: Path
    socket: Path
    clients: Path
    capabilities: Path
    history: Path
    broker_receipt: Path
    broker_log: Path


@dataclass(frozen=True)
class SharedRuntimeBinding:
    socket: Path
    client_lease: Path
    capability_file: Path
    release_launcher: Path


def _utc_text() -> str:
    return clock.now().isoformat().replace("+00:00", "Z")


def _utc_text_at(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_utc_text(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _absolute_path(path: Path) -> str:
    return os.path.abspath(os.fspath(path))


def _launch_contract() -> dict[str, object]:
    """Return the effective non-secret broker launch contract.

    Every value here can affect data, authorization, audit evidence, runtime
    ownership, or broker lifetime. The principal token is deliberately absent;
    it is bound separately through the private selector HMAC.
    """
    from bridge_db.auth import auth_mode

    manifest = os.environ.get("BRIDGE_DB_GENERATION_MANIFEST")
    launcher_override = os.environ.get("BRIDGE_DB_SHARED_RUNTIME_LAUNCHER")
    runtime_source = (
        Path(manifest).parent / "bin" / "bridge-db-mcp"
        if manifest
        else Path(launcher_override)
        if launcher_override
        else Path(__file__)
    )
    return {
        "schema": LAUNCH_CONTRACT_SCHEMA,
        "auth_mode": auth_mode(),
        "generation": os.environ.get("BRIDGE_DB_GENERATION_ID", "mutable"),
        "generation_manifest": _absolute_path(Path(manifest)) if manifest else None,
        "runtime_source": _absolute_path(runtime_source),
        "python_executable": _absolute_path(Path(sys.executable)),
        "db_path": _absolute_path(config.DB_PATH),
        "bridge_file_path": _absolute_path(config.BRIDGE_FILE_PATH),
        "principals_path": _absolute_path(config.PRINCIPALS_PATH),
        "audit_log_path": _absolute_path(config.AUDIT_LOG_PATH),
        "audit_failure_log_path": _absolute_path(config.AUDIT_FAILURE_LOG_PATH),
        "evidence_ack_log_path": _absolute_path(config.EVIDENCE_ACK_LOG_PATH),
        "evidence_disposition_log_path": _absolute_path(
            config.EVIDENCE_DISPOSITION_LOG_PATH
        ),
        "project_registry_path": _absolute_path(config.PROJECT_REGISTRY_PATH),
        "meta_shipped_events_path": _absolute_path(config.META_SHIPPED_EVENTS_PATH),
        "tenancy_root": _absolute_path(tenancy_root()),
        "client_owner": os.environ.get("BRIDGE_DB_CLIENT_OWNER", "").strip().lower(),
        "allow_empty_bridge_export": os.environ.get(
            "BRIDGE_DB_ALLOW_EMPTY_BRIDGE_EXPORT", ""
        ),
        "log_level": config.LOG_LEVEL,
        "audit_log_rotate_bytes": config.AUDIT_LOG_ROTATE_BYTES,
        "recall_log_rotate_bytes": config.RECALL_LOG_ROTATE_BYTES,
        "broker_idle_seconds": _idle_seconds(),
        "relay_capability_ttl_seconds": _capability_ttl_seconds(),
        "transport": "streamable_http_over_private_unix_socket",
    }


def _launch_contract_bytes() -> bytes:
    return json.dumps(
        _launch_contract(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _launch_contract_sha256() -> str:
    return hashlib.sha256(_launch_contract_bytes()).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_private_lock(path: Path) -> int:
    if path.is_symlink():
        raise SharedRuntimeContractError("shared_runtime.lock_invalid")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise SharedRuntimeContractError("shared_runtime.lock_invalid") from exc
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise SharedRuntimeContractError("shared_runtime.lock_not_private")
    return descriptor


@contextmanager
def _group_lifecycle_lock(
    paths: SharedRuntimePaths, *, nonblocking: bool = False
) -> Generator[None, None, None]:
    descriptor = _open_private_lock(paths.group / "ensure.lock")
    try:
        operation = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        fcntl.flock(descriptor, operation)
        yield
    finally:
        os.close(descriptor)


def _guard_private_directory(path: Path, *, create: bool) -> Path:
    if not path.is_absolute() or path in (Path("/"), Path.home()):
        raise SharedRuntimeContractError("shared_runtime.path_invalid")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists() or not path.is_dir() or path.is_symlink():
        raise SharedRuntimeContractError("shared_runtime.directory_invalid")
    if path.resolve(strict=True) != path:
        raise SharedRuntimeContractError("shared_runtime.directory_symlink_refused")
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SharedRuntimeContractError("shared_runtime.directory_not_private")
    return path


def _atomic_json(path: Path, payload: dict[str, Any], *, replace: bool) -> None:
    encoded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.pending-"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_secret(path: Path, value: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.pending-"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o400)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SharedRuntimeContractError("shared_runtime.record_invalid")
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SharedRuntimeContractError("shared_runtime.record_not_private")
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise SharedRuntimeContractError("shared_runtime.record_invalid") from exc
    if not isinstance(raw, dict):
        raise SharedRuntimeContractError("shared_runtime.record_invalid")
    return {str(key): value for key, value in cast(dict[object, Any], raw).items()}


def _required_principal_token() -> str:
    raw_token = os.environ.get("BRIDGE_DB_PRINCIPAL_TOKEN")
    token = raw_token.strip() if raw_token is not None else ""
    if len(token) < 32:
        raise SharedRuntimeContractError("shared_runtime.credential_invalid")
    return token


def _selector_secret(root: Path, *, create: bool = True) -> bytes:
    path = root / "selector.key"
    if not path.exists():
        if not create:
            raise SharedRuntimeContractError("shared_runtime.selector_missing")
        descriptor, temporary_name = tempfile.mkstemp(
            dir=root, prefix=".selector.key.pending-"
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o400)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(os.urandom(32))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path, follow_symlinks=False)
                _fsync_directory(root)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    if path.is_symlink() or not path.is_file():
        raise SharedRuntimeContractError("shared_runtime.selector_invalid")
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SharedRuntimeContractError("shared_runtime.selector_not_private")
    value = path.read_bytes()
    if len(value) != 32:
        raise SharedRuntimeContractError("shared_runtime.selector_invalid")
    return value


def _credential_key(root: Path, *, create_selector: bool = True) -> str:
    token = _required_principal_token()
    contract = _launch_contract_bytes()
    selector = hmac.new(
        _selector_secret(root, create=create_selector),
        token.encode("utf-8") + b"\0" + contract,
        hashlib.sha256,
    ).hexdigest()[:16]
    return f"{selector}-{hashlib.sha256(contract).hexdigest()[:12]}"


def _shared_runtime_root_path() -> Path:
    return Path(
        os.environ.get(
            "BRIDGE_DB_SHARED_RUNTIME_ROOT",
            str(config.DB_PATH.parent / "shared-runtime"),
        )
    )


def _shared_runtime_root() -> Path:
    return _guard_private_directory(_shared_runtime_root_path(), create=True)


def _paths_for_group(
    root: Path, group: Path, *, create: bool = True
) -> SharedRuntimePaths:
    group = _guard_private_directory(group, create=create)
    clients = _guard_private_directory(group / "clients", create=create)
    capabilities = _guard_private_directory(group / "capabilities", create=create)
    history = _guard_private_directory(group / "history", create=create)
    return SharedRuntimePaths(
        root=root,
        group=group,
        socket=group / "bridge.sock",
        clients=clients,
        capabilities=capabilities,
        history=history,
        broker_receipt=group / "broker.json",
        broker_log=group / "broker.log",
    )


def _broker_auth_key(root: Path) -> bytes:
    return hmac.new(
        _selector_secret(root),
        (
            b"bridge-shared-runtime-broker-v1\0"
            + _required_principal_token().encode("utf-8")
            + b"\0"
            + _launch_contract_bytes()
        ),
        hashlib.sha256,
    ).digest()


def _socket_identity(path: Path) -> dict[str, int]:
    if not path.exists() or path.is_symlink():
        raise SharedRuntimeContractError("shared_runtime.socket_target_invalid")
    metadata = path.stat()
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise SharedRuntimeContractError("shared_runtime.socket_target_invalid")
    return {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
    }


def _validate_socket_identity_value(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        raise SharedRuntimeContractError("shared_runtime.broker_receipt_invalid")
    raw = cast(dict[object, object], value)
    identity: dict[str, int] = {}
    for key in ("device", "inode", "mode", "uid"):
        item = raw.get(key)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise SharedRuntimeContractError("shared_runtime.broker_receipt_invalid")
        identity[key] = item
    if identity["uid"] != os.getuid():
        raise SharedRuntimeContractError("shared_runtime.broker_receipt_invalid")
    return identity


def _connected_peer_pid(handle: socket.socket) -> int:
    """Return the process id for the connected Unix socket peer."""
    so_peercred = getattr(socket, "SO_PEERCRED", None)
    if isinstance(so_peercred, int):
        try:
            data = handle.getsockopt(
                socket.SOL_SOCKET,
                so_peercred,
                struct.calcsize("3i"),
            )
            pid, uid, _gid = struct.unpack("3i", data)
        except (OSError, struct.error) as exc:
            raise SharedRuntimeContractError(
                "shared_runtime.socket_peer_identity_unavailable"
            ) from exc
        if pid < 1 or uid != os.getuid():
            raise SharedRuntimeContractError(
                "shared_runtime.socket_peer_identity_mismatch"
            )
        return pid

    if sys.platform == "darwin":
        # Python does not expose LOCAL_PEERPID on this macOS build, but the
        # Darwin socket option is stable and returns the peer process id.
        sol_local = getattr(socket, "SOL_LOCAL", 0)
        local_peerpid = getattr(socket, "LOCAL_PEERPID", 2)
        try:
            data = handle.getsockopt(
                sol_local,
                local_peerpid,
                struct.calcsize("i"),
            )
            (pid,) = struct.unpack("i", data[: struct.calcsize("i")])
        except (OSError, struct.error) as exc:
            raise SharedRuntimeContractError(
                "shared_runtime.socket_peer_identity_unavailable"
            ) from exc
        if pid < 1:
            raise SharedRuntimeContractError(
                "shared_runtime.socket_peer_identity_mismatch"
            )
        return pid

    raise SharedRuntimeContractError(
        "shared_runtime.socket_peer_identity_unavailable"
    )


def _broker_receipt_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key != "receipt_hmac_sha256"
    }


def _broker_receipt_hmac(paths: SharedRuntimePaths, record: dict[str, Any]) -> str:
    payload = json.dumps(
        _broker_receipt_payload(record),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_broker_auth_key(paths.root), payload, hashlib.sha256).hexdigest()


def _broker_receipt_record(
    paths: SharedRuntimePaths,
    *,
    pid: int,
    process_identity_value: str,
    state: str = "ready",
    started_at: str | None = None,
) -> dict[str, Any]:
    launch_contract = _launch_contract()
    record: dict[str, Any] = {
        "schema": BROKER_SCHEMA,
        "state": state,
        "group_id": paths.group.name,
        "launch_contract_sha256": _launch_contract_sha256(),
        "pid": pid,
        "process_identity": process_identity_value,
        "socket": str(paths.socket),
        "socket_identity": _socket_identity(paths.socket),
        "broker_nonce": os.urandom(16).hex(),
        "started_at": started_at or _utc_text(),
        "generation": launch_contract["generation"],
        "auth_mode": launch_contract["auth_mode"],
        "transport": "streamable_http_over_private_unix_socket",
    }
    record["receipt_hmac_sha256"] = _broker_receipt_hmac(paths, record)
    return record


def _rehash_broker_receipt(
    paths: SharedRuntimePaths, record: dict[str, Any]
) -> dict[str, Any]:
    updated = dict(record)
    updated["receipt_hmac_sha256"] = _broker_receipt_hmac(paths, updated)
    return updated


def shared_runtime_paths() -> SharedRuntimePaths:
    root = _shared_runtime_root()
    key = _credential_key(root)
    if not _KEY_RE.fullmatch(key):  # pragma: no cover - construction invariant
        raise SharedRuntimeContractError("shared_runtime.key_invalid")
    return _paths_for_group(root, root / key)


def shared_runtime_current_readiness() -> dict[str, Any]:
    """Read exact current-broker readiness without exposing its selector."""
    readiness: dict[str, Any] = {
        "schema": READINESS_SCHEMA,
        "state": "missing",
        "ready": False,
        "adoption_state": "inactive",
        "receipt_state": "missing",
        "broker_process_state": "missing",
        "socket_reachable": False,
    }
    root = _shared_runtime_root_path()
    if not root.exists():
        readiness["reason_code"] = "shared_runtime.root_missing"
        return readiness
    try:
        root = _guard_private_directory(root, create=False)
        key = _credential_key(root, create_selector=False)
        group = root / key
        if not group.exists():
            readiness["reason_code"] = "shared_runtime.current_group_missing"
            return readiness
        paths = _paths_for_group(root, group, create=False)
        if not paths.broker_receipt.exists():
            readiness["reason_code"] = (
                "shared_runtime.current_broker_receipt_missing"
            )
            return readiness
        record = _read_json(paths.broker_receipt)
        _validate_broker_record(paths, record)
        pid = int(record["pid"])
        identity = str(record["process_identity"])
        process_state = probe_process(pid, identity)
        socket_reachable = _probe_broker_socket(paths, record)
        receipt_state = str(record["state"])
        readiness.update(
            {
                "state": "observed",
                "adoption_state": (
                    "active"
                    if receipt_state == "ready"
                    and process_state == "same"
                    and socket_reachable
                    and pid == os.getpid()
                    else "draining"
                    if receipt_state == "draining"
                    and process_state == "same"
                    and socket_reachable
                    and pid == os.getpid()
                    else "unknown"
                    if process_state == "unknown"
                    else "inactive"
                ),
                "receipt_state": receipt_state,
                "broker_process_state": process_state,
                "socket_reachable": socket_reachable,
            }
        )
        readiness["ready"] = readiness["adoption_state"] == "active"
        if not readiness["ready"]:
            readiness["reason_code"] = (
                "shared_runtime.current_broker_pid_mismatch"
                if pid != os.getpid()
                else "shared_runtime.current_broker_not_ready"
            )
        return readiness
    except (OSError, SharedRuntimeContractError) as exc:
        readiness.update(
            {
                "state": "unverified",
                "adoption_state": "unknown",
                "reason_code": (
                    exc.reason_code
                    if isinstance(exc, SharedRuntimeContractError)
                    else "shared_runtime.current_readiness_failed"
                ),
            }
        )
        return readiness


def _empty_shared_runtime_inventory(selected: Path, *, state: str) -> dict[str, Any]:
    return {
        "schema": INVENTORY_SCHEMA,
        "state": state,
        "root": str(selected),
        "adoption_state": "inactive" if state in ("missing", "observed") else "unknown",
        "group_count": 0,
        "broker_receipt_count": 0,
        "ready_broker_count": 0,
        "live_broker_count": 0,
        "broker_process_states": {
            "same": 0,
            "missing": 0,
            "mismatch": 0,
            "unknown": 0,
        },
        "socket_count": 0,
        "reachable_socket_count": 0,
        "orphan_socket_count": 0,
        "client_lease_count": 0,
        "capability_file_count": 0,
        "orphan_capability_file_count": 0,
        "pending_capability_count": 0,
        "expired_capability_count": 0,
        "consumed_capability_count": 0,
        "live_client_count": 0,
        "stale_client_count": 0,
        "unknown_client_count": 0,
        "client_process_states": {
            "same": 0,
            "missing": 0,
            "mismatch": 0,
            "unknown": 0,
        },
        "generations": {},
        "auth_modes": {},
    }


def shared_runtime_inventory(root: Path | None = None) -> dict[str, Any]:
    """Read one bounded no-secret inventory without reconciling runtime state."""
    selected = root or _shared_runtime_root_path()
    inventory = _empty_shared_runtime_inventory(selected, state="observed")
    if not selected.exists():
        inventory["state"] = "missing"
        return inventory
    try:
        selected = _guard_private_directory(selected, create=False)
        selector = selected / "selector.key"
        if selector.is_symlink() or not selector.is_file():
            raise SharedRuntimeContractError("shared_runtime.selector_invalid")
        selector_metadata = selector.stat()
        if (
            selector_metadata.st_uid != os.getuid()
            or stat.S_IMODE(selector_metadata.st_mode) & 0o077
            or selector_metadata.st_size != 32
        ):
            raise SharedRuntimeContractError("shared_runtime.selector_not_private")

        groups: list[Path] = []
        for child in sorted(selected.iterdir()):
            if child.name == "selector.key":
                continue
            if not _KEY_RE.fullmatch(child.name):
                raise SharedRuntimeContractError(
                    "shared_runtime.inventory_child_invalid"
                )
            groups.append(_guard_private_directory(child, create=False))

        inventory["root"] = str(selected)
        inventory["group_count"] = len(groups)
        broker_states = cast(dict[str, int], inventory["broker_process_states"])
        client_states = cast(dict[str, int], inventory["client_process_states"])
        generations = cast(dict[str, int], inventory["generations"])
        auth_modes = cast(dict[str, int], inventory["auth_modes"])

        for group in groups:
            group_paths = _paths_for_group(selected, group, create=False)
            clients = group_paths.clients
            capabilities = _guard_private_directory(
                group / "capabilities", create=False
            )
            _guard_private_directory(group / "history", create=False)
            broker_path = group_paths.broker_receipt
            socket_path = group_paths.socket
            socket_exists = socket_path.exists()
            if socket_exists:
                _socket_identity(socket_path)
                inventory["socket_count"] += 1

            if broker_path.exists():
                record = _read_json(broker_path)
                _validate_broker_record(
                    group_paths,
                    record,
                    verify_receipt_hmac=False,
                    verify_launch_contract=False,
                )
                contract_sha256 = record.get("launch_contract_sha256")
                generation = record.get("generation")
                mode = record.get("auth_mode")
                pid = record.get("pid")
                identity = record.get("process_identity")
                if (
                    record.get("schema") != BROKER_SCHEMA
                    or record.get("group_id") != group.name
                    or record.get("socket") != str(socket_path)
                    or record.get("state") not in ("ready", "draining")
                    or not isinstance(contract_sha256, str)
                    or not _SHA256_RE.fullmatch(contract_sha256)
                    or contract_sha256[:12] != group.name.rsplit("-", 1)[1]
                    or not isinstance(generation, str)
                    or not generation
                    or mode not in ("off", "warn", "enforce")
                    or not isinstance(pid, int)
                    or isinstance(pid, bool)
                    or pid < 1
                    or not isinstance(identity, str)
                    or not identity
                ):
                    raise SharedRuntimeContractError(
                        "shared_runtime.broker_receipt_invalid"
                    )
                inventory["broker_receipt_count"] += 1
                process_state = probe_process(pid, identity)
                broker_states[process_state] += 1
                reachable = socket_exists and _probe_broker_socket(
                    group_paths,
                    record,
                    verify_receipt_hmac=False,
                    verify_launch_contract=False,
                )
                if reachable:
                    inventory["reachable_socket_count"] += 1
                if process_state == "same" and reachable:
                    inventory["live_broker_count"] += 1
                    if record["state"] == "ready":
                        inventory["ready_broker_count"] += 1
                generations[generation] = generations.get(generation, 0) + 1
                auth_modes[mode] = auth_modes.get(mode, 0) + 1
            elif socket_exists:
                inventory["orphan_socket_count"] += 1

            lease_paths = sorted(clients.iterdir())
            for lease_path in lease_paths:
                if (
                    lease_path.is_symlink()
                    or not lease_path.is_file()
                    or not _LEASE_RE.fullmatch(lease_path.stem)
                    or lease_path.suffix != ".json"
                ):
                    raise SharedRuntimeContractError(
                        "shared_runtime.client_lease_invalid"
                    )
                record = _read_json(lease_path)
                _validate_client_record(lease_path, record)
                capability_path = _capability_path_for_lease(lease_path)
                if capability_path.exists() or capability_path.is_symlink():
                    capability_value = _read_capability_value(
                        capability_path, expected_lease_id=lease_path.stem
                    )
                    capability_secret = capability_value.decode("ascii").split(".", 1)[
                        1
                    ]
                    if not hmac.compare_digest(
                        str(record["capability_sha256"]), _sha256(capability_secret)
                    ):
                        raise SharedRuntimeContractError(
                            "shared_runtime.capability_file_invalid"
                        )
                    inventory["capability_file_count"] += 1
                expires_at = _parse_utc_text(record.get("capability_expires_at"))
                if record.get("capability_consumed_at") is not None:
                    inventory["consumed_capability_count"] += 1
                elif expires_at is None:
                    pass
                elif clock.now() >= expires_at:
                    inventory["expired_capability_count"] += 1
                else:
                    inventory["pending_capability_count"] += 1
                state = probe_process(
                    int(record["pid"]), str(record["process_identity"])
                )
                client_states[state] += 1
                inventory["client_lease_count"] += 1

            expected_capabilities = {
                f"{lease_path.stem}.header" for lease_path in lease_paths
            }
            observed_capabilities: set[str] = set()
            for capability_path in sorted(capabilities.iterdir()):
                if (
                    capability_path.suffix != ".header"
                    or not _LEASE_RE.fullmatch(capability_path.stem)
                ):
                    raise SharedRuntimeContractError(
                        "shared_runtime.capability_file_invalid"
                    )
                _read_capability_value(
                    capability_path, expected_lease_id=capability_path.stem
                )
                observed_capabilities.add(capability_path.name)
            inventory["orphan_capability_file_count"] += len(
                observed_capabilities - expected_capabilities
            )

        inventory["live_client_count"] = client_states["same"]
        inventory["stale_client_count"] = (
            client_states["missing"] + client_states["mismatch"]
        )
        inventory["unknown_client_count"] = client_states["unknown"]
        if inventory["ready_broker_count"] > 0:
            inventory["adoption_state"] = "active"
        elif inventory["live_broker_count"] > 0:
            inventory["adoption_state"] = "draining"
        elif broker_states["unknown"] > 0 or client_states["unknown"] > 0:
            inventory["adoption_state"] = "unknown"
        return inventory
    except (OSError, SharedRuntimeContractError) as exc:
        failed = _empty_shared_runtime_inventory(selected, state="unverified")
        failed["reason_code"] = (
            exc.reason_code
            if isinstance(exc, SharedRuntimeContractError)
            else "shared_runtime.inventory_read_failed"
        )
        return failed


def _capability_path_for_lease(path: Path) -> Path:
    return path.parent.parent / "capabilities" / f"{path.stem}.header"


def _read_capability_value(path: Path, *, expected_lease_id: str) -> bytes:
    """Read one exact legacy private capability header and return only its value."""
    if path.is_symlink():
        raise SharedRuntimeContractError("shared_runtime.capability_file_invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SharedRuntimeContractError(
            "shared_runtime.capability_file_invalid"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o400
        ):
            raise SharedRuntimeContractError(
                "shared_runtime.capability_file_not_private"
            )
        if metadata.st_size > 256:
            raise SharedRuntimeContractError(
                "shared_runtime.capability_file_invalid"
            )
        raw = os.read(descriptor, 257)
        if len(raw) != metadata.st_size:
            raise SharedRuntimeContractError(
                "shared_runtime.capability_file_invalid"
            )
    finally:
        os.close(descriptor)
    prefix = b"X-Bridge-Relay-Capability: "
    if not raw.startswith(prefix) or not raw.endswith(b"\n"):
        raise SharedRuntimeContractError("shared_runtime.capability_file_invalid")
    value = raw[len(prefix) : -1]
    try:
        decoded = value.decode("ascii")
    except UnicodeDecodeError as exc:
        raise SharedRuntimeContractError(
            "shared_runtime.capability_file_invalid"
        ) from exc
    match = _CAPABILITY_RE.fullmatch(decoded)
    if match is None or match.group(1) != expected_lease_id:
        raise SharedRuntimeContractError("shared_runtime.capability_file_invalid")
    return value


def _validate_client_record(
    path: Path,
    record: dict[str, Any],
    *,
    expected_contract_sha256: str | None = None,
) -> None:
    lease_id = record.get("lease_id")
    launch_contract_sha256 = record.get("launch_contract_sha256")
    capability_sha256 = record.get("capability_sha256")
    generation = record.get("generation")
    if (
        record.get("schema") != CLIENT_SCHEMA
        or not isinstance(lease_id, str)
        or not _LEASE_RE.fullmatch(lease_id)
        or path.stem != lease_id
        or record.get("group_id") != path.parent.parent.name
        or not isinstance(launch_contract_sha256, str)
        or not _SHA256_RE.fullmatch(launch_contract_sha256)
        or launch_contract_sha256[:12] != path.parent.parent.name.rsplit("-", 1)[1]
        or not isinstance(capability_sha256, str)
        or not _SHA256_RE.fullmatch(capability_sha256)
        or record.get("capability_file") != str(_capability_path_for_lease(path))
        or not isinstance(generation, str)
        or not generation
        or not isinstance(record.get("capability_sequence", 0), int)
        or isinstance(record.get("capability_sequence", 0), bool)
        or int(record.get("capability_sequence", 0)) < 0
        or (
            expected_contract_sha256 is not None
            and (
                launch_contract_sha256 != expected_contract_sha256
                or generation != _launch_contract()["generation"]
            )
        )
    ):
        raise SharedRuntimeContractError("shared_runtime.client_lease_invalid")
    pid = record.get("pid")
    identity = record.get("process_identity")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid < 1
        or not isinstance(identity, str)
        or not identity
    ):
        raise SharedRuntimeContractError("shared_runtime.client_identity_invalid")
    for field in (
        "capability_issued_at",
        "capability_expires_at",
        "capability_consumed_at",
    ):
        timestamp = record.get(field)
        if timestamp is not None and _parse_utc_text(timestamp) is None:
            raise SharedRuntimeContractError("shared_runtime.client_lease_invalid")


def _retire_client(
    path: Path,
    record: dict[str, Any],
    *,
    reason: str,
    expected_contract_sha256: str | None = None,
) -> None:
    _validate_client_record(
        path, record, expected_contract_sha256=expected_contract_sha256
    )
    capability_path = _capability_path_for_lease(path)
    if capability_path.exists() or capability_path.is_symlink():
        if capability_path.is_symlink() or not capability_path.is_file():
            raise SharedRuntimeContractError("shared_runtime.capability_file_invalid")
        capability_metadata = capability_path.stat()
        if (
            capability_metadata.st_uid != os.getuid()
            or stat.S_IMODE(capability_metadata.st_mode) != 0o400
        ):
            raise SharedRuntimeContractError(
                "shared_runtime.capability_file_not_private"
            )
    closed_at = _utc_text()
    event = _sha256(f"{record['lease_id']}\0{reason}\0{closed_at}")[:12]
    history = path.parent.parent / "history" / f"{record['lease_id']}-{event}.json"
    _atomic_json(
        history,
        {**record, "closed_at": closed_at, "lifecycle_reason": reason},
        replace=False,
    )
    if capability_path.exists() or capability_path.is_symlink():
        capability_path.unlink()
        _fsync_directory(capability_path.parent)
    path.unlink()
    _fsync_directory(path.parent)


def _register_client(
    paths: SharedRuntimePaths, *, owner_pid: int | None = None
) -> tuple[Path, Path]:
    pid = owner_pid if owner_pid is not None else os.getppid()
    identity = process_identity(pid)
    if identity is None:
        raise SharedRuntimeContractError("shared_runtime.client_identity_unknown")
    nonce = os.urandom(16).hex()
    lease_id = _sha256(f"{pid}\0{identity}\0{nonce}")[:24]
    path = paths.clients / f"{lease_id}.json"
    capability_path = _capability_path_for_lease(path)
    _atomic_json(
        path,
        {
            "schema": CLIENT_SCHEMA,
            "lease_id": lease_id,
            "group_id": paths.group.name,
            "launch_contract_sha256": _launch_contract_sha256(),
            "capability_sha256": "0" * 64,
            "capability_file": str(capability_path),
            "capability_sequence": 0,
            "capability_issued_at": None,
            "capability_expires_at": None,
            "capability_consumed_at": None,
            "pid": pid,
            "process_identity": identity,
            "created_at": _utc_text(),
            "generation": _launch_contract()["generation"],
            "lifecycle_reason": "relay_registered",
        },
        replace=False,
    )
    return path, capability_path


def _capability_ttl_seconds() -> float:
    return _CAPABILITY_TTL_DEFAULT_SECONDS


def _lease_paths_from_client_path(path: Path) -> tuple[SharedRuntimePaths, Path]:
    root = _guard_private_directory(_shared_runtime_root_path(), create=False)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise SharedRuntimeContractError("shared_runtime.client_path_outside_group") from exc
    if (
        len(relative.parts) != 3
        or not _KEY_RE.fullmatch(relative.parts[0])
        or relative.parts[1] != "clients"
        or not _LEASE_RE.fullmatch(Path(relative.parts[2]).stem)
        or Path(relative.parts[2]).suffix != ".json"
    ):
        raise SharedRuntimeContractError("shared_runtime.client_path_invalid")
    paths = _paths_for_group(root, root / relative.parts[0], create=False)
    if path.parent != paths.clients:
        raise SharedRuntimeContractError("shared_runtime.client_path_invalid")
    return paths, path


def _require_owner_owns_client(
    record: dict[str, Any], *, owner_pid: int | None = None, mismatch_reason: str
) -> None:
    pid = owner_pid if owner_pid is not None else os.getppid()
    identity = process_identity(pid)
    if record.get("pid") != pid or record.get("process_identity") != identity:
        raise SharedRuntimeContractError(mismatch_reason)


def renew_shared_capability(path: Path, *, owner_pid: int | None = None) -> str:
    """Mint one short-lived request capability for the exact relay owner."""
    paths, path = _lease_paths_from_client_path(path)
    with _group_lifecycle_lock(paths):
        if not path.exists():
            raise SharedRuntimeContractError("shared_runtime.client_lease_missing")
        record = _read_json(path)
        contract_sha256 = _launch_contract_sha256()
        _validate_client_record(
            path, record, expected_contract_sha256=contract_sha256
        )
        _require_owner_owns_client(
            record,
            owner_pid=owner_pid,
            mismatch_reason="shared_runtime.capability_renew_identity_mismatch",
        )
        now = clock.now()
        expires_at = now + timedelta(seconds=_capability_ttl_seconds())
        lease_id = str(record["lease_id"])
        capability_secret = os.urandom(32).hex()
        sequence = int(record.get("capability_sequence", 0)) + 1
        _atomic_json(
            path,
            {
                **record,
                "capability_sha256": _sha256(capability_secret),
                "capability_sequence": sequence,
                "capability_issued_at": _utc_text_at(now),
                "capability_expires_at": _utc_text_at(expires_at),
                "capability_consumed_at": None,
            },
            replace=True,
        )
    return f"X-Bridge-Relay-Capability: {lease_id}.{capability_secret}"


def _live_client_count_unlocked(paths: SharedRuntimePaths) -> int:
    """Count/reconcile relay leases while the group lifecycle lock is held."""
    live = 0
    contract_sha256 = _launch_contract_sha256()
    for path in sorted(paths.clients.glob("*.json")):
        record = _read_json(path)
        _validate_client_record(
            path, record, expected_contract_sha256=contract_sha256
        )
        state = probe_process(int(record["pid"]), str(record["process_identity"]))
        if state == "same":
            live += 1
        elif state in ("missing", "mismatch"):
            _retire_client(
                path,
                record,
                reason=f"relay_{state}",
                expected_contract_sha256=contract_sha256,
            )
        else:
            # Unknown process state is not safe to reap and keeps the broker resident.
            live += 1
    return live


def _relay_capability_authorized(paths: SharedRuntimePaths, raw_value: bytes) -> bool:
    try:
        value = raw_value.decode("ascii")
    except UnicodeDecodeError:
        return False
    match = _CAPABILITY_RE.fullmatch(value)
    if match is None:
        return False
    lease_id, secret = match.groups()
    lease_path = paths.clients / f"{lease_id}.json"
    try:
        with _group_lifecycle_lock(paths):
            record = _read_json(lease_path)
            _validate_client_record(
                lease_path,
                record,
                expected_contract_sha256=_launch_contract_sha256(),
            )
            if (
                probe_process(int(record["pid"]), str(record["process_identity"]))
                != "same"
            ):
                return False
            expires_at = _parse_utc_text(record.get("capability_expires_at"))
            if (
                expires_at is None
                or clock.now() >= expires_at
                or record.get("capability_consumed_at") is not None
                or not hmac.compare_digest(
                    str(record["capability_sha256"]), _sha256(secret)
                )
            ):
                return False
            _atomic_json(
                lease_path,
                {**record, "capability_consumed_at": _utc_text()},
                replace=True,
            )
            return True
    except (OSError, SharedRuntimeContractError):
        return False


class _RelayCapabilityMiddleware:
    """Require and strip one relay-specific capability on every HTTP request."""

    def __init__(self, paths: SharedRuntimePaths, app: Any) -> None:
        self.paths = paths
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = list(cast(list[tuple[bytes, bytes]], scope.get("headers", [])))
        capabilities = [value for name, value in headers if name == _CAPABILITY_HEADER]
        if len(capabilities) != 1 or not _relay_capability_authorized(
            self.paths, capabilities[0]
        ):
            body = b'{"error":"shared_runtime.relay_capability_required"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        forwarded_scope = dict(scope)
        forwarded_scope["headers"] = [
            (name, value) for name, value in headers if name != _CAPABILITY_HEADER
        ]
        await self.app(forwarded_scope, receive, send)


def _probe_socket(path: Path) -> bool:
    if not path.exists() or path.is_symlink():
        return False
    try:
        if not stat.S_ISSOCK(path.stat().st_mode):
            raise SharedRuntimeContractError("shared_runtime.socket_target_invalid")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as handle:
            handle.settimeout(0.25)
            handle.connect(str(path))
        return True
    except (ConnectionRefusedError, FileNotFoundError, TimeoutError):
        return False
    except OSError:
        return False


def _connect_verified_broker_socket(
    paths: SharedRuntimePaths,
    record: dict[str, Any],
    *,
    timeout: float,
    allowed_states: tuple[str, ...] = ("ready",),
    verify_receipt_hmac: bool = True,
    verify_launch_contract: bool = True,
) -> socket.socket:
    _validate_broker_record(
        paths,
        record,
        allowed_states=allowed_states,
        verify_receipt_hmac=verify_receipt_hmac,
        verify_launch_contract=verify_launch_contract,
    )
    handle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        handle.settimeout(timeout)
        handle.connect(str(paths.socket))
        if _socket_identity(paths.socket) != _validate_socket_identity_value(
            record.get("socket_identity")
        ):
            raise SharedRuntimeContractError("shared_runtime.socket_identity_mismatch")
        peer_pid = _connected_peer_pid(handle)
        if peer_pid != record.get("pid") or process_identity(peer_pid) != record.get(
            "process_identity"
        ):
            raise SharedRuntimeContractError(
                "shared_runtime.socket_peer_identity_mismatch"
            )
        return handle
    except Exception:
        handle.close()
        raise


def _probe_broker_socket(
    paths: SharedRuntimePaths,
    record: dict[str, Any],
    *,
    verify_receipt_hmac: bool = True,
    verify_launch_contract: bool = True,
) -> bool:
    try:
        with _connect_verified_broker_socket(
            paths,
            record,
            timeout=0.25,
            allowed_states=("ready", "draining"),
            verify_receipt_hmac=verify_receipt_hmac,
            verify_launch_contract=verify_launch_contract,
        ):
            return True
    except (ConnectionRefusedError, FileNotFoundError, TimeoutError):
        return False
    except OSError:
        return False


def _validate_broker_record(
    paths: SharedRuntimePaths,
    record: dict[str, Any],
    *,
    allowed_states: tuple[str, ...] = ("ready", "draining"),
    verify_receipt_hmac: bool = True,
    verify_launch_contract: bool = True,
) -> None:
    expected_keys = {
        "schema",
        "state",
        "group_id",
        "launch_contract_sha256",
        "pid",
        "process_identity",
        "socket",
        "socket_identity",
        "broker_nonce",
        "started_at",
        "generation",
        "auth_mode",
        "transport",
        "receipt_hmac_sha256",
    }
    if record.get("state") == "draining":
        expected_keys.add("draining_at")
    contract_sha256 = record.get("launch_contract_sha256")
    launch_contract = _launch_contract() if verify_launch_contract else {}
    pid = record.get("pid")
    identity = record.get("process_identity")
    broker_nonce = record.get("broker_nonce")
    receipt_hmac_sha256 = record.get("receipt_hmac_sha256")
    if (
        set(record) != expected_keys
        or record.get("schema") != BROKER_SCHEMA
        or record.get("group_id") != paths.group.name
        or record.get("socket") != str(paths.socket)
        or record.get("state") not in allowed_states
        or not isinstance(contract_sha256, str)
        or not _SHA256_RE.fullmatch(contract_sha256)
        or contract_sha256[:12] != paths.group.name.rsplit("-", 1)[1]
        or (
            verify_launch_contract
            and (
                contract_sha256 != _launch_contract_sha256()
                or record.get("generation") != launch_contract["generation"]
                or record.get("auth_mode") != launch_contract["auth_mode"]
            )
        )
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid < 1
        or not isinstance(identity, str)
        or not identity
        or not isinstance(broker_nonce, str)
        or not _NONCE_RE.fullmatch(broker_nonce)
        or not isinstance(receipt_hmac_sha256, str)
        or not _SHA256_RE.fullmatch(receipt_hmac_sha256)
    ):
        raise SharedRuntimeContractError("shared_runtime.broker_receipt_invalid")
    _validate_socket_identity_value(record.get("socket_identity"))
    if verify_receipt_hmac and not hmac.compare_digest(
        receipt_hmac_sha256, _broker_receipt_hmac(paths, record)
    ):
        raise SharedRuntimeContractError("shared_runtime.broker_receipt_invalid")


def _archive_broker_receipt(paths: SharedRuntimePaths, *, reason: str) -> None:
    if not paths.broker_receipt.exists():
        return
    record = _read_json(paths.broker_receipt)
    _validate_broker_record(paths, record)
    closed_at = _utc_text()
    event = _sha256(f"{record.get('pid')}\0{reason}\0{closed_at}")[:12]
    destination = paths.history / f"broker-{event}.json"
    _atomic_json(
        destination,
        {**record, "closed_at": closed_at, "lifecycle_reason": reason},
        replace=False,
    )
    paths.broker_receipt.unlink()
    _fsync_directory(paths.group)


def _existing_broker_ready(paths: SharedRuntimePaths) -> bool:
    if not paths.broker_receipt.exists():
        return False
    record = _read_json(paths.broker_receipt)
    _validate_broker_record(paths, record)
    state_value = record.get("state")
    pid = record.get("pid")
    identity = record.get("process_identity")
    if not isinstance(pid, int) or not isinstance(identity, str):
        raise SharedRuntimeContractError("shared_runtime.broker_identity_invalid")
    state = probe_process(pid, identity)
    if state_value == "draining" and state == "same":
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and paths.broker_receipt.exists():
            if probe_process(pid, identity) != "same" or not _probe_broker_socket(
                paths, record
            ):
                break
            time.sleep(0.05)
        if paths.broker_receipt.exists():
            record = _read_json(paths.broker_receipt)
            _validate_broker_record(paths, record)
            pid = record.get("pid")
            identity = record.get("process_identity")
            if not isinstance(pid, int) or not isinstance(identity, str):
                raise SharedRuntimeContractError("shared_runtime.broker_identity_invalid")
            state = probe_process(pid, identity)
        else:
            return False
    if state == "same" and _probe_broker_socket(paths, record):
        if state_value == "draining":
            raise SharedRuntimeContractError("shared_runtime.broker_drain_timeout")
        return True
    if state in ("missing", "mismatch"):
        _archive_broker_receipt(paths, reason=f"broker_{state}")
        if paths.socket.exists():
            _socket_identity(paths.socket)
            paths.socket.unlink()
            _fsync_directory(paths.group)
        return False
    raise SharedRuntimeContractError("shared_runtime.broker_state_unknown")


def _launcher_path() -> Path:
    override = os.environ.get("BRIDGE_DB_SHARED_RUNTIME_LAUNCHER")
    manifest = os.environ.get("BRIDGE_DB_GENERATION_MANIFEST")
    if manifest:
        path = Path(manifest).parent / "bin" / "bridge-db-mcp"
    elif override:
        path = Path(override)
    else:
        path = Path(sys.argv[0])
    if not path.is_absolute() or not path.exists() or not os.access(path, os.X_OK):
        raise SharedRuntimeContractError("shared_runtime.launcher_invalid")
    return path


def _publish_broker_receipt(paths: SharedRuntimePaths) -> None:
    """Publish startup proof from the process that owns the broker socket."""
    identity = process_identity(os.getpid())
    if identity is None:
        raise SharedRuntimeContractError("shared_runtime.broker_identity_unknown")
    _atomic_json(
        paths.broker_receipt,
        _broker_receipt_record(
            paths,
            pid=os.getpid(),
            process_identity_value=identity,
        ),
        replace=False,
    )


def _start_broker(paths: SharedRuntimePaths) -> None:
    launcher = _launcher_path()
    log_descriptor = os.open(
        paths.broker_log,
        os.O_WRONLY | os.O_APPEND | os.O_CREAT,
        0o600,
    )
    try:
        process = subprocess.Popen(
            [str(launcher), "--run-shared-broker"],
            stdin=subprocess.DEVNULL,
            stdout=log_descriptor,
            stderr=log_descriptor,
            close_fds=True,
            start_new_session=True,
            env=os.environ.copy(),
        )
    finally:
        os.close(log_descriptor)

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SharedRuntimeContractError("shared_runtime.broker_start_failed")
        if paths.broker_receipt.exists():
            record = _read_json(paths.broker_receipt)
            _validate_broker_record(paths, record, allowed_states=("ready",))
            if record.get("pid") != process.pid or not _probe_broker_socket(
                paths, record
            ):
                raise SharedRuntimeContractError(
                    "shared_runtime.broker_start_receipt_invalid"
                )
            return
        time.sleep(0.05)
    raise SharedRuntimeContractError("shared_runtime.broker_start_timeout")


def ensure_shared_broker(*, owner_pid: int | None = None) -> SharedRuntimeBinding:
    """Register the parent relay and ensure exactly one matching broker is ready."""
    paths = shared_runtime_paths()
    with _group_lifecycle_lock(paths):
        lease, capability_file = _register_client(paths, owner_pid=owner_pid)
        try:
            if not _existing_broker_ready(paths):
                _start_broker(paths)
            return SharedRuntimeBinding(
                socket=paths.socket,
                client_lease=lease,
                capability_file=capability_file,
                release_launcher=_launcher_path(),
            )
        except Exception:
            if lease.exists():
                _retire_client(
                    lease,
                    _read_json(lease),
                    reason="broker_ensure_failed",
                    expected_contract_sha256=_launch_contract_sha256(),
                )
            raise


def release_shared_client(path: Path, *, owner_pid: int | None = None) -> None:
    """Close the exact lease owned by the calling relay parent."""
    paths, path = _lease_paths_from_client_path(path)
    with _group_lifecycle_lock(paths):
        if not path.exists():
            raise SharedRuntimeContractError("shared_runtime.client_lease_missing")
        record = _read_json(path)
        contract_sha256 = _launch_contract_sha256()
        _validate_client_record(
            path, record, expected_contract_sha256=contract_sha256
        )
        _require_owner_owns_client(
            record,
            owner_pid=owner_pid,
            mismatch_reason="shared_runtime.client_release_identity_mismatch",
        )
        _retire_client(
            path,
            record,
            reason="relay_normal_close",
            expected_contract_sha256=contract_sha256,
        )


class _VerifiedUnixHTTPConnection(http.client.HTTPConnection):
    def __init__(
        self,
        paths: SharedRuntimePaths,
        record: dict[str, Any],
        *,
        timeout: float = 10.0,
    ) -> None:
        super().__init__("localhost", timeout=timeout)
        self._paths = paths
        self._record = record
        self._socket_timeout = timeout

    def connect(self) -> None:
        self.sock = _connect_verified_broker_socket(
            self._paths,
            self._record,
            timeout=self._socket_timeout,
            allowed_states=("ready",),
        )


def _relay_capability_header(path: Path) -> tuple[str, str]:
    header = renew_shared_capability(path, owner_pid=os.getpid())
    name, separator, value = header.partition(": ")
    if (
        separator != ": "
        or name != "X-Bridge-Relay-Capability"
        or _CAPABILITY_RE.fullmatch(value) is None
    ):
        raise SharedRuntimeContractError("shared_runtime.capability_header_invalid")
    return name, value


def _http_request_to_broker(
    paths: SharedRuntimePaths,
    *,
    method: str,
    body: bytes | None,
    headers: dict[str, str],
) -> tuple[int, dict[str, str], bytes]:
    record = _read_json(paths.broker_receipt)
    _validate_broker_record(paths, record, allowed_states=("ready",))
    connection = _VerifiedUnixHTTPConnection(paths, record)
    try:
        connection.request(method, "/mcp", body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read()
        response_headers = {
            name.lower(): value for name, value in response.getheaders()
        }
        return response.status, response_headers, response_body
    except (http.client.HTTPException, OSError) as exc:
        raise SharedRuntimeContractError("shared_runtime.relay_http_failed") from exc
    finally:
        connection.close()


def _relay_request_headers(
    client_lease: Path,
    *,
    session_id: str | None,
    protocol_version: str | None,
) -> dict[str, str]:
    capability_name, capability_value = _relay_capability_header(client_lease)
    headers = {
        capability_name: capability_value,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    if protocol_version:
        headers["Mcp-Protocol-Version"] = protocol_version
    return headers


def _extract_protocol_version(body: bytes) -> str | None:
    try:
        payload = cast(object, json.loads(body.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    payload_map = cast(dict[object, object], payload)
    result = payload_map.get("result")
    if not isinstance(result, dict):
        return None
    result_map = cast(dict[object, object], result)
    protocol_version = result_map.get("protocolVersion")
    return protocol_version if isinstance(protocol_version, str) else None


def _write_relay_body(body: bytes) -> None:
    if not body:
        raise SharedRuntimeContractError("shared_runtime.relay_empty_response")
    text = body.decode("utf-8").replace("\n", "")
    if not text:
        raise SharedRuntimeContractError("shared_runtime.relay_empty_response")
    print(text, flush=True)


def run_shared_relay() -> int:
    """Run the stdio-to-shared-broker relay in one authenticated process."""
    binding = ensure_shared_broker(owner_pid=os.getpid())
    paths, client_lease = _lease_paths_from_client_path(binding.client_lease)
    session_id: str | None = None
    protocol_version: str | None = None
    try:
        for raw_message in sys.stdin:
            message = raw_message.rstrip("\n")
            status, response_headers, response_body = _http_request_to_broker(
                paths,
                method="POST",
                body=message.encode("utf-8"),
                headers=_relay_request_headers(
                    client_lease,
                    session_id=session_id,
                    protocol_version=protocol_version,
                ),
            )
            if status == 202:
                continue
            if status != 200:
                raise SharedRuntimeContractError(
                    f"shared_runtime.relay_http_{status}"
                )
            if session_id is None:
                observed_session = response_headers.get("mcp-session-id")
                if observed_session:
                    session_id = observed_session
            if protocol_version is None:
                protocol_version = _extract_protocol_version(response_body)
            _write_relay_body(response_body)
        return 0
    finally:
        try:
            if session_id is not None:
                _http_request_to_broker(
                    paths,
                    method="DELETE",
                    body=None,
                    headers=_relay_request_headers(
                        client_lease,
                        session_id=session_id,
                        protocol_version=protocol_version,
                    ),
                )
        except SharedRuntimeContractError:
            pass
        with suppress(SharedRuntimeContractError, OSError):
            release_shared_client(client_lease, owner_pid=os.getpid())


def _idle_seconds() -> float:
    raw = os.environ.get("BRIDGE_DB_BROKER_IDLE_SECONDS", "300")
    try:
        value = float(raw)
    except ValueError as exc:
        raise SharedRuntimeContractError("shared_runtime.idle_seconds_invalid") from exc
    if value < 1 or value > 86400:
        raise SharedRuntimeContractError("shared_runtime.idle_seconds_invalid")
    return value


async def _monitor_idle_broker(
    paths: SharedRuntimePaths, server: Any, *, idle_seconds: float
) -> None:
    empty_since: float | None = None
    while not server.should_exit:
        await asyncio.sleep(min(1.0, idle_seconds))
        try:
            with _group_lifecycle_lock(paths, nonblocking=True):
                live_client_count = _live_client_count_unlocked(paths)
        except BlockingIOError:
            continue
        if live_client_count > 0:
            empty_since = None
            continue
        empty_since = empty_since or time.monotonic()
        if time.monotonic() - empty_since >= idle_seconds:
            try:
                with _group_lifecycle_lock(paths, nonblocking=True):
                    if _live_client_count_unlocked(paths) > 0:
                        empty_since = None
                        continue
                    if paths.broker_receipt.exists():
                        receipt = _read_json(paths.broker_receipt)
                        _validate_broker_record(
                            paths, receipt, allowed_states=("ready",)
                        )
                        if receipt.get("pid") != os.getpid():
                            raise SharedRuntimeContractError(
                                "shared_runtime.broker_receipt_invalid"
                            )
                        _atomic_json(
                            paths.broker_receipt,
                            _rehash_broker_receipt(
                                paths,
                                {
                                    **receipt,
                                    "state": "draining",
                                    "draining_at": _utc_text(),
                                },
                            ),
                            replace=True,
                        )
                    server.should_exit = True
                    return
            except BlockingIOError:
                continue


async def _monitor_broker_retirement(tracker: Any, server: Any) -> None:
    while not server.should_exit:
        await asyncio.sleep(1.0)
        if tracker.retirement_ready():
            server.should_exit = True
            return


async def _await_broker_tasks(
    server: Any,
    serve_task: asyncio.Task[None],
    monitors: set[asyncio.Task[None]],
    *,
    shutdown_timeout: float = 10.0,
) -> None:
    """Propagate monitor failures and bound cooperative server shutdown."""
    done, _pending = await asyncio.wait(
        {serve_task, *monitors}, return_when=asyncio.FIRST_COMPLETED
    )
    if serve_task in done:
        await serve_task
        return

    monitor_error: BaseException | None = None
    for task in done & monitors:
        if task.cancelled():
            monitor_error = SharedRuntimeContractError(
                "shared_runtime.monitor_cancelled"
            )
            break
        exception = task.exception()
        if exception is not None:
            monitor_error = exception
            break
        if not server.should_exit:
            monitor_error = SharedRuntimeContractError(
                "shared_runtime.monitor_stopped_without_shutdown"
            )
            break

    server.should_exit = True
    try:
        await asyncio.wait_for(serve_task, timeout=shutdown_timeout)
    except TimeoutError as exc:
        raise SharedRuntimeContractError(
            "shared_runtime.broker_shutdown_timeout"
        ) from exc
    if monitor_error is not None:
        raise monitor_error


async def _serve_broker(paths: SharedRuntimePaths) -> None:
    import uvicorn

    from bridge_db.auth import load_principal_grants, resolve_grant
    from bridge_db.server import build_tenancy_tracker, mcp

    raw_token = os.environ.get("BRIDGE_DB_PRINCIPAL_TOKEN")
    token = raw_token.strip() if raw_token is not None else None
    grant = resolve_grant(token, load_principal_grants(config.PRINCIPALS_PATH))
    principal = grant.caller if grant is not None else None
    tracker, _runtime_generation = build_tenancy_tracker(principal)
    tracker.start()
    mcp.enable_shared_runtime(tracker)
    mcp.settings.json_response = True
    # DNS-rebinding checks protect TCP listeners. This transport has no TCP
    # listener and is reachable only through its owner-only Unix socket.
    transport_security = mcp.settings.transport_security
    if transport_security is None:  # pragma: no cover - FastMCP default invariant
        raise SharedRuntimeContractError("shared_runtime.transport_security_missing")
    transport_security.enable_dns_rebinding_protection = False
    app = _RelayCapabilityMiddleware(paths, mcp.streamable_http_app())
    configuration = uvicorn.Config(
        app,
        uds=str(paths.socket),
        log_level=config.LOG_LEVEL.lower(),
        access_log=False,
    )
    server = uvicorn.Server(configuration)
    serve_task = asyncio.create_task(server.serve())
    monitors: set[asyncio.Task[None]] = set()
    try:
        startup_deadline = time.monotonic() + 10.0
        while not server.started:
            if serve_task.done():
                await serve_task
                raise SharedRuntimeContractError(
                    "shared_runtime.broker_start_failed"
                )
            if time.monotonic() >= startup_deadline:
                server.should_exit = True
                raise SharedRuntimeContractError(
                    "shared_runtime.broker_start_timeout"
                )
            await asyncio.sleep(0.01)
        _publish_broker_receipt(paths)
        idle_task = asyncio.create_task(
            _monitor_idle_broker(paths, server, idle_seconds=_idle_seconds())
        )
        retirement_task = asyncio.create_task(
            _monitor_broker_retirement(tracker, server)
        )
        monitors = {idle_task, retirement_task}
        await _await_broker_tasks(server, serve_task, monitors)
    finally:
        server.should_exit = True
        for task in monitors:
            task.cancel()
        for task in monitors:
            with suppress(asyncio.CancelledError):
                await task
        if not serve_task.done():
            with suppress(TimeoutError):
                await asyncio.wait_for(serve_task, timeout=10.0)
        mcp.close_shared_runtime()


def run_shared_broker() -> None:
    """Run one credential/generation broker until its relay references go idle."""
    paths = shared_runtime_paths()
    run_lock = _open_private_lock(paths.group / "broker.run.lock")
    try:
        try:
            fcntl.flock(run_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SharedRuntimeContractError("shared_runtime.broker_already_running") from exc
        if paths.socket.exists():
            raise SharedRuntimeContractError("shared_runtime.socket_already_exists")
        previous_umask = os.umask(0o077)
        try:
            asyncio.run(_serve_broker(paths))
        finally:
            os.umask(previous_umask)
    finally:
        if paths.socket.exists():
            _socket_identity(paths.socket)
            paths.socket.unlink()
            _fsync_directory(paths.group)
        if paths.broker_receipt.exists():
            record = _read_json(paths.broker_receipt)
            if record.get("pid") == os.getpid():
                _archive_broker_receipt(paths, reason="broker_idle_close")
        os.close(run_lock)
