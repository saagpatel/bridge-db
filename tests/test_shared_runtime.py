"""Shared broker and thin stdio relay contract tests."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from bridge_db.shared_runtime import shared_runtime_paths

_REPO_ROOT = Path(__file__).parents[1]
_STABLE_LAUNCHER = "/Users/d/.local/state/bridge-db/current/bin/bridge-db-mcp"


@pytest.fixture
def short_runtime_root() -> Iterator[Path]:
    # Darwin limits AF_UNIX paths to roughly 104 bytes; pytest's nested tmp path
    # is intentionally long, so the transport root gets its own private short path.
    root = Path(tempfile.mkdtemp(prefix="bridge-shared-", dir="/private/tmp"))
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
    monkeypatch.setenv("BRIDGE_DB_GENERATION_ID", "generation-one")
    first = shared_runtime_paths()
    monkeypatch.setenv("BRIDGE_DB_GENERATION_ID", "generation-two")
    second = shared_runtime_paths()

    assert first.group != second.group
    assert first.group.parent == second.group.parent == tmp_path / "shared"
    for path in (first.root, first.group, first.clients, first.history, second.group):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


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
        "BRIDGE_DB_ENV_FILE": str(env_file),
        "BRIDGE_DB_PATH": str(tmp_path / "bridge.db"),
        "BRIDGE_FILE_PATH": str(tmp_path / "bridge.md"),
        "BRIDGE_DB_PRINCIPALS_PATH": str(tmp_path / "principals.json"),
        "BRIDGE_DB_TENANCY_ROOT": str(tmp_path / "tenancy"),
        "BRIDGE_DB_SHARED_RUNTIME_ROOT": str(shared_root),
        "BRIDGE_DB_SHARED_RUNTIME_LAUNCHER": str(launcher),
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
    registration_deadline = time.monotonic() + 10
    while time.monotonic() < registration_deadline:
        if (
            len(list(shared_root.glob("*/clients/*.json"))) == 2
            and len(list(shared_root.glob("*/broker.json"))) == 1
        ):
            break
        time.sleep(0.05)
    assert len(list(shared_root.glob("*/clients/*.json"))) == 2
    assert len(list(shared_root.glob("*/broker.json"))) == 1

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
    client_history = list(shared_root.glob("*/history/*.json"))
    assert any(
        json.loads(path.read_text(encoding="utf-8")).get("lifecycle_reason")
        == "relay_normal_close"
        for path in client_history
    )
