"""Source-owned immutable client and checkpoint launch input tests."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[1]
_STABLE_LAUNCHER = "/Users/d/.local/state/bridge-db/current/bin/bridge-db-mcp"


def test_checkpoint_launch_agent_uses_receipt_wrapper_and_stable_launcher() -> None:
    path = _REPO_ROOT / "config" / "com.saagar.bridge-db-checkpoint.plist"
    with path.open("rb") as handle:
        payload = plistlib.load(handle)

    assert payload["Label"] == "com.saagar.bridge-db-checkpoint"
    assert payload["StartInterval"] == 1800
    assert payload["RunAtLoad"] is True
    assert payload["ProgramArguments"] == [
        "/bin/bash",
        "/Users/d/.local/state/operator-scripts/current/run-with-receipt.sh",
        "--automation-id",
        "com.saagar.bridge-db-checkpoint",
        "--expect-stdout",
        "Overall: truncated",
        "--",
        _STABLE_LAUNCHER,
        "--checkpoint",
    ]
    serialized = path.read_text(encoding="utf-8")
    assert "/Users/d/Projects/bridge-db" not in serialized
    assert "/Users/d/.local/share/launchd-fleet/run-with-receipt.sh" not in serialized
    assert "<string>uv</string>" not in serialized


def _executable_wrapper(tmp_path: Path, launcher: Path) -> Path:
    source = (_REPO_ROOT / "config" / "bridge-db-mcp-immutable").read_text(
        encoding="utf-8"
    )
    rendered = source.replace(_STABLE_LAUNCHER, str(launcher))
    assert rendered != source
    wrapper = tmp_path / "bridge-db-mcp-immutable"
    wrapper.write_text(rendered, encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def test_codex_wrapper_parses_only_exact_private_keys_and_forwards_args(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    launcher = tmp_path / "launcher"
    launcher.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n%s\\n%s\\n' \"$BRIDGE_DB_PRINCIPAL_TOKEN\" "
        "\"$BRIDGE_DB_AUTH_MODE\" \"$*\" > \"$FIXTURE_OUTPUT\"\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    wrapper = _executable_wrapper(tmp_path, launcher)
    secret = "fixture-wrapper-secret-abcdefghijklmnopqrstuvwxyz"
    env_file = tmp_path / "bridge-db.env"
    env_file.write_text(
        f"BRIDGE_DB_PRINCIPAL_TOKEN={secret}\nBRIDGE_DB_AUTH_MODE=warn\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    result = subprocess.run(
        [str(wrapper), "--checkpoint"],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "BRIDGE_DB_ENV_FILE": str(env_file),
            "FIXTURE_OUTPUT": str(output),
        },
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert output.read_text(encoding="utf-8").splitlines() == [
        secret,
        "warn",
        "--checkpoint",
    ]


def test_codex_wrapper_defaults_no_argument_launch_to_direct_transport(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    launcher = tmp_path / "launcher"
    launcher.write_text(
        "#!/bin/sh\nprintf '%s\\n%s\\n' \"$BRIDGE_DB_TRANSPORT_MODE\" \"$#\" > \"$FIXTURE_OUTPUT\"\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    wrapper = _executable_wrapper(tmp_path, launcher)
    env_file = tmp_path / "bridge-db.env"
    env_file.write_text(
        "BRIDGE_DB_PRINCIPAL_TOKEN=fixture-wrapper-secret-abcdefghijklmnopqrstuvwxyz\n"
        "BRIDGE_DB_AUTH_MODE=warn\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    result = subprocess.run(
        [str(wrapper)],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "BRIDGE_DB_ENV_FILE": str(env_file),
            "FIXTURE_OUTPUT": str(output),
        },
    )

    assert result.returncode == 0
    assert output.read_text(encoding="utf-8").splitlines() == ["direct", "0"]


def test_codex_wrapper_forces_direct_transport_for_cli_maintenance_passthrough(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    launcher = tmp_path / "launcher"
    launcher.write_text(
        "#!/bin/sh\nprintf '%s\\n%s\\n' \"$BRIDGE_DB_TRANSPORT_MODE\" \"$*\" > \"$FIXTURE_OUTPUT\"\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    wrapper = _executable_wrapper(tmp_path, launcher)
    env_file = tmp_path / "bridge-db.env"
    env_file.write_text(
        "BRIDGE_DB_PRINCIPAL_TOKEN=fixture-wrapper-secret-abcdefghijklmnopqrstuvwxyz\n"
        "BRIDGE_DB_AUTH_MODE=warn\n"
        "BRIDGE_DB_TRANSPORT_MODE=shared\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    result = subprocess.run(
        [str(wrapper), "--checkpoint"],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "BRIDGE_DB_ENV_FILE": str(env_file),
            "FIXTURE_OUTPUT": str(output),
        },
    )

    assert result.returncode == 0
    assert output.read_text(encoding="utf-8").splitlines() == [
        "direct",
        "--checkpoint",
    ]


@pytest.mark.parametrize(
    "hidden_command",
    ["--ensure-shared-broker", "--run-shared-broker", "--release-shared-client"],
)
def test_codex_wrapper_preserves_shared_transport_for_hidden_lifecycle_commands(
    tmp_path: Path, hidden_command: str
) -> None:
    output = tmp_path / "output"
    launcher = tmp_path / "launcher"
    launcher.write_text(
        "#!/bin/sh\nprintf '%s\\n%s\\n' \"$BRIDGE_DB_TRANSPORT_MODE\" \"$*\" > \"$FIXTURE_OUTPUT\"\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    wrapper = _executable_wrapper(tmp_path, launcher)
    env_file = tmp_path / "bridge-db.env"
    env_file.write_text(
        "BRIDGE_DB_PRINCIPAL_TOKEN=fixture-wrapper-secret-abcdefghijklmnopqrstuvwxyz\n"
        "BRIDGE_DB_AUTH_MODE=warn\n"
        "BRIDGE_DB_TRANSPORT_MODE=shared\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    arguments = [hidden_command]
    if hidden_command == "--release-shared-client":
        arguments.append(str(tmp_path / "client.json"))

    result = subprocess.run(
        [str(wrapper), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "BRIDGE_DB_ENV_FILE": str(env_file),
            "FIXTURE_OUTPUT": str(output),
        },
    )

    assert result.returncode == 0
    assert output.read_text(encoding="utf-8").splitlines() == [
        "shared",
        " ".join(arguments),
    ]


def test_codex_wrapper_rejects_malformed_stat_owner_without_launching(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "launcher-ran"
    launcher = tmp_path / "launcher"
    launcher.write_text(f"#!/bin/sh\ntouch {str(marker)!r}\n", encoding="utf-8")
    launcher.chmod(0o755)
    wrapper = _executable_wrapper(tmp_path, launcher)
    env_file = tmp_path / "bridge-db.env"
    env_file.write_text(
        "BRIDGE_DB_PRINCIPAL_TOKEN=fixture-wrapper-secret-abcdefghijklmnopqrstuvwxyz\n"
        "BRIDGE_DB_AUTH_MODE=warn\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_stat = fake_bin / "stat"
    fake_stat.write_text(
        "#!/bin/sh\n"
        "case \"$2\" in\n"
        "    '%u') printf '%s\\n' not-a-uid ;;\n"
        "    '%Lp'|'%a') printf '%s\\n' 600 ;;\n"
        "    *) exit 64 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_stat.chmod(0o755)

    result = subprocess.run(
        [str(wrapper), "--checkpoint"],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "BRIDGE_DB_ENV_FILE": str(env_file),
            "PATH": os.pathsep.join((str(fake_bin), os.environ.get("PATH", ""))),
        },
    )

    assert result.returncode == 78
    assert result.stdout == ""
    assert result.stderr.strip() == "bridge_db.wrapper_env_file_not_private"
    assert not marker.exists()


def test_codex_wrapper_rejects_malformed_capability_owner_before_relay(
    tmp_path: Path,
) -> None:
    group = tmp_path / "runtime" / "group"
    clients = group / "clients"
    capabilities = group / "capabilities"
    clients.mkdir(parents=True)
    capabilities.mkdir()
    broker_socket = group / "broker.sock"
    client_lease = clients / "client.json"
    capability_file = capabilities / "client.header"
    capability_file.write_text(
        "Authorization: Bearer fixture-capability\n", encoding="utf-8"
    )
    capability_file.chmod(0o400)
    launcher = tmp_path / "launcher"
    launcher.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = --ensure-shared-broker ]; then\n"
        f"    printf '%s\\n' {str(broker_socket)!r} {str(client_lease)!r} "
        f"{str(capability_file)!r} {str(launcher)!r}\n"
        "fi\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    wrapper = _executable_wrapper(tmp_path, launcher)
    env_file = tmp_path / "bridge-db.env"
    env_file.write_text(
        "BRIDGE_DB_PRINCIPAL_TOKEN=fixture-wrapper-secret-abcdefghijklmnopqrstuvwxyz\n"
        "BRIDGE_DB_AUTH_MODE=warn\n"
        "BRIDGE_DB_TRANSPORT_MODE=shared\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_stat = fake_bin / "stat"
    fake_stat.write_text(
        "#!/bin/sh\n"
        "case \"$3\" in\n"
        "    */bridge-db.env) owner=$(id -u); mode=600 ;;\n"
        "    */client.header) owner=not-a-uid; mode=400 ;;\n"
        "    *) exit 64 ;;\n"
        "esac\n"
        "case \"$2\" in\n"
        "    '%u') printf '%s\\n' \"$owner\" ;;\n"
        "    '%Lp'|'%a') printf '%s\\n' \"$mode\" ;;\n"
        "    *) exit 64 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_stat.chmod(0o755)

    result = subprocess.run(
        [str(wrapper)],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "BRIDGE_DB_ENV_FILE": str(env_file),
            "PATH": os.pathsep.join((str(fake_bin), os.environ.get("PATH", ""))),
        },
    )

    assert result.returncode == 69
    assert result.stdout == ""
    assert (
        result.stderr.strip()
        == "bridge_db.shared_relay_capability_file_not_private"
    )
    assert not any(group.glob("relay-*"))


@pytest.mark.parametrize(
    "bad_content",
    [
        "BRIDGE_DB_PRINCIPAL_TOKEN=fixture-secret-abcdefghijklmnopqrstuvwxyz\n"
        "BRIDGE_DB_AUTH_MODE=warn\nUNREVIEWED_KEY=value\n",
        "BRIDGE_DB_PRINCIPAL_TOKEN=fixture-secret-abcdefghijklmnopqrstuvwxyz\n"
        "BRIDGE_DB_PRINCIPAL_TOKEN=duplicate-secret-abcdefghijklmnopqrstuvwxyz\n"
        "BRIDGE_DB_AUTH_MODE=warn\n",
        "BRIDGE_DB_PRINCIPAL_TOKEN=fixture-secret-abcdefghijklmnopqrstuvwxyz\n"
        "BRIDGE_DB_AUTH_MODE=warn\nBRIDGE_DB_TRANSPORT_MODE=surprise\n",
    ],
)
def test_codex_wrapper_refuses_unknown_or_duplicate_keys_without_secret_output(
    tmp_path: Path, bad_content: str
) -> None:
    marker = tmp_path / "launcher-ran"
    launcher = tmp_path / "launcher"
    launcher.write_text(f"#!/bin/sh\ntouch {str(marker)!r}\n", encoding="utf-8")
    launcher.chmod(0o755)
    wrapper = _executable_wrapper(tmp_path, launcher)
    env_file = tmp_path / "bridge-db.env"
    env_file.write_text(bad_content, encoding="utf-8")
    env_file.chmod(0o600)

    result = subprocess.run(
        [str(wrapper)],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "BRIDGE_DB_ENV_FILE": str(env_file)},
    )

    assert result.returncode == 78
    assert not marker.exists()
    assert "fixture-secret" not in result.stderr
    assert "duplicate-secret" not in result.stderr


def test_codex_wrapper_source_is_syntax_valid_and_immutable_path_bound() -> None:
    wrapper = _REPO_ROOT / "config" / "bridge-db-mcp-immutable"
    source = wrapper.read_text(encoding="utf-8")
    result = subprocess.run(
        ["/bin/sh", "-n", str(wrapper)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"stable_launcher={_STABLE_LAUNCHER}" in source
    assert "/Users/d/Projects/bridge-db" not in source
    assert "source " not in source
    assert ". \"$env_file\"" not in source
    assert '--header "@$capability_file"' in source
    assert 'cat "$capability_file"' not in source
