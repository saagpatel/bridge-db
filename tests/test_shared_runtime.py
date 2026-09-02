"""Shared broker and thin stdio relay contract tests."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from bridge_db import config
from bridge_db import shared_runtime as shared_runtime_module
from bridge_db.shared_runtime import (
    SharedRuntimeContractError,
    shared_runtime_current_readiness,
    shared_runtime_inventory,
    shared_runtime_paths,
)
from bridge_db.tenancy import process_identity

_REPO_ROOT = Path(__file__).parents[1]
_STABLE_LAUNCHER = "/Users/d/.local/state/bridge-db/current/bin/bridge-db-mcp"


def _noisy_gnu_stat_shim(tmp_path: Path) -> Path:
    """Fail BSD stat probes with stdout before emulating GNU file formats."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "stat"
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = -f ]; then\n"
        "    printf '%s\\n' filesystem-status-noise\n"
        "    exit 1\n"
        "fi\n"
        "if [ \"$1\" != -c ]; then exit 64; fi\n"
        "case \"$(uname -s)\" in\n"
        "    Darwin)\n"
        "        case \"$2\" in\n"
        "            '%u') exec /usr/bin/stat -f '%u' \"$3\" ;;\n"
        "            '%a') exec /usr/bin/stat -f '%Lp' \"$3\" ;;\n"
        "            *) exit 64 ;;\n"
        "        esac\n"
        "        ;;\n"
        "    *) exec /usr/bin/stat \"$@\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return bin_dir


def _broker_receipt(
    paths: shared_runtime_module.SharedRuntimePaths,
    *,
    pid: int | None = None,
    identity: str | None = None,
    state: str = "ready",
) -> dict[str, object]:
    process_id = os.getpid() if pid is None else pid
    process_identity_value = identity or process_identity(process_id)
    assert process_identity_value is not None
    return shared_runtime_module._broker_receipt_record(  # pyright: ignore[reportPrivateUsage]
        paths,
        pid=process_id,
        process_identity_value=process_identity_value,
        state=state,
        started_at="2026-08-05T00:00:00Z",
    )


@pytest.fixture
def short_runtime_root() -> Iterator[Path]:
    # AF_UNIX paths are short on several platforms. Resolve both the platform
    # temp directory and POSIX /tmp (when present), then use the shortest
    # writable parent instead of hard-coding one operating system's target.
    candidates = {Path(tempfile.gettempdir()).resolve()}
    posix_tmp = Path("/tmp")
    if posix_tmp.is_dir():
        candidates.add(posix_tmp.resolve())
    writable = [
        path
        for path in candidates
        if path.is_dir() and os.access(path, os.W_OK | os.X_OK)
    ]
    assert writable
    short_parent = min(writable, key=lambda path: len(str(path)))
    root = Path(tempfile.mkdtemp(prefix="bridge-shared-", dir=short_parent))
    root.chmod(0o700)
    try:
        yield root
    finally:
        shutil.rmtree(root)


