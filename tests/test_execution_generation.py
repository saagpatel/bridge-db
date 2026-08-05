"""Immutable execution-generation staging, activation, and rollback tests."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

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
    (source / "src" / "bridge_db" / "tools").mkdir(parents=True)
    files = {
        "pyproject.toml": "[project]\nname='bridge-db-fixture'\nversion='1'\n",
        "uv.lock": "version = 1\n",
        ".codex/verify.commands": "python -m pytest\n",
        "integration-spec.md": "# Fixture contract\n",
        "src/bridge_db/auth.py": "SCOPES = ('fixture',)\n",
        "src/bridge_db/execution_generation.py": "GENERATION = 'fixture'\n",
        "src/bridge_db/secure_binding.py": "BINDING = 'fixture'\n",
        "src/bridge_db/server.py": "SERVER = 'fixture'\n",
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
