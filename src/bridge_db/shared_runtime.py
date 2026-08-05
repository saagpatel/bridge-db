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
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from bridge_db import clock, config
from bridge_db.tenancy import probe_process, process_identity

CLIENT_SCHEMA = "BridgeSharedRuntimeClientLeaseV1"
BROKER_SCHEMA = "BridgeSharedRuntimeBrokerReceiptV1"
_KEY_RE = re.compile(r"^[0-9a-f]{16}-[0-9a-f]{12}$")
_LEASE_RE = re.compile(r"^[0-9a-f]{24}$")


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
    history: Path
    broker_receipt: Path
    broker_log: Path


@dataclass(frozen=True)
class SharedRuntimeBinding:
    socket: Path
    client_lease: Path
    release_launcher: Path


def _utc_text() -> str:
    return clock.now().isoformat().replace("+00:00", "Z")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
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
    generation = os.environ.get("BRIDGE_DB_GENERATION_ID", "mutable")
    selector = hmac.new(
        _selector_secret(root), token.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:16]
    return f"{selector}-{_sha256(generation)[:12]}"


def _shared_runtime_root() -> Path:
    return _guard_private_directory(
        Path(
            os.environ.get(
                "BRIDGE_DB_SHARED_RUNTIME_ROOT",
                str(config.DB_PATH.parent / "shared-runtime"),
            )
        ),
        create=True,
    )