def test_shared_runtime_partitions_private_groups_by_credential_and_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRIDGE_DB_SHARED_RUNTIME_ROOT", str(tmp_path / "shared"))
    monkeypatch.setenv(
        "BRIDGE_DB_PRINCIPAL_TOKEN", "fixture-shared-secret-abcdefghijklmnopqrstuvwxyz"
    )
    monkeypatch.setattr(config, "AUTH_MODE", "off")
    monkeypatch.setenv("BRIDGE_DB_GENERATION_ID", "generation-one")
    first = shared_runtime_paths()
    monkeypatch.setenv("BRIDGE_DB_GENERATION_ID", "generation-two")
    second = shared_runtime_paths()
    monkeypatch.setenv("BRIDGE_DB_GENERATION_ID", "generation-one")
    monkeypatch.setattr(config, "AUTH_MODE", "warn")
    different_auth_mode = shared_runtime_paths()
    monkeypatch.setattr(config, "AUTH_MODE", "off")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "different.db")
    different_database = shared_runtime_paths()

    assert len(
        {
            first.group,
            second.group,
            different_auth_mode.group,
            different_database.group,
        }
    ) == 4
    assert (
        first.group.parent
        == second.group.parent
        == different_auth_mode.group.parent
        == different_database.group.parent
        == tmp_path / "shared"
    )
    assert "fixture-shared-secret" not in first.group.name
    for path in (
        first.root,
        first.group,
        first.clients,
        first.history,
        second.group,
        different_auth_mode.group,
        different_database.group,
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_current_readiness_cannot_borrow_an_unrelated_active_group(
    short_runtime_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "BRIDGE_DB_SHARED_RUNTIME_ROOT", str(short_runtime_root / "shared")
    )
    monkeypatch.setenv("BRIDGE_DB_GENERATION_ID", "generation-one")
    monkeypatch.setattr(config, "AUTH_MODE", "off")
    monkeypatch.setenv(
        "BRIDGE_DB_PRINCIPAL_TOKEN",
        "fixture-first-shared-secret-abcdefghijklmnopqrstuvwxyz",
    )
    first = shared_runtime_paths()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(first.socket))
    listener.listen(1)
    shared_runtime_module._atomic_json(  # pyright: ignore[reportPrivateUsage]
        first.broker_receipt,
        _broker_receipt(first),
        replace=False,
    )
    def selected_socket(
        paths: shared_runtime_module.SharedRuntimePaths,
        _record: dict[str, object],
        *,
        verify_receipt_hmac: bool = True,
        verify_launch_contract: bool = True,
    ) -> bool:
        _ = (verify_receipt_hmac, verify_launch_contract)
        return paths.socket == first.socket

    monkeypatch.setattr(shared_runtime_module, "_probe_broker_socket", selected_socket)

    try:
        assert shared_runtime_inventory(first.root)["adoption_state"] == "active"
        assert shared_runtime_current_readiness()["ready"] is True

        monkeypatch.setenv(
            "BRIDGE_DB_PRINCIPAL_TOKEN",
            "fixture-second-shared-secret-abcdefghijklmnopqrstuvwxyz",
        )
        unrelated = shared_runtime_current_readiness()

        assert shared_runtime_inventory(first.root)["adoption_state"] == "active"
        assert unrelated["state"] == "missing"
        assert unrelated["ready"] is False
        assert unrelated["reason_code"] == "shared_runtime.current_group_missing"
    finally:
        listener.close()
        first.socket.unlink(missing_ok=True)


def test_stale_broker_receipt_and_socket_are_archived_before_restart(
    short_runtime_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "BRIDGE_DB_SHARED_RUNTIME_ROOT", str(short_runtime_root / "shared")
    )
    monkeypatch.setenv(
        "BRIDGE_DB_PRINCIPAL_TOKEN", "fixture-shared-secret-abcdefghijklmnopqrstuvwxyz"
    )
    monkeypatch.setenv("BRIDGE_DB_GENERATION_ID", "generation-one")
    monkeypatch.setattr(config, "AUTH_MODE", "enforce")
    paths = shared_runtime_paths()
    stale_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale_socket.bind(str(paths.socket))
    stale_socket.close()
    shared_runtime_module._atomic_json(  # pyright: ignore[reportPrivateUsage]
        paths.broker_receipt,
        _broker_receipt(
            paths,
            pid=99_999_999,
            identity="fixture-missing-process",
        ),
        replace=False,
    )

    with shared_runtime_module._group_lifecycle_lock(  # pyright: ignore[reportPrivateUsage]
        paths
    ):
        assert shared_runtime_module._existing_broker_ready(  # pyright: ignore[reportPrivateUsage]
            paths
        ) is False

    assert not paths.broker_receipt.exists()
    assert not paths.socket.exists()
    archived = list(paths.history.glob("broker-*.json"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text(encoding="utf-8"))[
        "lifecycle_reason"
    ] == "broker_missing"


def test_existing_broker_receipt_must_match_complete_launch_contract(
    short_runtime_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "BRIDGE_DB_SHARED_RUNTIME_ROOT", str(short_runtime_root / "shared")
    )
    monkeypatch.setenv(
        "BRIDGE_DB_PRINCIPAL_TOKEN", "fixture-shared-secret-abcdefghijklmnopqrstuvwxyz"
    )
    monkeypatch.setenv("BRIDGE_DB_GENERATION_ID", "generation-one")
    paths = shared_runtime_paths()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(paths.socket))
    listener.listen(1)
    identity = process_identity(os.getpid())
    assert identity is not None
    actual_contract = shared_runtime_module._launch_contract_sha256()  # pyright: ignore[reportPrivateUsage]
    replacement = "0" if actual_contract[12] != "0" else "1"
    mismatched_contract = actual_contract[:12] + replacement + actual_contract[13:]
    record = _broker_receipt(paths)
    record["launch_contract_sha256"] = mismatched_contract
    record = shared_runtime_module._rehash_broker_receipt(  # pyright: ignore[reportPrivateUsage]
        paths, record
    )
    shared_runtime_module._atomic_json(  # pyright: ignore[reportPrivateUsage]
        paths.broker_receipt,
        record,
        replace=False,
    )

    try:
        with (
            shared_runtime_module._group_lifecycle_lock(  # pyright: ignore[reportPrivateUsage]
                paths
            ),
            pytest.raises(SharedRuntimeContractError) as exc_info,
        ):
            shared_runtime_module._existing_broker_ready(  # pyright: ignore[reportPrivateUsage]
                paths
            )
        assert exc_info.value.reason_code == "shared_runtime.broker_receipt_invalid"
    finally:
        listener.close()
        paths.socket.unlink(missing_ok=True)


