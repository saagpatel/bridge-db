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
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from bridge_db import clock, config
from bridge_db.tenancy import probe_process, process_identity, tenancy_root

CLIENT_SCHEMA = "BridgeSharedRuntimeClientLeaseV1"
BROKER_SCHEMA = "BridgeSharedRuntimeBrokerReceiptV1"
INVENTORY_SCHEMA = "BridgeSharedRuntimeInventoryV1"
LAUNCH_CONTRACT_SCHEMA = "BridgeSharedRuntimeLaunchContractV1"
_KEY_RE = re.compile(r"^[0-9a-f]{16}-[0-9a-f]{12}$")
_LEASE_RE = re.compile(r"^[0-9a-f]{24}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CAPABILITY_RE = re.compile(r"^([0-9a-f]{24})\.([0-9a-f]{64})$")
_CAPABILITY_HEADER = b"x-bridge-relay-capability"


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
        Path(launcher_override)
        if launcher_override
        else Path(manifest)
        if manifest
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


def _selector_secret(root: Path) -> bytes:
    path = root / "selector.key"
    if not path.exists():
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


def _credential_key(root: Path) -> str:
    raw_token = os.environ.get("BRIDGE_DB_PRINCIPAL_TOKEN")
    token = raw_token.strip() if raw_token is not None else ""
    if len(token) < 32:
        raise SharedRuntimeContractError("shared_runtime.credential_invalid")
    contract = _launch_contract_bytes()
    selector = hmac.new(
        _selector_secret(root), token.encode("utf-8") + b"\0" + contract, hashlib.sha256
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


def shared_runtime_paths() -> SharedRuntimePaths:
    root = _shared_runtime_root()
    key = _credential_key(root)
    if not _KEY_RE.fullmatch(key):  # pragma: no cover - construction invariant
        raise SharedRuntimeContractError("shared_runtime.key_invalid")
    return _paths_for_group(root, root / key)


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
            clients = _guard_private_directory(group / "clients", create=False)
            capabilities = _guard_private_directory(
                group / "capabilities", create=False
            )
            _guard_private_directory(group / "history", create=False)
            broker_path = group / "broker.json"
            socket_path = group / "bridge.sock"
            socket_exists = socket_path.exists()
            if socket_exists:
                if socket_path.is_symlink() or not stat.S_ISSOCK(
                    socket_path.stat().st_mode
                ):
                    raise SharedRuntimeContractError(
                        "shared_runtime.socket_target_invalid"
                    )
                inventory["socket_count"] += 1

            if broker_path.exists():
                record = _read_json(broker_path)
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
                reachable = socket_exists and _probe_socket(socket_path)
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
                capability_value = _read_capability_value(
                    capability_path, expected_lease_id=lease_path.stem
                )
                capability_secret = capability_value.decode("ascii").split(".", 1)[1]
                if not hmac.compare_digest(
                    str(record["capability_sha256"]), _sha256(capability_secret)
                ):
                    raise SharedRuntimeContractError(
                        "shared_runtime.capability_file_invalid"
                    )
                state = probe_process(
                    int(record["pid"]), str(record["process_identity"])
                )
                client_states[state] += 1
                inventory["client_lease_count"] += 1
                inventory["capability_file_count"] += 1

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
    """Read one exact private curl header and return only its request value."""
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


def _register_client(paths: SharedRuntimePaths) -> tuple[Path, Path]:
    pid = os.getppid()
    identity = process_identity(pid)
    if identity is None:
        raise SharedRuntimeContractError("shared_runtime.client_identity_unknown")
    nonce = os.urandom(16).hex()
    lease_id = _sha256(f"{pid}\0{identity}\0{nonce}")[:24]
    path = paths.clients / f"{lease_id}.json"
    capability_secret = os.urandom(32).hex()
    capability_path = _capability_path_for_lease(path)
    capability_header = (
        f"X-Bridge-Relay-Capability: {lease_id}.{capability_secret}\n"
    ).encode("ascii")
    _atomic_secret(capability_path, capability_header)
    try:
        _atomic_json(
            path,
            {
                "schema": CLIENT_SCHEMA,
                "lease_id": lease_id,
                "group_id": paths.group.name,
                "launch_contract_sha256": _launch_contract_sha256(),
                "capability_sha256": _sha256(capability_secret),
                "capability_file": str(capability_path),
                "pid": pid,
                "process_identity": identity,
                "created_at": _utc_text(),
                "generation": _launch_contract()["generation"],
                "lifecycle_reason": "relay_registered",
            },
            replace=False,
        )
    except Exception:
        capability_path.unlink(missing_ok=True)
        _fsync_directory(capability_path.parent)
        raise
    return path, capability_path


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
    capability_path = paths.capabilities / f"{lease_id}.header"
    try:
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
        capability_value = _read_capability_value(
            capability_path, expected_lease_id=lease_id
        )
        return hmac.compare_digest(capability_value, raw_value) and hmac.compare_digest(
            str(record["capability_sha256"]), _sha256(secret)
        )
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


def _validate_broker_record(
    paths: SharedRuntimePaths,
    record: dict[str, Any],
    *,
    allowed_states: tuple[str, ...] = ("ready", "draining"),
) -> None:
    contract_sha256 = record.get("launch_contract_sha256")
    launch_contract = _launch_contract()
    if (
        record.get("schema") != BROKER_SCHEMA
        or record.get("group_id") != paths.group.name
        or record.get("socket") != str(paths.socket)
        or record.get("state") not in allowed_states
        or not isinstance(contract_sha256, str)
        or not _SHA256_RE.fullmatch(contract_sha256)
        or contract_sha256[:12] != paths.group.name.rsplit("-", 1)[1]
        or contract_sha256 != _launch_contract_sha256()
        or record.get("generation") != launch_contract["generation"]
        or record.get("auth_mode") != launch_contract["auth_mode"]
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
            if probe_process(pid, identity) != "same" or not _probe_socket(paths.socket):
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
    if state == "same" and _probe_socket(paths.socket):
        if state_value == "draining":
            raise SharedRuntimeContractError("shared_runtime.broker_drain_timeout")
        return True
    if state in ("missing", "mismatch"):
        _archive_broker_receipt(paths, reason=f"broker_{state}")
        if paths.socket.exists():
            if paths.socket.is_symlink() or not stat.S_ISSOCK(paths.socket.stat().st_mode):
                raise SharedRuntimeContractError("shared_runtime.socket_target_invalid")
            paths.socket.unlink()
            _fsync_directory(paths.group)
        return False
    raise SharedRuntimeContractError("shared_runtime.broker_state_unknown")


def _launcher_path() -> Path:
    override = os.environ.get("BRIDGE_DB_SHARED_RUNTIME_LAUNCHER")
    manifest = os.environ.get("BRIDGE_DB_GENERATION_MANIFEST")
    if override:
        path = Path(override)
    elif manifest:
        path = Path(manifest).parent / "bin" / "bridge-db-mcp"
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
    launch_contract = _launch_contract()
    _atomic_json(
        paths.broker_receipt,
        {
            "schema": BROKER_SCHEMA,
            "state": "ready",
            "group_id": paths.group.name,
            "launch_contract_sha256": _launch_contract_sha256(),
            "pid": os.getpid(),
            "process_identity": identity,
            "socket": str(paths.socket),
            "started_at": _utc_text(),
            "generation": launch_contract["generation"],
            "auth_mode": launch_contract["auth_mode"],
            "transport": "streamable_http_over_private_unix_socket",
        },
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
            if record.get("pid") != process.pid or not _probe_socket(paths.socket):
                raise SharedRuntimeContractError(
                    "shared_runtime.broker_start_receipt_invalid"
                )
            return
        time.sleep(0.05)
    raise SharedRuntimeContractError("shared_runtime.broker_start_timeout")


def ensure_shared_broker() -> SharedRuntimeBinding:
    """Register the parent relay and ensure exactly one matching broker is ready."""
    paths = shared_runtime_paths()
    with _group_lifecycle_lock(paths):
        lease, capability_file = _register_client(paths)
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


def release_shared_client(path: Path) -> None:
    """Close the exact lease owned by the calling relay parent."""
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
    ):
        raise SharedRuntimeContractError("shared_runtime.client_path_invalid")
    paths = _paths_for_group(root, root / relative.parts[0], create=False)
    if path.parent != paths.clients:
        raise SharedRuntimeContractError("shared_runtime.client_path_invalid")
    with _group_lifecycle_lock(paths):
        if not path.exists():
            raise SharedRuntimeContractError("shared_runtime.client_lease_missing")
        record = _read_json(path)
        contract_sha256 = _launch_contract_sha256()
        _validate_client_record(
            path, record, expected_contract_sha256=contract_sha256
        )
        parent_pid = os.getppid()
        parent_identity = process_identity(parent_pid)
        if (
            record.get("pid") != parent_pid
            or record.get("process_identity") != parent_identity
        ):
            raise SharedRuntimeContractError(
                "shared_runtime.client_release_identity_mismatch"
            )
        _retire_client(
            path,
            record,
            reason="relay_normal_close",
            expected_contract_sha256=contract_sha256,
        )


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
                            {
                                **receipt,
                                "state": "draining",
                                "draining_at": _utc_text(),
                            },
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
            if paths.socket.is_symlink() or not stat.S_ISSOCK(paths.socket.stat().st_mode):
                raise SharedRuntimeContractError("shared_runtime.socket_target_invalid")
            paths.socket.unlink()
            _fsync_directory(paths.group)
        if paths.broker_receipt.exists():
            record = _read_json(paths.broker_receipt)
            if record.get("pid") == os.getpid():
                _archive_broker_receipt(paths, reason="broker_idle_close")
        os.close(run_lock)