def _paths_for_group(root: Path, group: Path) -> SharedRuntimePaths:
    group = _guard_private_directory(group, create=True)
    clients = _guard_private_directory(group / "clients", create=True)
    history = _guard_private_directory(group / "history", create=True)
    return SharedRuntimePaths(
        root=root,
        group=group,
        socket=group / "bridge.sock",
        clients=clients,
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


def _validate_client_record(path: Path, record: dict[str, Any]) -> None:
    lease_id = record.get("lease_id")
    if (
        record.get("schema") != CLIENT_SCHEMA
        or not isinstance(lease_id, str)
        or not _LEASE_RE.fullmatch(lease_id)
        or path.stem != lease_id
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


def _retire_client(path: Path, record: dict[str, Any], *, reason: str) -> None:
    _validate_client_record(path, record)
    closed_at = _utc_text()
    event = _sha256(f"{record['lease_id']}\0{reason}\0{closed_at}")[:12]
    history = path.parent.parent / "history" / f"{record['lease_id']}-{event}.json"
    _atomic_json(
        history,
        {**record, "closed_at": closed_at, "lifecycle_reason": reason},
        replace=False,
    )
    path.unlink()
    _fsync_directory(path.parent)


def _register_client(paths: SharedRuntimePaths) -> Path:
    pid = os.getppid()
    identity = process_identity(pid)
    if identity is None:
        raise SharedRuntimeContractError("shared_runtime.client_identity_unknown")
    nonce = os.urandom(16).hex()
    lease_id = _sha256(f"{pid}\0{identity}\0{nonce}")[:24]
    path = paths.clients / f"{lease_id}.json"
    _atomic_json(
        path,
        {
            "schema": CLIENT_SCHEMA,
            "lease_id": lease_id,
            "pid": pid,
            "process_identity": identity,
            "created_at": _utc_text(),
            "generation": os.environ.get("BRIDGE_DB_GENERATION_ID"),
            "lifecycle_reason": "relay_registered",
        },
        replace=False,
    )
    return path


def _live_client_count(paths: SharedRuntimePaths) -> int:
    live = 0
    for path in sorted(paths.clients.glob("*.json")):
        record = _read_json(path)
        _validate_client_record(path, record)
        state = probe_process(int(record["pid"]), str(record["process_identity"]))
        if state == "same":
            live += 1
        elif state in ("missing", "mismatch"):
            _retire_client(path, record, reason=f"relay_{state}")
        else:
            # Unknown process state is not safe to reap and keeps the broker resident.
            live += 1
    return live


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


def _archive_broker_receipt(paths: SharedRuntimePaths, *, reason: str) -> None:
    if not paths.broker_receipt.exists():
        return
    record = _read_json(paths.broker_receipt)
    if record.get("schema") != BROKER_SCHEMA:
        raise SharedRuntimeContractError("shared_runtime.broker_receipt_invalid")
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
    if record.get("schema") != BROKER_SCHEMA or record.get("socket") != str(
        paths.socket
    ):
        raise SharedRuntimeContractError("shared_runtime.broker_receipt_invalid")
    state_value = record.get("state")
    if state_value not in ("ready", "draining"):
        raise SharedRuntimeContractError("shared_runtime.broker_receipt_invalid")
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
        if _probe_socket(paths.socket):
            identity = process_identity(process.pid)
            if identity is None:
                raise SharedRuntimeContractError("shared_runtime.broker_identity_unknown")
            _atomic_json(
                paths.broker_receipt,
                {
                    "schema": BROKER_SCHEMA,
                    "state": "ready",
                    "pid": process.pid,
                    "process_identity": identity,
                    "socket": str(paths.socket),
                    "started_at": _utc_text(),
                    "generation": os.environ.get("BRIDGE_DB_GENERATION_ID"),
                    "transport": "streamable_http_over_private_unix_socket",
                },
                replace=False,
            )
            return
        time.sleep(0.05)
    raise SharedRuntimeContractError("shared_runtime.broker_start_timeout")


def ensure_shared_broker() -> SharedRuntimeBinding:
    """Register the parent relay and ensure exactly one matching broker is ready."""
    paths = shared_runtime_paths()
    lease: Path | None = None
    lock_path = paths.group / "ensure.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        lease = _register_client(paths)
        if not _existing_broker_ready(paths):
            _start_broker(paths)
        return SharedRuntimeBinding(
            socket=paths.socket,
            client_lease=lease,
            release_launcher=_launcher_path(),
        )
    except Exception:
        if lease is not None and lease.exists():
            _retire_client(lease, _read_json(lease), reason="broker_ensure_failed")
        raise
    finally:
        os.close(descriptor)


def release_shared_client(path: Path) -> None:
    """Close the exact lease owned by the calling relay parent."""
    root = _shared_runtime_root()
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
    paths = _paths_for_group(root, root / relative.parts[0])
    if path.parent != paths.clients:
        raise SharedRuntimeContractError("shared_runtime.client_path_invalid")
    record = _read_json(path)
    _validate_client_record(path, record)
    parent_pid = os.getppid()
    parent_identity = process_identity(parent_pid)
    if record.get("pid") != parent_pid or record.get("process_identity") != parent_identity:
        raise SharedRuntimeContractError("shared_runtime.client_release_identity_mismatch")
    _retire_client(path, record, reason="relay_normal_close")


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
        if _live_client_count(paths) > 0:
            empty_since = None
            continue
        empty_since = empty_since or time.monotonic()
        if time.monotonic() - empty_since >= idle_seconds:
            descriptor = os.open(
                paths.group / "ensure.lock", os.O_RDWR | os.O_CREAT, 0o600
            )
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                if _live_client_count(paths) > 0:
                    empty_since = None
                    continue
                if paths.broker_receipt.exists():
                    receipt = _read_json(paths.broker_receipt)
                    if (
                        receipt.get("schema") != BROKER_SCHEMA
                        or receipt.get("pid") != os.getpid()
                        or receipt.get("state") != "ready"
                    ):
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
            finally:
                os.close(descriptor)


async def _monitor_broker_retirement(tracker: Any, server: Any) -> None:
    while not server.should_exit:
        await asyncio.sleep(1.0)
        if tracker.retirement_ready():
            server.should_exit = True
            return


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
    app = mcp.streamable_http_app()
    configuration = uvicorn.Config(
        app,
        uds=str(paths.socket),
        log_level=config.LOG_LEVEL.lower(),
        access_log=False,
    )
    server = uvicorn.Server(configuration)
    serve_task = asyncio.create_task(server.serve())
    idle_task = asyncio.create_task(
        _monitor_idle_broker(paths, server, idle_seconds=_idle_seconds())
    )
    retirement_task = asyncio.create_task(_monitor_broker_retirement(tracker, server))
    monitors = {idle_task, retirement_task}
    try:
        done, _pending = await asyncio.wait(
            {serve_task, *monitors}, return_when=asyncio.FIRST_COMPLETED
        )
        monitor_error: BaseException | None = None
        for task in done & monitors:
            if not task.cancelled() and task.exception() is not None:
                monitor_error = task.exception()
                server.should_exit = True
        if serve_task not in done:
            await serve_task
        else:
            await serve_task
        if monitor_error is not None:
            raise monitor_error
    finally:
        for task in monitors:
            task.cancel()
        for task in monitors:
            with suppress(asyncio.CancelledError):
                await task
        mcp.close_shared_runtime()


def run_shared_broker() -> None:
    """Run one credential/generation broker until its relay references go idle."""
    paths = shared_runtime_paths()
    run_lock = os.open(paths.group / "broker.run.lock", os.O_RDWR | os.O_CREAT, 0o600)
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