def test_existing_broker_receipt_requires_credential_bound_hmac(
    short_runtime_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "BRIDGE_DB_SHARED_RUNTIME_ROOT", str(short_runtime_root / "shared")
    )
    monkeypatch.setenv(
        "BRIDGE_DB_PRINCIPAL_TOKEN", "fixture-shared-secret-abcdefghijklmnopqrstuvwxyz"
    )
    monkeypatch.setenv("BRIDGE_DB_GENERATION_ID", "generation-one")
    paths = shared_runtime_paths()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(paths.socket))
    listener.listen(1)
    forged_record = _broker_receipt(paths)
    forged_record["receipt_hmac_sha256"] = "0" * 64
    shared_runtime_module._atomic_json(  # pyright: ignore[reportPrivateUsage]
        paths.broker_receipt,
        forged_record,
        replace=False,
    )

    try:
        with (
            shared_runtime_module._group_lifecycle_lock(  # pyright: ignore[reportPrivateUsage]
                paths
            ),
            pytest.raises(SharedRuntimeContractError) as exc_info,
        ):
            shared_runtime_module._existing_broker_ready(  # pyright: ignore[reportPrivateUsage]
                paths
            )
        assert exc_info.value.reason_code == "shared_runtime.broker_receipt_invalid"
    finally:
        listener.close()
        paths.socket.unlink(missing_ok=True)


def test_relay_request_rejects_socket_replacement_after_receipt_validation(
    short_runtime_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "BRIDGE_DB_SHARED_RUNTIME_ROOT", str(short_runtime_root / "shared")
    )
    monkeypatch.setenv(
        "BRIDGE_DB_PRINCIPAL_TOKEN", "fixture-shared-secret-abcdefghijklmnopqrstuvwxyz"
    )
    monkeypatch.setenv("BRIDGE_DB_GENERATION_ID", "generation-one")
    paths = shared_runtime_paths()
    original_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    original_listener.bind(str(paths.socket))
    original_listener.listen(1)
    broker_record = _broker_receipt(paths)
    shared_runtime_module._atomic_json(  # pyright: ignore[reportPrivateUsage]
        paths.broker_receipt,
        broker_record,
        replace=False,
    )
    with shared_runtime_module._group_lifecycle_lock(  # pyright: ignore[reportPrivateUsage]
        paths
    ):
        lease, _capability_file = shared_runtime_module._register_client(  # pyright: ignore[reportPrivateUsage]
            paths,
            owner_pid=os.getpid(),
        )
    original_listener.close()
    paths.socket.unlink()
    replacement_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement_listener.bind(str(paths.socket))
    replacement_listener.listen(1)

    try:
        with pytest.raises(SharedRuntimeContractError) as exc_info:
            shared_runtime_module._http_request_to_broker(  # pyright: ignore[reportPrivateUsage]
                paths,
                method="POST",
                body=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
                headers=shared_runtime_module._relay_request_headers(  # pyright: ignore[reportPrivateUsage]
                    lease,
                    session_id=None,
                    protocol_version=None,
                ),
            )
        assert exc_info.value.reason_code == "shared_runtime.socket_identity_mismatch"
    finally:
        replacement_listener.close()
        paths.socket.unlink(missing_ok=True)


