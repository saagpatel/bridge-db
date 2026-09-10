"""Exact Claude JSON launcher rebinding tests use secret fixtures only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import bridge_db.client_rebinding as client_rebinding
from bridge_db.auth import hash_token
from bridge_db.client_rebinding import (
    IMMUTABLE_LAUNCHER,
    ClientRebindingError,
    rebind_claude_launcher,
    restore_claude_launcher,
)


def _config(
    tmp_path: Path,
    *,
    client: str = "claude-code",
    secret: str = "fixture-claude-secret-abcdefghijklmnopqrstuvwxyz",
) -> tuple[Path, Path, bytes]:
    config_dir = tmp_path / "config"
    config_dir.mkdir(mode=0o700)
    name = ".claude.json" if client == "claude-code" else "claude_desktop_config.json"
    path = config_dir / name
    document = {
        "mcpServers": {
            "bridge-db": {
                "command": "uv",
                "args": [
                    "run",
                    "--directory",
                    "/Users/d/Projects/bridge-db",
                    "python",
                    "-m",
                    "bridge_db",
                ],
                "env": {
                    "BRIDGE_DB_AUTH_MODE": "warn",
                    "BRIDGE_DB_PRINCIPAL_TOKEN": secret,
                },
            },
            "unrelated": {"command": "fixture", "args": ["keep"]},
        },
        "unrelatedTopLevel": {"preserved": True},
    }
    original = (json.dumps(document, indent=4) + "\n").encode("utf-8")
    path.write_bytes(original)
    path.chmod(0o600)
    return path, tmp_path / "backups", original


@pytest.mark.parametrize("client", ["claude-code", "claude-desktop"])
def test_rebind_preserves_environment_and_writes_private_exact_backup(
    tmp_path: Path, client: str
) -> None:
    secret = f"fixture-{client}-secret-abcdefghijklmnopqrstuvwxyz"
    path, backup_root, original = _config(tmp_path, client=client, secret=secret)

    receipt = rebind_claude_launcher(
        client=client,  # type: ignore[arg-type]
        config_path=path,
        backup_root=backup_root,
    )

    updated = json.loads(path.read_text(encoding="utf-8"))
    bridge = updated["mcpServers"]["bridge-db"]
    assert bridge["command"] == str(IMMUTABLE_LAUNCHER)
    assert bridge["args"] == []
    assert bridge["env"] == {
        "BRIDGE_DB_AUTH_MODE": "warn",
        "BRIDGE_DB_PRINCIPAL_TOKEN": secret,
    }
    assert updated["mcpServers"]["unrelated"] == {
        "command": "fixture",
        "args": ["keep"],
    }
    assert path.stat().st_mode & 0o777 == 0o600
    backup = Path(str(receipt["backup_path"]))
    assert backup.read_bytes() == original
    assert backup.stat().st_mode & 0o777 == 0o400
    assert backup_root.stat().st_mode & 0o777 == 0o700
    serialized = json.dumps(receipt, sort_keys=True)
    assert secret not in serialized
    assert hash_token(secret) not in serialized
    assert receipt["environment_value_output"] == "none"


def test_rebind_refuses_non_exact_legacy_invocation(tmp_path: Path) -> None:
    path, backup_root, original = _config(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["mcpServers"]["bridge-db"]["args"].append("--unexpected")
    changed = (json.dumps(document, indent=2) + "\n").encode("utf-8")
    path.write_bytes(changed)
    path.chmod(0o600)

    with pytest.raises(ClientRebindingError) as refused:
        rebind_claude_launcher(
            client="claude-code", config_path=path, backup_root=backup_root
        )

    assert refused.value.reason_code == "client_rebind.legacy_launcher_mismatch"
    assert path.read_bytes() == changed
    assert not backup_root.exists()
    assert original != changed


def test_rebind_restores_original_after_caught_post_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, backup_root, original = _config(tmp_path)
    original_fsync = client_rebinding._fsync_directory  # pyright: ignore[reportPrivateUsage]
    calls = 0

    def fail_first_config_fsync(selected: Path) -> None:
        nonlocal calls
        if selected == path.parent:
            calls += 1
            if calls == 1:
                raise OSError("fixture post-replace fsync failure")
        original_fsync(selected)

    monkeypatch.setattr(client_rebinding, "_fsync_directory", fail_first_config_fsync)

    with pytest.raises(ClientRebindingError) as failed:
        rebind_claude_launcher(
            client="claude-code", config_path=path, backup_root=backup_root
        )

    assert failed.value.reason_code == "client_rebind.replace_failed"
    assert path.read_bytes() == original
    assert path.stat().st_mode & 0o777 == 0o600


def test_explicit_restore_is_digest_bound_and_exact(tmp_path: Path) -> None:
    path, backup_root, original = _config(tmp_path)
    rebound = rebind_claude_launcher(
        client="claude-code", config_path=path, backup_root=backup_root
    )
    current = path.read_bytes()

    restored = restore_claude_launcher(
        client="claude-code",
        config_path=path,
        backup_path=Path(str(rebound["backup_path"])),
        expected_current_sha256=str(rebound["current_config_sha256"]),
    )

    assert restored["outcome"] == "exact_backup_restored"
    assert restored["environment_value_output"] == "none"
    assert path.read_bytes() == original
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ClientRebindingError) as stale:
        restore_claude_launcher(
            client="claude-code",
            config_path=path,
            backup_path=Path(str(rebound["backup_path"])),
            expected_current_sha256=str(rebound["current_config_sha256"]),
        )
    assert stale.value.reason_code == "client_restore.current_digest_mismatch"
    assert current != original
