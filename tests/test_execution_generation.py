"""Immutable execution-generation staging, activation, and rollback tests."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import bridge_db.execution_generation as execution_generation
from bridge_db.execution_generation import (
    GenerationContractError,
    activate_generation,
    read_activation,
    rollback_generation,
    runtime_generation_identity,
    stage_generation,
    verify_generation,
)


def _git(source: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _source_repo(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    (source / ".codex").mkdir()
    (source / "config").mkdir()
    (source / "src" / "bridge_db" / "tools").mkdir(parents=True)
    files = {
        "pyproject.toml": "[project]\nname='bridge-db-fixture'\nversion='1'\n",
        "uv.lock": "version = 1\n",
        ".codex/verify.commands": "python -m pytest\n",
        "config/bridge-db-mcp-immutable": "#!/bin/sh\nexit 0\n",
        "config/com.saagar.bridge-db-checkpoint.plist": "<plist/>\n",
        "integration-spec.md": "# Fixture contract\n",
        "src/bridge_db/auth.py": "SCOPES = ('fixture',)\n",
        "src/bridge_db/client_rebinding.py": "REBINDING = 'fixture'\n",
        "src/bridge_db/db.py": "SCHEMA_VERSION = 23\n",
        "src/bridge_db/execution_generation.py": "GENERATION = 'fixture'\n",
        "src/bridge_db/secure_binding.py": "BINDING = 'fixture'\n",
        "src/bridge_db/server.py": "SERVER = 'fixture'\n",
        "src/bridge_db/snapshot_service.py": "SNAPSHOT = 'fixture'\n",
        "src/bridge_db/tenancy.py": "TENANCY = 'fixture'\n",
        "src/bridge_db/tools/__init__.py": "TOOLS = ()\n",
        "README.md": "# Fixture\n",
    }
    for name, content in files.items():
        (source / name).write_text(content, encoding="utf-8")
    _git(source, "init")
    _git(source, "config", "user.name", "Codex Fixture")
    _git(source, "config", "user.email", "fixture@example.invalid")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture one")
    return source, _git(source, "rev-parse", "HEAD")


def _stage(source: Path, root: Path, sha: str) -> dict[str, object]:
    return stage_generation(
        source=source,
        root=root,
        reviewed_sha=sha,
        python_executable=Path(sys.executable),
    )


def _commit_generation(
    source: Path, root: Path, *, marker: str
) -> tuple[str, str]:
    (source / "src" / "bridge_db" / "server.py").write_text(
        f"SERVER = {marker!r}\n", encoding="utf-8"
    )
    _git(source, "add", ".")
    _git(source, "commit", "-m", f"fixture {marker}")
    sha = _git(source, "rev-parse", "HEAD")
    staged = _stage(source, root, sha)
    return str(staged["generation_id"]), sha


def _write_activation_journal(
    root: Path,
    *,
    operation: str,
    old_current: str | None,
    old_previous: str | None,
    new_current: str,
) -> Path:
    body = {
        "schema": execution_generation.ACTIVATION_SCHEMA,
        "operation": operation,
        "old_current": old_current,
        "old_previous": old_previous,
        "new_current": new_current,
        "created_at": "2026-08-05T00:00:00Z",
    }
    journal = {
        **body,
        "journal_sha256": hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    path = root / ".activation.pending.json"
    path.write_text(
        json.dumps(journal, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    path.chmod(0o600)
    return path


def _write_activation_state(
    root: Path,
    *,
    operation: str,
    current: str,
    previous: str | None,
) -> None:
    state = {
        "schema": execution_generation.ACTIVATION_SCHEMA,
        "current_generation": current,
        "previous_generation": previous,
        "activated_at": "2026-08-05T00:00:01Z",
        "operation": operation,
    }
    path = root / "activation-state.json"
    if path.exists():
        path.chmod(0o600)
    path.write_text(
        json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    path.chmod(0o400)


def test_stage_is_content_addressed_immutable_and_idempotent(tmp_path: Path) -> None:
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"

    first = _stage(source, root, sha)
    second = _stage(source, root, sha)
    generation_id = str(first["generation_id"])
    release = root / "releases" / generation_id

    assert first["disposition"] == "staged"
    assert second["disposition"] == "preserved_existing"
    assert verify_generation(root, generation_id)["state"] == "verified"
    assert stat.S_IMODE(release.stat().st_mode) == 0o555
    assert stat.S_IMODE((release / "pyproject.toml").stat().st_mode) == 0o444
    assert stat.S_IMODE((release / "bin" / "bridge-db-mcp").stat().st_mode) == 0o555
    manifest = json.loads((release / "generation-manifest.json").read_text())
    assert manifest["reviewed_source_sha"] == sha
    assert len(manifest["dependency_sha256"]) == 64
    assert len(manifest["contract_sha256"]) == 64
    assert len(manifest["source_tree_sha256"]) == 64
    assert len(manifest["python_sha256"]) == 64
    assert first["dependency_environment_state"] == "external_unmanaged_lockfiles_only"
    assert first["claim_ceiling"] == (
        "source_and_interpreter_bound_external_environment_unmanaged"
    )
    assert first["database_rollback_contract"] == {
        "core_user_version": 23,
        "previous_merged_generation_user_version": 23,
        "previous_merged_generation_sha": "d7272d489873faa5ed84c81734636ffc8cecb095",
        "snapshot_refusal_extension": "BridgeSnapshotRefusalSchemaV1",
        "compatibility": "additive_extension_ignored_by_previous_runtime",
    }


def test_verify_rejects_extra_files_and_metadata_drift(tmp_path: Path) -> None:
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    staged = _stage(source, root, sha)
    generation_id = str(staged["generation_id"])
    release = root / "releases" / generation_id
    tracked = release / "README.md"

    tracked.chmod(0o644)
    with pytest.raises(GenerationContractError) as writable:
        verify_generation(root, generation_id)
    assert writable.value.reason_code == "generation.release_file_mode_mismatch"
    tracked.chmod(0o444)

    release.chmod(0o755)
    extra = release / "unreviewed.py"
    extra.write_text("UNREVIEWED = True\n", encoding="utf-8")
    extra.chmod(0o444)
    release.chmod(0o555)
    with pytest.raises(GenerationContractError) as unexpected:
        verify_generation(root, generation_id)
    assert unexpected.value.reason_code == "generation.release_extra_file"


def test_verify_rejects_external_interpreter_drift(tmp_path: Path) -> None:
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    python_copy = tmp_path / "python-fixture"
    python_copy.write_bytes(Path(sys.executable).resolve().read_bytes())
    python_copy.chmod(0o755)
    staged = stage_generation(
        source=source,
        root=root,
        reviewed_sha=sha,
        python_executable=python_copy,
    )
    generation_id = str(staged["generation_id"])

    python_copy.write_bytes(python_copy.read_bytes() + b"drift")
    python_copy.chmod(0o755)
    with pytest.raises(GenerationContractError) as drift:
        verify_generation(root, generation_id)
    assert drift.value.reason_code == "generation.python_digest_mismatch"


def test_generation_id_rejects_coherent_source_manifest_rewrite(tmp_path: Path) -> None:
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    staged = _stage(source, root, sha)
    generation_id = str(staged["generation_id"])
    release = root / "releases" / generation_id
    readme = release / "README.md"
    manifest_path = release / "generation-manifest.json"

    readme.chmod(0o644)
    readme.write_text("# Coherently rewritten fixture\n", encoding="utf-8")
    readme.chmod(0o444)
    manifest_path.chmod(0o644)
    manifest = json.loads(manifest_path.read_text())
    for entry in manifest["source_files"]:
        if entry["path"] == "README.md":
            entry["sha256"] = hashlib.sha256(readme.read_bytes()).hexdigest()
    manifest["source_tree_sha256"] = hashlib.sha256(
        json.dumps(
            manifest["source_files"], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    manifest_path.chmod(0o444)

    with pytest.raises(GenerationContractError) as mismatch:
        verify_generation(root, generation_id)
    assert mismatch.value.reason_code == "generation.id_content_mismatch"


def test_stage_rechecks_source_after_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, sha = _source_repo(tmp_path)
    original_copy = execution_generation._copy_tracked_source  # pyright: ignore[reportPrivateUsage]

    def racing_copy(
        selected_source: Path,
        target: Path,
        entries: list[dict[str, object]],
    ) -> None:
        original_copy(selected_source, target, entries)
        (selected_source / "README.md").write_text("raced\n", encoding="utf-8")

    monkeypatch.setattr(execution_generation, "_copy_tracked_source", racing_copy)
    with pytest.raises(GenerationContractError) as raced:
        _stage(source, tmp_path / "runtime", sha)
    assert raced.value.reason_code == "generation.source_changed_during_stage"


def test_stage_refuses_dirty_or_wrong_source_identity(tmp_path: Path) -> None:
    source, sha = _source_repo(tmp_path)
    (source / "untracked.txt").write_text("dirty", encoding="utf-8")

    with pytest.raises(GenerationContractError) as dirty:
        _stage(source, tmp_path / "runtime", sha)
    assert dirty.value.reason_code == "generation.source_not_clean"

    (source / "untracked.txt").unlink()
    with pytest.raises(GenerationContractError) as wrong:
        _stage(source, tmp_path / "runtime", "0" * 40)
    assert wrong.value.reason_code == "generation.reviewed_sha_mismatch"


def test_activation_second_generation_and_rollback_have_exact_readback(
    tmp_path: Path,
) -> None:
    source, first_sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    first = _stage(source, root, first_sha)
    first_id = str(first["generation_id"])
    activate_generation(root, first_id)

    (source / "src" / "bridge_db" / "server.py").write_text(
        "SERVER = 'fixture-two'\n", encoding="utf-8"
    )
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture two")
    second_sha = _git(source, "rev-parse", "HEAD")
    second = _stage(source, root, second_sha)
    second_id = str(second["generation_id"])
    activation = activate_generation(root, second_id)

    assert activation["outcome"] == "activated"
    assert activation["readback"]["current_generation"] == second_id
    assert activation["readback"]["previous_generation"] == first_id
    drain = json.loads((root / "drain" / f"{first_id}.json").read_text())
    assert drain["policy"] == "cooperative_no_process_termination"
    assert drain["superseded_by"] == second_id

    rollback = rollback_generation(root)
    assert rollback["outcome"] == "activated"
    assert read_activation(root)["current_generation"] == first_id
    assert read_activation(root)["previous_generation"] == second_id


def test_pending_before_map_is_restored_then_activation_retried(tmp_path: Path) -> None:
    source, first_sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    first_id = str(_stage(source, root, first_sha)["generation_id"])
    activate_generation(root, first_id)
    second_id, _ = _commit_generation(source, root, marker="fixture-two")
    _write_activation_journal(
        root,
        operation="activate",
        old_current=first_id,
        old_previous=None,
        new_current=second_id,
    )

    result = activate_generation(root, second_id)

    assert result["outcome"] == "activated"
    assert read_activation(root)["current_generation"] == second_id
    assert read_activation(root)["previous_generation"] == first_id
    assert not (root / ".activation.pending.json").exists()


def test_pending_partial_pointer_map_is_restored_then_retried(tmp_path: Path) -> None:
    source, first_sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    first_id = str(_stage(source, root, first_sha)["generation_id"])
    activate_generation(root, first_id)
    second_id, _ = _commit_generation(source, root, marker="fixture-two")
    _write_activation_journal(
        root,
        operation="activate",
        old_current=first_id,
        old_previous=None,
        new_current=second_id,
    )
    execution_generation._replace_pointer(root, "current", second_id)  # pyright: ignore[reportPrivateUsage]

    result = activate_generation(root, second_id)

    assert result["outcome"] == "activated"
    assert read_activation(root)["current_generation"] == second_id
    assert read_activation(root)["previous_generation"] == first_id


def test_pending_committed_map_finalizes_post_actions_once(tmp_path: Path) -> None:
    source, first_sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    first_id = str(_stage(source, root, first_sha)["generation_id"])
    activate_generation(root, first_id)
    second_id, _ = _commit_generation(source, root, marker="fixture-two")
    _write_activation_journal(
        root,
        operation="activate",
        old_current=first_id,
        old_previous=None,
        new_current=second_id,
    )
    execution_generation._replace_pointer(root, "current", second_id)  # pyright: ignore[reportPrivateUsage]
    execution_generation._replace_pointer(root, "previous", first_id)  # pyright: ignore[reportPrivateUsage]
    _write_activation_state(
        root,
        operation="activate",
        current=second_id,
        previous=first_id,
    )

    result = activate_generation(root, second_id)

    assert result["outcome"] == "activated_recovered"
    assert result["recovery_disposition"] == "committed_finalized"
    assert (root / "drain" / f"{first_id}.json").is_file()
    assert Path(str(result["receipt_path"])).is_file()
    assert not (root / ".activation.pending.json").exists()


def test_pending_arbitrary_pointer_map_is_refused_and_retained(tmp_path: Path) -> None:
    source, first_sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    first_id = str(_stage(source, root, first_sha)["generation_id"])
    activate_generation(root, first_id)
    second_id, _ = _commit_generation(source, root, marker="fixture-two")
    third_id, _ = _commit_generation(source, root, marker="fixture-three")
    journal = _write_activation_journal(
        root,
        operation="activate",
        old_current=first_id,
        old_previous=None,
        new_current=second_id,
    )
    execution_generation._replace_pointer(root, "current", third_id)  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(GenerationContractError) as mismatch:
        activate_generation(root, second_id)

    assert mismatch.value.reason_code == "generation.activation_journal_map_mismatch"
    assert journal.is_file()
    assert read_activation(root)["state"] == "interrupted"


def test_post_action_failure_recovers_without_pointer_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, first_sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    first_id = str(_stage(source, root, first_sha)["generation_id"])
    activate_generation(root, first_id)
    second_id, _ = _commit_generation(source, root, marker="fixture-two")
    original_mark = execution_generation._mark_draining  # pyright: ignore[reportPrivateUsage]

    def fail_mark(*_args: object, **_kwargs: object) -> None:
        raise OSError("fixture drain failure")

    monkeypatch.setattr(execution_generation, "_mark_draining", fail_mark)
    with pytest.raises(GenerationContractError) as pending:
        activate_generation(root, second_id)
    assert (
        pending.value.reason_code
        == "generation.activation_committed_post_actions_pending"
    )
    assert read_activation(root)["state"] == "interrupted"

    monkeypatch.setattr(execution_generation, "_mark_draining", original_mark)
    recovered = activate_generation(root, second_id)

    assert recovered["outcome"] == "activated_recovered"
    assert read_activation(root)["current_generation"] == second_id
    assert read_activation(root)["previous_generation"] == first_id


def test_committed_rollback_recovery_does_not_roll_forward(tmp_path: Path) -> None:
    source, first_sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    first_id = str(_stage(source, root, first_sha)["generation_id"])
    activate_generation(root, first_id)
    second_id, _ = _commit_generation(source, root, marker="fixture-two")
    activate_generation(root, second_id)
    _write_activation_journal(
        root,
        operation="rollback",
        old_current=second_id,
        old_previous=first_id,
        new_current=first_id,
    )
    execution_generation._replace_pointer(root, "current", first_id)  # pyright: ignore[reportPrivateUsage]
    execution_generation._replace_pointer(root, "previous", second_id)  # pyright: ignore[reportPrivateUsage]
    _write_activation_state(
        root,
        operation="rollback",
        current=first_id,
        previous=second_id,
    )

    recovered = rollback_generation(root)

    assert recovered["outcome"] == "activated_recovered"
    assert read_activation(root)["current_generation"] == first_id
    assert read_activation(root)["previous_generation"] == second_id


def test_readback_fails_closed_on_pending_or_pointer_mismatch(tmp_path: Path) -> None:
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    staged = _stage(source, root, sha)
    generation_id = str(staged["generation_id"])
    activate_generation(root, generation_id)
    (root / ".activation.pending.json").write_text("{}\n", encoding="utf-8")

    assert read_activation(root)["state"] == "interrupted"

    (root / ".activation.pending.json").unlink()
    (root / "current").unlink()
    (root / "current").write_text("not a pointer", encoding="utf-8")
    with pytest.raises(GenerationContractError) as mismatch:
        read_activation(root)
    assert mismatch.value.reason_code == "generation.pointer_not_symlink"


@pytest.mark.parametrize("lock_kind", ["symlink", "directory"])
def test_activation_refuses_symlink_or_special_lock(
    tmp_path: Path, lock_kind: str
) -> None:
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    staged = _stage(source, root, sha)
    generation_id = str(staged["generation_id"])
    lock = root / ".activation.lock"
    if lock_kind == "symlink":
        target = root / "lock-target"
        target.write_text("fixture\n", encoding="utf-8")
        target.chmod(0o600)
        lock.symlink_to(target)
    else:
        lock.mkdir(mode=0o700)

    with pytest.raises(GenerationContractError) as refused:
        activate_generation(root, generation_id)

    assert refused.value.reason_code == "generation.activation_lock_invalid"


def test_runtime_identity_requires_full_release_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    staged = _stage(source, root, sha)
    generation_id = str(staged["generation_id"])
    manifest = root / "releases" / generation_id / "generation-manifest.json"
    monkeypatch.setenv("BRIDGE_DB_GENERATION_MANIFEST", str(manifest))
    monkeypatch.setenv("BRIDGE_DB_GENERATION_ID", generation_id)

    identity = runtime_generation_identity()
    assert identity["state"] == "verified"
    assert identity["generation_id"] == generation_id
    assert identity["reviewed_source_sha"] == sha

    tracked = root / "releases" / generation_id / "src" / "bridge_db" / "server.py"
    os.chmod(tracked, 0o644)
    tracked.write_text("tampered\n", encoding="utf-8")
    assert runtime_generation_identity()["state"] == "unverified"


def test_runtime_identity_labels_mutable_direct_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BRIDGE_DB_GENERATION_MANIFEST", raising=False)
    monkeypatch.delenv("BRIDGE_DB_GENERATION_ID", raising=False)
    assert runtime_generation_identity()["state"] == "mutable_direct_path"