def test_relay_request_rejects_socket_swap_restored_before_path_readback(
    short_runtime_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "BRIDGE_DB_SHARED_RUNTIME_ROOT", str(short_runtime_root / "shared")
    )
    monkeypatch.setenv(
        "BRIDGE_DB_PRINCIPAL_TOKEN", "fixture-shared-secret-abcdefghijklmnopqrstuvwxyz"
    )
    monkeypatch.setenv("BRIDGE_DB_GENERATION_ID", "generation-one")
    paths = shared_runtime_paths()
    original_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    original_listener.bind(str(paths.socket))
    original_listener.listen(1)
    broker_record = _broker_receipt(paths)
    shared_runtime_module._atomic_json(  # pyright: ignore[reportPrivateUsage]
        paths.broker_receipt,
        broker_record,
        replace=False,
    )
    saved_original = paths.group / "bridge.sock.original"
    os.rename(paths.socket, saved_original)
    replacement = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import socket, sys, time\n"
                "listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
                "listener.bind(sys.argv[1])\n"
                "listener.listen(1)\n"
                "print('ready', flush=True)\n"
                "conn, _ = listener.accept()\n"
                "time.sleep(1)\n"
                "conn.close()\n"
                "listener.close()\n"
            ),
            str(paths.socket),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert replacement.stdout is not None
    assert replacement.stdout.readline().strip() == "ready"
    original_socket_identity = shared_runtime_module._socket_identity  # pyright: ignore[reportPrivateUsage]
    armed = True

    def restored_socket_identity(path: Path) -> dict[str, int]:
        nonlocal armed
        if armed:
            armed = False
            path.unlink()
            os.rename(saved_original, path)
        return original_socket_identity(path)

    monkeypatch.setattr(
        shared_runtime_module,
        "_socket_identity",
        restored_socket_identity,
    )

    try:
        with pytest.raises(SharedRuntimeContractError) as exc_info:
            shared_runtime_module._connect_verified_broker_socket(  # pyright: ignore[reportPrivateUsage]
                paths,
                broker_record,
                timeout=1.0,
            )
        assert exc_info.value.reason_code == (
            "shared_runtime.socket_peer_identity_mismatch"
        )
    finally:
        replacement.terminate()
        replacement.communicate(timeout=5)
        original_listener.close()
        paths.socket.unlink(missing_ok=True)


