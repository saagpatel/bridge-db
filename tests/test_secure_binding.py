"""No-secret-output principal binding tests use fixtures only."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bridge_db.auth import hash_token, load_principal_grants, resolve_grant
from bridge_db.secure_binding import (
    SecureBindingError,
    bind_principal_from_fd,
    verify_principal_binding,
)


def _targets(tmp_path: Path) -> tuple[Path, Path]:
    registry_dir = tmp_path / "registry"
    binding_dir = tmp_path / "binding"
    registry_dir.mkdir(mode=0o700)
    binding_dir.mkdir(mode=0o700)
    return registry_dir / "principals.json", binding_dir / "bridge-db.env"


def _secret_fd(secret: str) -> int:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, secret.encode("utf-8") + b"\n")
    os.close(write_fd)
    return read_fd


def _bind(
    *, secret: str, registry: Path, binding: Path, caller: str = "codex"
) -> dict[str, object]:
    descriptor = _secret_fd(secret)
    try:
        return bind_principal_from_fd(
            caller=caller,
            secret_fd=descriptor,
            principals_path=registry,
            binding_path=binding,
            auth_mode="warn",
        )
    finally:
        os.close(descriptor)


def test_secure_binding_rotates_and_emits_no_secret_material(tmp_path: Path) -> None:
    registry, binding = _targets(tmp_path)
    first_secret = "fixture-secret-one-abcdefghijklmnopqrstuvwxyz"
    second_secret = "fixture-secret-two-abcdefghijklmnopqrstuvwxyz"

    first = _bind(secret=first_secret, registry=registry, binding=binding)
    second = _bind(secret=second_secret, registry=registry, binding=binding)

    assert first["generation"] == 1
    assert second["generation"] == 2
    assert second["lock_scope"] == "ordered_target_parent_locks"
    assert second["rollback"] == (
        "ordered_locked_replace_with_verified_rollback_on_caught_failure; "
        "crash_between_replaces_requires_retry_or_recovery"
    )
    assert first["secret_output"] == "none"
    serialized = json.dumps([first, second], sort_keys=True)
    assert first_secret not in serialized
    assert second_secret not in serialized
    assert hash_token(first_secret) not in serialized
    assert hash_token(second_secret) not in serialized
    assert registry.stat().st_mode & 0o777 == 0o600
    assert binding.stat().st_mode & 0o777 == 0o600
    grants = load_principal_grants(registry)
    assert hash_token(first_secret) not in grants
    assert grants[hash_token(second_secret)].caller == "codex"
    assert binding.read_text(encoding="utf-8").splitlines() == [
        f"BRIDGE_DB_PRINCIPAL_TOKEN={second_secret}",
        "BRIDGE_DB_AUTH_MODE=warn",
    ]
    for parent in (registry.parent, binding.parent):
        lock = parent / ".bridge-db-secure-binding.lock"
        assert lock.is_file()
        assert lock.stat().st_mode & 0o777 == 0o600


def test_secure_binding_rejects_stdio_and_non_codex_callers(tmp_path: Path) -> None:
    registry, binding = _targets(tmp_path)
    with pytest.raises(SecureBindingError) as stdio:
        bind_principal_from_fd(
            caller="codex",
            secret_fd=0,
            principals_path=registry,
            binding_path=binding,
        )
    assert stdio.value.reason_code == "binding.secret_fd_stdio_refused"

    descriptor = _secret_fd("fixture-secret-abcdefghijklmnopqrstuvwxyz")
    try:
        with pytest.raises(SecureBindingError) as caller:
            bind_principal_from_fd(
                caller="personal_ops",
                secret_fd=descriptor,
                principals_path=registry,
                binding_path=binding,
            )
    finally:
        os.close(descriptor)
    assert caller.value.reason_code == "binding.caller_not_authorized_for_local_binding"


def test_secure_binding_rejects_insecure_regular_secret_file(tmp_path: Path) -> None:
    registry, binding = _targets(tmp_path)
    secret_path = tmp_path / "secret.txt"
    secret_path.write_text(
        "fixture-secret-abcdefghijklmnopqrstuvwxyz", encoding="utf-8"
    )
    secret_path.chmod(0o644)
    descriptor = os.open(secret_path, os.O_RDONLY)
    try:
        with pytest.raises(SecureBindingError) as refused:
            bind_principal_from_fd(
                caller="codex",
                secret_fd=descriptor,
                principals_path=registry,
                binding_path=binding,
            )
    finally:
        os.close(descriptor)
    assert refused.value.reason_code == "binding.secret_fd_mode_invalid"


def test_secure_binding_rejects_symlink_target(tmp_path: Path) -> None:
    registry, binding = _targets(tmp_path)
    real_binding = binding.parent / "real.env"
    real_binding.write_text("fixture\n", encoding="utf-8")
    real_binding.chmod(0o600)
    binding.symlink_to(real_binding)
    descriptor = _secret_fd("fixture-secret-abcdefghijklmnopqrstuvwxyz")
    try:
        with pytest.raises(SecureBindingError) as refused:
            bind_principal_from_fd(
                caller="codex",
                secret_fd=descriptor,
                principals_path=registry,
                binding_path=binding,
            )
    finally:
        os.close(descriptor)
    assert refused.value.reason_code == "binding.target_symlink_or_special_refused"


def test_secure_binding_rejects_non_private_target_parent(tmp_path: Path) -> None:
    registry, binding = _targets(tmp_path)
    binding.parent.chmod(0o755)
    descriptor = _secret_fd("fixture-secret-abcdefghijklmnopqrstuvwxyz")
    try:
        with pytest.raises(SecureBindingError) as refused:
            bind_principal_from_fd(
                caller="codex",
                secret_fd=descriptor,
                principals_path=registry,
                binding_path=binding,
            )
    finally:
        os.close(descriptor)
    assert refused.value.reason_code == "binding.parent_not_private"


def test_secure_binding_rejects_symlinked_lock_in_either_parent(tmp_path: Path) -> None:
    registry, binding = _targets(tmp_path)
    lock_target = tmp_path / "lock-target"
    lock_target.write_text("fixture\n", encoding="utf-8")
    lock_target.chmod(0o600)
    (binding.parent / ".bridge-db-secure-binding.lock").symlink_to(lock_target)
    descriptor = _secret_fd("fixture-secret-abcdefghijklmnopqrstuvwxyz")
    try:
        with pytest.raises(SecureBindingError) as refused:
            bind_principal_from_fd(
                caller="codex",
                secret_fd=descriptor,
                principals_path=registry,
                binding_path=binding,
            )
    finally:
        os.close(descriptor)

    assert refused.value.reason_code == "binding.lock_invalid"
    assert not registry.exists()
    assert not binding.exists()


def test_secure_binding_restores_both_files_after_partial_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, binding = _targets(tmp_path)
    old_secret = "fixture-old-secret-abcdefghijklmnopqrstuvwxyz"
    _bind(secret=old_secret, registry=registry, binding=binding)
    old_registry = registry.read_bytes()
    old_binding = binding.read_bytes()
    original_replace = os.replace
    calls = 0

    def fail_second_replace(source: str | Path, target: str | Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fixture second replace failure")
        original_replace(source, target)

    monkeypatch.setattr("bridge_db.secure_binding.os.replace", fail_second_replace)
    descriptor = _secret_fd("fixture-new-secret-abcdefghijklmnopqrstuvwxyz")
    try:
        with pytest.raises(SecureBindingError) as failed:
            bind_principal_from_fd(
                caller="codex",
                secret_fd=descriptor,
                principals_path=registry,
                binding_path=binding,
            )
    finally:
        os.close(descriptor)

    assert failed.value.reason_code == "binding.atomic_replace_failed"
    assert registry.read_bytes() == old_registry
    assert binding.read_bytes() == old_binding


def test_split_binding_pair_cannot_authenticate_or_read_back_green(
    tmp_path: Path,
) -> None:
    registry, binding = _targets(tmp_path)
    registered_secret = "fixture-registered-secret-abcdefghijklmnopqrstuvwxyz"
    split_secret = "fixture-split-secret-abcdefghijklmnopqrstuvwxyz"
    _bind(secret=registered_secret, registry=registry, binding=binding)
    binding.write_text(
        f"BRIDGE_DB_PRINCIPAL_TOKEN={split_secret}\nBRIDGE_DB_AUTH_MODE=warn\n",
        encoding="utf-8",
    )
    binding.chmod(0o600)

    readback = verify_principal_binding(
        principals_path=registry,
        binding_path=binding,
    )

    assert readback["ok"] is False
    assert readback["reason_code"] == "binding.readback_identity_mismatch"
    serialized = json.dumps(readback, sort_keys=True)
    assert registered_secret not in serialized
    assert split_secret not in serialized
    assert hash_token(registered_secret) not in serialized
    assert hash_token(split_secret) not in serialized
    assert resolve_grant(split_secret, load_principal_grants(registry)) is None