def test_broker_start_refuses_an_unreceipted_existing_socket(
    tmp_path: Path,
    short_runtime_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = tmp_path / "bridge-db-mcp"
    launcher.write_text(
        f"#!/bin/sh\nexec {sys.executable!s} -m bridge_db \"$@\"\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    monkeypatch.setenv(
        "BRIDGE_DB_SHARED_RUNTIME_ROOT", str(short_runtime_root / "shared")
    )
    monkeypatch.setenv(
        "BRIDGE_DB_PRINCIPAL_TOKEN",
        "fixture-shared-secret-abcdefghijklmnopqrstuvwxyz",
    )
    monkeypatch.setenv("BRIDGE_DB_AUTH_MODE", "off")
    monkeypatch.setattr(config, "AUTH_MODE", "off")
    contract_paths = {
        "DB_PATH": ("BRIDGE_DB_PATH", tmp_path / "bridge.db"),
        "BRIDGE_FILE_PATH": ("BRIDGE_FILE_PATH", tmp_path / "bridge.md"),
        "PRINCIPALS_PATH": (
            "BRIDGE_DB_PRINCIPALS_PATH",
            tmp_path / "principals.json",
        ),
        "AUDIT_LOG_PATH": ("BRIDGE_DB_AUDIT_LOG_PATH", tmp_path / "audit.jsonl"),
        "AUDIT_FAILURE_LOG_PATH": (
            "BRIDGE_DB_AUDIT_FAILURE_LOG_PATH",
            tmp_path / "audit-failures.jsonl",
        ),
        "EVIDENCE_ACK_LOG_PATH": (
            "BRIDGE_DB_EVIDENCE_ACK_LOG_PATH",
            tmp_path / "evidence-ack.jsonl",
        ),
        "EVIDENCE_DISPOSITION_LOG_PATH": (
            "BRIDGE_DB_EVIDENCE_DISPOSITION_LOG_PATH",
            tmp_path / "evidence-disposition.jsonl",
        ),
        "PROJECT_REGISTRY_PATH": (
            "BRIDGE_DB_PROJECT_REGISTRY_PATH",
            tmp_path / "project-registry.json",
        ),
        "META_SHIPPED_EVENTS_PATH": (
            "BRIDGE_DB_META_SHIPPED_EVENTS_PATH",
            tmp_path / "meta-shipped-events.json",
        ),
    }
    for config_name, (environment_name, value) in contract_paths.items():
        monkeypatch.setattr(config, config_name, value)
        monkeypatch.setenv(environment_name, str(value))
    monkeypatch.setenv("BRIDGE_DB_TENANCY_ROOT", str(tmp_path / "tenancy"))
    monkeypatch.setenv("BRIDGE_DB_SHARED_RUNTIME_LAUNCHER", str(launcher))
    paths = shared_runtime_paths()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(paths.socket))
    listener.listen(1)

    try:
        with pytest.raises(SharedRuntimeContractError) as exc_info:
            shared_runtime_module._start_broker(  # pyright: ignore[reportPrivateUsage]
                paths
            )
        assert exc_info.value.reason_code == "shared_runtime.broker_start_failed"
        assert not paths.broker_receipt.exists()
    finally:
        listener.close()
        paths.socket.unlink(missing_ok=True)


def test_generation_manifest_launcher_preempts_inherited_launcher_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "release"
    launcher = release / "bin" / "bridge-db-mcp"
    manifest = release / "generation-manifest.json"
    malicious = tmp_path / "malicious-launcher"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    manifest.write_text("{}\n", encoding="utf-8")
    malicious.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    malicious.chmod(0o755)
    monkeypatch.setenv("BRIDGE_DB_GENERATION_MANIFEST", str(manifest))
    monkeypatch.setenv("BRIDGE_DB_SHARED_RUNTIME_LAUNCHER", str(malicious))

    assert shared_runtime_module._launcher_path() == launcher  # pyright: ignore[reportPrivateUsage]
    assert (
        shared_runtime_module._launch_contract()["runtime_source"]  # pyright: ignore[reportPrivateUsage]
        == str(launcher)
    )


def test_release_waits_for_inflight_lease_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDGE_DB_SHARED_RUNTIME_ROOT", str(tmp_path / "shared"))
    monkeypatch.setenv(
        "BRIDGE_DB_PRINCIPAL_TOKEN", "fixture-shared-secret-abcdefghijklmnopqrstuvwxyz"
    )
    monkeypatch.setenv("BRIDGE_DB_GENERATION_ID", "generation-one")
    paths = shared_runtime_paths()
    with shared_runtime_module._group_lifecycle_lock(  # pyright: ignore[reportPrivateUsage]
        paths
    ):
        lease, capability_file = shared_runtime_module._register_client(  # pyright: ignore[reportPrivateUsage]
            paths
        )

    original_read_json = shared_runtime_module._read_json  # pyright: ignore[reportPrivateUsage]
    scan_entered = threading.Event()
    allow_scan = threading.Event()
    release_finished = threading.Event()
    failures: list[BaseException] = []
    counts: list[int] = []

    def blocking_read_json(path: Path) -> dict[str, object]:
        if threading.current_thread().name == "shared-runtime-scan" and path == lease:
            scan_entered.set()
            if not allow_scan.wait(timeout=2):
                raise AssertionError("lease scan was not released")
        return original_read_json(path)

    monkeypatch.setattr(shared_runtime_module, "_read_json", blocking_read_json)

    def scan() -> None:
        try:
            with shared_runtime_module._group_lifecycle_lock(  # pyright: ignore[reportPrivateUsage]
                paths
            ):
                counts.append(
                    shared_runtime_module._live_client_count_unlocked(  # pyright: ignore[reportPrivateUsage]
                        paths
                    )
                )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    def release() -> None:
        try:
            shared_runtime_module.release_shared_client(lease)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            release_finished.set()

    scan_thread = threading.Thread(target=scan, name="shared-runtime-scan")
    release_thread = threading.Thread(target=release, name="shared-runtime-release")
    scan_thread.start()
    assert scan_entered.wait(timeout=2)
    release_thread.start()
    assert not release_finished.wait(timeout=0.05)
    allow_scan.set()
    scan_thread.join(timeout=2)
    release_thread.join(timeout=2)

    assert not scan_thread.is_alive()
    assert not release_thread.is_alive()
    assert failures == []
    assert counts == [1]
    assert not lease.exists()
    assert not capability_file.exists()


def test_request_capability_is_single_use_and_hash_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDGE_DB_SHARED_RUNTIME_ROOT", str(tmp_path / "shared"))
    monkeypatch.setenv(
        "BRIDGE_DB_PRINCIPAL_TOKEN",
        "fixture-shared-secret-abcdefghijklmnopqrstuvwxyz",
    )
    paths = shared_runtime_paths()
    with shared_runtime_module._group_lifecycle_lock(  # pyright: ignore[reportPrivateUsage]
        paths
    ):
        lease, capability_file = shared_runtime_module._register_client(  # pyright: ignore[reportPrivateUsage]
            paths
        )
    capability_header = shared_runtime_module.renew_shared_capability(  # pyright: ignore[reportPrivateUsage]
        lease
    )
    capability_value = capability_header.encode("ascii").split(b": ", 1)[1]

    assert shared_runtime_module._relay_capability_authorized(  # pyright: ignore[reportPrivateUsage]
        paths, capability_value
    )

    assert not shared_runtime_module._relay_capability_authorized(  # pyright: ignore[reportPrivateUsage]
        paths, capability_value
    )
    assert not capability_file.exists()


def test_request_capability_renewal_requires_exact_relay_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDGE_DB_SHARED_RUNTIME_ROOT", str(tmp_path / "shared"))
    monkeypatch.setenv(
        "BRIDGE_DB_PRINCIPAL_TOKEN",
        "fixture-shared-secret-abcdefghijklmnopqrstuvwxyz",
    )
    paths = shared_runtime_paths()
    with shared_runtime_module._group_lifecycle_lock(  # pyright: ignore[reportPrivateUsage]
        paths
    ):
        lease, _capability_file = shared_runtime_module._register_client(  # pyright: ignore[reportPrivateUsage]
            paths
        )

    def mismatched_parent(_pid: int) -> str:
        return "fixture-different-parent"

    monkeypatch.setattr(
        shared_runtime_module,
        "process_identity",
        mismatched_parent,
    )

    with pytest.raises(SharedRuntimeContractError) as exc_info:
        shared_runtime_module.renew_shared_capability(lease)  # pyright: ignore[reportPrivateUsage]

    assert exc_info.value.reason_code == (
        "shared_runtime.capability_renew_identity_mismatch"
    )
    inventory = shared_runtime_inventory(paths.root)
    assert inventory["state"] == "observed"
    assert inventory["capability_file_count"] == 0


def test_request_capability_requires_live_exact_relay_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDGE_DB_SHARED_RUNTIME_ROOT", str(tmp_path / "shared"))
    monkeypatch.setenv(
        "BRIDGE_DB_PRINCIPAL_TOKEN",
        "fixture-shared-secret-abcdefghijklmnopqrstuvwxyz",
    )
    paths = shared_runtime_paths()
    with shared_runtime_module._group_lifecycle_lock(  # pyright: ignore[reportPrivateUsage]
        paths
    ):
        lease, _capability_file = shared_runtime_module._register_client(  # pyright: ignore[reportPrivateUsage]
            paths
        )
    capability_header = shared_runtime_module.renew_shared_capability(  # pyright: ignore[reportPrivateUsage]
        lease
    )
    capability_value = capability_header.encode("ascii").split(b": ", 1)[1]

    def mismatched_process(_pid: int, _identity: str) -> str:
        return "mismatch"

    monkeypatch.setattr(
        shared_runtime_module,
        "probe_process",
        mismatched_process,
    )

    assert not shared_runtime_module._relay_capability_authorized(  # pyright: ignore[reportPrivateUsage]
        paths, capability_value
    )


@pytest.mark.asyncio
async def test_monitor_failure_requests_shutdown_and_propagates() -> None:
    class _Server:
        should_exit = False

    server = _Server()

    async def serve() -> None:
        while not server.should_exit:
            await asyncio.sleep(0)

    async def fail_monitor() -> None:
        await asyncio.sleep(0)
        raise SharedRuntimeContractError("shared_runtime.fixture_monitor_failed")

    serve_task = asyncio.create_task(serve())
    monitor_task = asyncio.create_task(fail_monitor())
    with pytest.raises(SharedRuntimeContractError) as exc_info:
        await shared_runtime_module._await_broker_tasks(  # pyright: ignore[reportPrivateUsage]
            server, serve_task, {monitor_task}, shutdown_timeout=1
        )

    assert exc_info.value.reason_code == "shared_runtime.fixture_monitor_failed"
    assert server.should_exit is True


def test_shared_wrapper_relays_mcp_over_one_idle_bounded_broker(
    tmp_path: Path, short_runtime_root: Path
) -> None:
    launcher = tmp_path / "bridge-db-mcp"
    launcher.write_text(
        f"#!/bin/sh\nexec {sys.executable!s} -m bridge_db \"$@\"\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    wrapper_source = (
        _REPO_ROOT / "config" / "bridge-db-mcp-immutable"
    ).read_text(encoding="utf-8")
    wrapper = tmp_path / "bridge-db-mcp-immutable"
    wrapper.write_text(
        wrapper_source.replace(_STABLE_LAUNCHER, str(launcher)), encoding="utf-8"
    )
    wrapper.chmod(0o755)
    env_file = tmp_path / "bridge-db.env"
    env_file.write_text(
        "BRIDGE_DB_PRINCIPAL_TOKEN=fixture-shared-secret-abcdefghijklmnopqrstuvwxyz\n"
        "BRIDGE_DB_AUTH_MODE=off\n"
        "BRIDGE_DB_TRANSPORT_MODE=shared\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    shared_root = short_runtime_root / "shared"
    environment = {
        **os.environ,
        "PATH": os.pathsep.join(
            (str(_noisy_gnu_stat_shim(tmp_path)), os.environ.get("PATH", ""))
        ),
        "BRIDGE_DB_ENV_FILE": str(env_file),
        "BRIDGE_DB_PATH": str(tmp_path / "bridge.db"),
        "BRIDGE_FILE_PATH": str(tmp_path / "bridge.md"),
        "BRIDGE_DB_PRINCIPALS_PATH": str(tmp_path / "principals.json"),
        "BRIDGE_DB_TENANCY_ROOT": str(tmp_path / "tenancy"),
        "BRIDGE_DB_SHARED_RUNTIME_ROOT": str(shared_root),
        "BRIDGE_DB_SHARED_RUNTIME_LAUNCHER": str(launcher),
        "BRIDGE_DB_BROKER_START_TIMEOUT_SECONDS": "20",
        "BRIDGE_DB_BROKER_IDLE_SECONDS": "1",
        "BRIDGE_DB_GENERATION_ID": "fixture-generation",
    }
    initialize: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "shared-runtime-test", "version": "1"},
        },
    }
    initialized: dict[str, object] = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }
    tools_list: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }
    messages: tuple[dict[str, object], ...] = (initialize, initialized, tools_list)
    input_text = "".join(
        json.dumps(message, separators=(",", ":")) + "\n"
        for message in messages
    )

    processes = [
        subprocess.Popen(
            [str(wrapper)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        for _ in range(2)
    ]
    registration_deadline = time.monotonic() + 20
    while time.monotonic() < registration_deadline:
        if (
            len(list(shared_root.glob("*/clients/*.json"))) == 2
            and len(list(shared_root.glob("*/broker.json"))) == 1
        ):
            break
        time.sleep(0.05)
    assert len(list(shared_root.glob("*/clients/*.json"))) == 2
    assert len(list(shared_root.glob("*/broker.json"))) == 1
    capability_files = list(shared_root.glob("*/capabilities/*.header"))
    assert capability_files == []

    broker_socket = next(shared_root.glob("*/bridge.sock"))
    unauthorized = subprocess.run(
        [
            "curl",
            "--silent",
            "--unix-socket",
            str(broker_socket),
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code}",
            "--header",
            "Accept: application/json, text/event-stream",
            "--header",
            "Content-Type: application/json",
            "--data-binary",
            "@-",
            "http://localhost/mcp",
        ],
        input=json.dumps(initialize, separators=(",", ":")),
        capture_output=True,
        text=True,
        check=False,
    )
    assert unauthorized.returncode == 0
    assert unauthorized.stdout == "401"
    live_inventory = shared_runtime_inventory(shared_root)
    assert live_inventory["state"] == "observed"
    assert live_inventory["adoption_state"] == "active"
    assert live_inventory["group_count"] == 1
    assert live_inventory["ready_broker_count"] == 1
    assert live_inventory["live_client_count"] == 2
    assert live_inventory["capability_file_count"] == 0
    assert live_inventory["orphan_capability_file_count"] == 0
    assert live_inventory["auth_modes"] == {"off": 1}

    for process in processes:
        assert process.stdin is not None
        process.stdin.write(input_text)
        process.stdin.close()

    for process in processes:
        assert process.stdout is not None
        assert process.stderr is not None
        stdout = process.stdout.read()
        stderr = process.stderr.read()
        process.wait(timeout=20)
        assert process.returncode == 0, stderr
        responses = [json.loads(line) for line in stdout.splitlines()]
        assert [response["id"] for response in responses] == [1, 2]
        assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
        assert any(
            tool["name"] == "health" for tool in responses[1]["result"]["tools"]
        )

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        broker_receipts = list(shared_root.glob("*/broker.json"))
        sockets = list(shared_root.glob("*/bridge.sock"))
        if not broker_receipts and not sockets:
            break
        time.sleep(0.1)
    assert list(shared_root.glob("*/broker.json")) == []
    assert list(shared_root.glob("*/bridge.sock")) == []
    stopped_inventory = shared_runtime_inventory(shared_root)
    assert stopped_inventory["state"] == "observed"
    assert stopped_inventory["adoption_state"] == "inactive"
    assert stopped_inventory["live_broker_count"] == 0
    assert stopped_inventory["client_lease_count"] == 0
    assert stopped_inventory["capability_file_count"] == 0
    broker_logs = list(shared_root.glob("*/broker.log"))
    assert len(broker_logs) == 1
    broker_log_text = broker_logs[0].read_text(encoding="utf-8")
    history_text = "".join(
        path.read_text(encoding="utf-8")
        for path in shared_root.glob("*/history/*.json")
    )
    assert "X-Bridge-Relay-Capability" not in broker_log_text
    assert "X-Bridge-Relay-Capability" not in history_text
    client_history = list(shared_root.glob("*/history/*.json"))
    assert any(
        json.loads(path.read_text(encoding="utf-8")).get("lifecycle_reason")
        == "relay_normal_close"
        for path in client_history
    )
