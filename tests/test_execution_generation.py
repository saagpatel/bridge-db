"""Immutable execution-generation staging, activation, and rollback tests."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata as metadata
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import bridge_db.execution_generation as execution_generation
from bridge_db import config, recovery, recovery_seal
from bridge_db.db import SCHEMA_VERSION, open_db
from bridge_db.execution_generation import (
    DEFAULT_RUNTIME_DEPENDENCY_DISTRIBUTIONS,
    GenerationContractError,
    RUNTIME_DEPENDENCY_CLAIM_CEILING,
    RUNTIME_DEPENDENCY_BUNDLE_SCHEMA,
    RUNTIME_DEPENDENCY_STATE,
    activate_generation,
    bootstrap_adopt_generation,
    read_activation,
    rollback_generation,
    runtime_generation_identity,
    stage_generation,
    verify_generation,
)
from bridge_db.tenancy import build_lifecycle_activation_evidence


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
    lock_packages = "\n".join(
        f'[[package]]\nname = "{name}"\nversion = "{metadata.version(name)}"\n'
        for name in DEFAULT_RUNTIME_DEPENDENCY_DISTRIBUTIONS
    )
    files = {
        "pyproject.toml": "[project]\nname='bridge-db-fixture'\nversion='1'\n",
        "uv.lock": f"version = 1\nrevision = 1\n{lock_packages}",
        ".codex/verify.commands": "python -m pytest\n",
        "config/bridge-db-mcp-immutable": "#!/bin/sh\nexit 0\n",
        "config/com.saagar.bridge-db-checkpoint.plist": "<plist/>\n",
        "integration-spec.md": "# Fixture contract\n",
        "src/bridge_db/auth.py": "SCOPES = ('fixture',)\n",
        "src/bridge_db/client_rebinding.py": "REBINDING = 'fixture'\n",
        "src/bridge_db/db.py": "SCHEMA_VERSION = 23\n",
        "src/bridge_db/execution_generation.py": "GENERATION = 'fixture'\n",
        "src/bridge_db/owner_delegation.py": "DELEGATION = 'fixture'\n",
        "src/bridge_db/secure_binding.py": "BINDING = 'fixture'\n",
        "src/bridge_db/server.py": "SERVER = 'fixture'\n",
        "src/bridge_db/shared_runtime.py": "SHARED_RUNTIME = 'fixture'\n",
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


def _stage_python() -> Path:
    return Path(os.environ.get("BRIDGE_DB_TEST_STAGE_PYTHON", sys.executable))


def _stage(source: Path, root: Path, sha: str) -> dict[str, object]:
    result = stage_generation(
        source=source,
        root=root,
        reviewed_sha=sha,
        python_executable=_stage_python(),
    )
    _write_tenancy_activation_evidence(root, str(result["generation_id"]))
    return result


def _write_tenancy_activation_evidence(root: Path, generation_id: str) -> Path:
    path = root / "tenancy-activation-evidence.json"
    if path.exists():
        path.chmod(0o600)
    evidence = build_lifecycle_activation_evidence(
        [
            {
                "owner": owner,
                "scenario": scenario,
                "process_count": index + 1,
                "lifetime_seconds": 120 + index,
                "rss_bytes": (32 + index) * 1024 * 1024,
            }
            for index, (owner, scenario) in enumerate(
                (
                    ("codex", "normal_close"),
                    ("claude", "app_restart"),
                    ("personal_ops", "abrupt_exit"),
                    ("hermes", "generation_rollover"),
                )
            )
        ],
        generation_id=generation_id,
    )
    path.write_text(
        json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    path.chmod(0o400)
    return path


def _initialize_recovery_database(db_path: Path) -> None:
    async def initialize() -> None:
        db = await open_db(db_path)
        await db.close()

    asyncio.run(initialize())


def _make_recovery_ready(db_path: Path) -> None:
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    sealed = recovery_seal.seal_recovery_batch(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
        batch_id="execution-generation-fixture",
        owner="codex",
    )
    assert sealed["outcome"] == "recovery_sealed"
    assert sealed["ready"] is True


@pytest.fixture
def _recovery_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "activation-bridge.db"
    _initialize_recovery_database(db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    _make_recovery_ready(db_path)
    return db_path


def _commit_generation(source: Path, root: Path, *, marker: str) -> tuple[str, str]:
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


def _break_generation_launcher(root: Path, generation_id: str) -> None:
    launcher = root / "releases" / generation_id / "bin" / "bridge-db-mcp"
    launcher.chmod(0o755)
    launcher.write_bytes(launcher.read_bytes() + b"# legacy drift\n")
    launcher.chmod(0o555)


def _manifest_sha256(root: Path, generation_id: str) -> str:
    return hashlib.sha256(
        (root / "releases" / generation_id / "generation-manifest.json").read_bytes()
    ).hexdigest()


def _bootstrap_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, str, str, str, str]:
    source, first_sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    first_id = str(_stage(source, root, first_sha)["generation_id"])
    activate_generation(root, first_id)
    second_id, _ = _commit_generation(source, root, marker="fixture-two")
    activate_generation(root, second_id)
    third_id, _ = _commit_generation(source, root, marker="fixture-three")
    fourth_id, _ = _commit_generation(source, root, marker="fixture-four")
    _write_tenancy_activation_evidence(root, third_id)
    _break_generation_launcher(root, first_id)
    _break_generation_launcher(root, second_id)
    return source, root, first_id, second_id, third_id, fourth_id


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
    assert len(manifest["runtime_dependency_sha256"]) == 64
    runtime_evidence = manifest["runtime_dependency_evidence"]
    assert runtime_evidence["schema"] == RUNTIME_DEPENDENCY_BUNDLE_SCHEMA
    assert runtime_evidence["sha256"] == manifest["runtime_dependency_sha256"]
    assert [item["distribution"] for item in runtime_evidence["distributions"]] == [
        *DEFAULT_RUNTIME_DEPENDENCY_DISTRIBUTIONS
    ]
    distribution_names = {
        item["distribution"] for item in runtime_evidence["distributions"]
    }
    assert {"aiosqlite", "mcp", "pydantic", "uvicorn"} <= distribution_names
    assert len(distribution_names) >= 30
    assert first["runtime_dependency_sha256"] == manifest["runtime_dependency_sha256"]
    assert first["runtime_dependency_state"] == "verified"
    assert first["dependency_environment_state"] == RUNTIME_DEPENDENCY_STATE
    assert first["claim_ceiling"] == RUNTIME_DEPENDENCY_CLAIM_CEILING
    assert first["claim_ceiling"] == (
        "source_interpreter_and_generation_immutable_runtime_dependencies_bound"
    )
    assert first["database_rollback_contract"] == {
        "core_user_version": 23,
        "previous_merged_generation_user_version": 23,
        "previous_merged_generation_sha": "d7272d489873faa5ed84c81734636ffc8cecb095",
        "snapshot_refusal_extension": "BridgeSnapshotRefusalSchemaV1",
        "owner_delegation_extension": "BridgeOwnerDelegationSchemaV1",
        "compatibility": "additive_extensions_ignored_by_previous_runtime",
    }


def test_pre_owner_delegation_generation_retains_legacy_rollback_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = _source_repo(tmp_path)
    (source / "src" / "bridge_db" / "owner_delegation.py").unlink()
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "fixture before owner delegation")
    reviewed_sha = _git(source, "rev-parse", "HEAD")
    root = tmp_path / "runtime"

    legacy_contract_paths = tuple(
        path
        for path in execution_generation.DEFAULT_CONTRACT_PATHS
        if path != "src/bridge_db/owner_delegation.py"
    )
    current_contract_paths = execution_generation.DEFAULT_CONTRACT_PATHS
    monkeypatch.setattr(
        execution_generation, "DEFAULT_CONTRACT_PATHS", legacy_contract_paths
    )
    staged = stage_generation(
        source=source,
        root=root,
        reviewed_sha=reviewed_sha,
        python_executable=Path(sys.executable),
        contract_paths=legacy_contract_paths,
    )
    monkeypatch.setattr(
        execution_generation, "DEFAULT_CONTRACT_PATHS", current_contract_paths
    )
    generation_id = str(staged["generation_id"])

    assert verify_generation(root, generation_id)["state"] == "verified"
    assert staged["database_rollback_contract"] == {
        "core_user_version": 23,
        "previous_merged_generation_user_version": 23,
        "previous_merged_generation_sha": "d7272d489873faa5ed84c81734636ffc8cecb095",
        "snapshot_refusal_extension": "BridgeSnapshotRefusalSchemaV1",
        "compatibility": "additive_extension_ignored_by_previous_runtime",
    }


def test_pre_shared_runtime_generation_remains_verified_and_rollbackable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _recovery_ready: Path,
) -> None:
    source, _ = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    shared_runtime = source / "src" / "bridge_db" / "shared_runtime.py"
    shared_runtime.unlink()
    _git(source, "add", "-A")
    _git(source, "commit", "-m", "fixture pre-shared runtime")
    legacy_sha = _git(source, "rev-parse", "HEAD")
    current_contract_paths = execution_generation.DEFAULT_CONTRACT_PATHS
    legacy_contract_paths = tuple(
        path
        for path in current_contract_paths
        if path != "src/bridge_db/shared_runtime.py"
    )
    monkeypatch.setattr(
        execution_generation, "DEFAULT_CONTRACT_PATHS", legacy_contract_paths
    )
    legacy = stage_generation(
        source=source,
        root=root,
        reviewed_sha=legacy_sha,
        python_executable=_stage_python(),
        contract_paths=legacy_contract_paths,
    )
    legacy_id = str(legacy["generation_id"])
    legacy_manifest_path = root / "releases" / legacy_id / "generation-manifest.json"
    legacy_manifest_path.chmod(0o644)
    legacy_manifest = json.loads(legacy_manifest_path.read_text())
    del legacy_manifest["runtime_dependency_sha256"]
    del legacy_manifest["runtime_dependency_evidence"]
    runtime_path = root / "releases" / legacy_id / "runtime"
    for candidate in runtime_path.rglob("*"):
        candidate.chmod(0o755 if candidate.is_dir() else 0o644)
    runtime_path.chmod(0o755)
    runtime_path.parent.chmod(0o755)
    shutil.rmtree(runtime_path)
    runtime_path.parent.chmod(0o555)
    legacy_manifest["dependency_environment_state"] = (
        execution_generation.LEGACY_RUNTIME_DEPENDENCY_STATE
    )
    legacy_launcher = execution_generation._make_launcher(  # pyright: ignore[reportPrivateUsage]
        release_path=runtime_path.parent,
        python_executable=Path(legacy_manifest["python_executable"]),
        python_resolved=Path(legacy_manifest["python_executable_resolved"]),
        python_sha256=legacy_manifest["python_sha256"],
        generation_id=legacy_id,
        bundled_runtime=False,
    )
    launcher_path = runtime_path.parent / "bin" / "bridge-db-mcp"
    launcher_path.chmod(0o755)
    launcher_path.write_bytes(legacy_launcher)
    launcher_path.chmod(0o555)
    legacy_manifest["launcher_sha256"] = hashlib.sha256(legacy_launcher).hexdigest()
    legacy_manifest_path.write_text(
        json.dumps(legacy_manifest, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    legacy_manifest_path.chmod(0o444)
    _write_tenancy_activation_evidence(root, legacy_id)
    monkeypatch.setattr(
        execution_generation, "DEFAULT_CONTRACT_PATHS", current_contract_paths
    )

    verified_legacy = verify_generation(root, legacy_id)
    assert verified_legacy["state"] == "verified"
    assert verified_legacy["runtime_dependency_sha256"] is None
    assert verified_legacy["runtime_dependency_state"] == "legacy_unverified"
    assert (
        verified_legacy["claim_ceiling"]
        == execution_generation.LEGACY_RUNTIME_DEPENDENCY_CLAIM_CEILING
    )
    activate_generation(root, legacy_id)

    shared_runtime.write_text("SHARED_RUNTIME = 'fixture'\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture shared runtime")
    current_sha = _git(source, "rev-parse", "HEAD")
    current_id = str(_stage(source, root, current_sha)["generation_id"])

    activation = activate_generation(root, current_id)
    assert activation["readback"]["previous_generation"] == legacy_id
    rollback = rollback_generation(root)
    assert rollback["outcome"] == "activated"
    assert read_activation(root)["current_generation"] == legacy_id


def test_verify_rejects_legacy_runtime_dependency_shape_with_shared_runtime(
    tmp_path: Path,
) -> None:
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    generation_id = str(_stage(source, root, sha)["generation_id"])
    manifest_path = root / "releases" / generation_id / "generation-manifest.json"
    manifest_path.chmod(0o644)
    manifest = json.loads(manifest_path.read_text())
    del manifest["runtime_dependency_sha256"]
    del manifest["runtime_dependency_evidence"]
    manifest["dependency_environment_state"] = (
        execution_generation.LEGACY_RUNTIME_DEPENDENCY_STATE
    )
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    manifest_path.chmod(0o444)

    with pytest.raises(GenerationContractError) as refused:
        verify_generation(root, generation_id)

    assert refused.value.reason_code == "generation.manifest_shape_invalid"


def test_verify_rejects_legacy_contract_downgrade_with_shared_runtime(
    tmp_path: Path,
) -> None:
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    generation_id = str(_stage(source, root, sha)["generation_id"])
    manifest_path = root / "releases" / generation_id / "generation-manifest.json"
    manifest_path.chmod(0o644)
    manifest = json.loads(manifest_path.read_text())
    legacy_paths = [
        path
        for path in manifest["contract_paths"]
        if path != "src/bridge_db/shared_runtime.py"
    ]
    selected_entries = [
        entry for entry in manifest["source_files"] if entry["path"] in legacy_paths
    ]
    manifest["contract_paths"] = legacy_paths
    manifest["contract_sha256"] = hashlib.sha256(
        json.dumps(selected_entries, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    manifest_path.chmod(0o444)

    with pytest.raises(GenerationContractError) as refused:
        verify_generation(root, generation_id)

    assert refused.value.reason_code == "generation.contract_paths_mismatch"


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


def test_verify_rejects_external_interpreter_digest_drift(tmp_path: Path) -> None:
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    staged = _stage(source, root, sha)
    generation_id = str(staged["generation_id"])
    manifest_path = root / "releases" / generation_id / "generation-manifest.json"
    manifest_path.chmod(0o644)
    manifest = json.loads(manifest_path.read_text())
    manifest["python_sha256"] = "0" * 64
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    manifest_path.chmod(0o444)

    with pytest.raises(GenerationContractError) as drift:
        verify_generation(root, generation_id)
    assert drift.value.reason_code == "generation.python_digest_mismatch"


def test_verify_rejects_runtime_dependency_bundle_drift(tmp_path: Path) -> None:
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    staged = _stage(source, root, sha)
    generation_id = str(staged["generation_id"])
    release = root / "releases" / generation_id
    manifest = json.loads((release / "generation-manifest.json").read_text())
    runtime_evidence = manifest["runtime_dependency_evidence"]
    bundled_file = release / runtime_evidence["files"][0]["path"]
    bundled_file.chmod(0o644)
    bundled_file.write_bytes(bundled_file.read_bytes() + b"drift")
    bundled_file.chmod(0o555 if runtime_evidence["files"][0]["executable"] else 0o444)

    with pytest.raises(GenerationContractError) as drift:
        verify_generation(root, generation_id)
    assert drift.value.reason_code == "generation.runtime_dependency_digest_mismatch"


def test_runtime_dependency_collector_rejects_path_swap_after_metadata(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    package = site / "codex_fake_dep"
    dist_info = site / "codex_fake_dep-1.0.dist-info"
    package.mkdir(parents=True)
    dist_info.mkdir()
    target = package / "__init__.py"
    replacement = tmp_path / "replacement.py"
    target.write_text("VALUE = 'original'\n", encoding="utf-8")
    replacement.write_text("VALUE = 'replacement'\n", encoding="utf-8")
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: codex-fake-dep\nVersion: 1.0\n",
        encoding="utf-8",
    )
    (dist_info / "RECORD").write_text(
        "\n".join(
            [
                "codex_fake_dep/__init__.py,,",
                "codex_fake_dep-1.0.dist-info/METADATA,,",
                "codex_fake_dep-1.0.dist-info/RECORD,,",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (site / "sitecustomize.py").write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import os",
                "_original_lstat = Path.lstat",
                "_swapped = False",
                "def _patched_lstat(self, *args, **kwargs):",
                "    global _swapped",
                "    result = _original_lstat(self, *args, **kwargs)",
                "    if not _swapped and os.environ.get('BRIDGE_TEST_SWAP_TARGET') == str(self):",
                "        _swapped = True",
                "        os.replace(",
                "            os.environ['BRIDGE_TEST_SWAP_REPLACEMENT'],",
                "            os.environ['BRIDGE_TEST_SWAP_TARGET'],",
                "        )",
                "    return result",
                "Path.lstat = _patched_lstat",
                "",
            ]
        ),
        encoding="utf-8",
    )
    script = execution_generation._runtime_dependency_collector_script(  # pyright: ignore[reportPrivateUsage]
        ("codex-fake-dep",)
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": str(site),
            "BRIDGE_TEST_SWAP_TARGET": str(target),
            "BRIDGE_TEST_SWAP_REPLACEMENT": str(replacement),
        },
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert json.loads(completed.stdout) == {
        "ok": False,
        "reason_code": "generation.runtime_dependency_file_changed_during_scan",
    }


def test_runtime_dependency_collector_rejects_cross_distribution_swap(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    site.mkdir()

    def write_distribution(import_name: str, distribution_name: str) -> Path:
        package = site / import_name
        dist_info = site / f"{import_name}-1.0.dist-info"
        package.mkdir()
        dist_info.mkdir()
        target = package / "__init__.py"
        target.write_text(f"NAME = {distribution_name!r}\n", encoding="utf-8")
        (dist_info / "METADATA").write_text(
            "\n".join(
                [
                    "Metadata-Version: 2.1",
                    f"Name: {distribution_name}",
                    "Version: 1.0",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (dist_info / "RECORD").write_text(
            "\n".join(
                [
                    f"{import_name}/__init__.py,,",
                    f"{import_name}-1.0.dist-info/METADATA,,",
                    f"{import_name}-1.0.dist-info/RECORD,,",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return target

    first_target = write_distribution("codex_fake_a", "codex-fake-a")
    second_target = write_distribution("codex_fake_b", "codex-fake-b")
    replacement = tmp_path / "replacement.py"
    replacement.write_text("NAME = 'replacement'\n", encoding="utf-8")
    (site / "sitecustomize.py").write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import os",
                "_original_lstat = Path.lstat",
                "_swapped = False",
                "def _patched_lstat(self, *args, **kwargs):",
                "    global _swapped",
                "    result = _original_lstat(self, *args, **kwargs)",
                "    if not _swapped and os.environ.get('BRIDGE_TEST_SWAP_TRIGGER') == str(self):",
                "        _swapped = True",
                "        os.replace(",
                "            os.environ['BRIDGE_TEST_SWAP_REPLACEMENT'],",
                "            os.environ['BRIDGE_TEST_SWAP_TARGET'],",
                "        )",
                "    return result",
                "Path.lstat = _patched_lstat",
                "",
            ]
        ),
        encoding="utf-8",
    )
    script = execution_generation._runtime_dependency_collector_script(  # pyright: ignore[reportPrivateUsage]
        ("codex-fake-a", "codex-fake-b")
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": str(site),
            "BRIDGE_TEST_SWAP_TARGET": str(first_target),
            "BRIDGE_TEST_SWAP_TRIGGER": str(second_target),
            "BRIDGE_TEST_SWAP_REPLACEMENT": str(replacement),
        },
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert json.loads(completed.stdout) == {
        "ok": False,
        "reason_code": "generation.runtime_dependency_file_changed_during_scan",
    }


def test_verify_rejects_legacy_unmanaged_dependency_claim(tmp_path: Path) -> None:
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    staged = _stage(source, root, sha)
    generation_id = str(staged["generation_id"])
    manifest_path = root / "releases" / generation_id / "generation-manifest.json"
    manifest_path.chmod(0o644)
    manifest = json.loads(manifest_path.read_text())
    manifest["dependency_environment_state"] = "external_unmanaged_lockfiles_only"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    manifest_path.chmod(0o444)

    with pytest.raises(GenerationContractError) as legacy_claim:
        verify_generation(root, generation_id)
    assert legacy_claim.value.reason_code == "generation.dependency_claim_invalid"


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


def test_activation_gate_refuses_missing_recovery_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "activation-bridge.db"
    _initialize_recovery_database(db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    generation_id = str(_stage(source, root, sha)["generation_id"])

    with pytest.raises(GenerationContractError) as refused:
        activate_generation(root, generation_id)

    assert refused.value.reason_code == "generation.recovery_anchor_missing"
    assert not (root / "current").exists()
    assert not (root / ".activation.pending.json").exists()


def test_activation_gate_refuses_missing_tenancy_evidence_before_pointer_write(
    tmp_path: Path, _recovery_ready: Path
) -> None:
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    generation_id = str(_stage(source, root, sha)["generation_id"])
    (root / "tenancy-activation-evidence.json").unlink()

    with pytest.raises(GenerationContractError) as refused:
        activate_generation(root, generation_id)

    assert refused.value.reason_code == "generation.tenancy_evidence_missing"
    assert not (root / "current").exists()
    assert not (root / ".activation.pending.json").exists()


def test_activation_gate_refuses_mutable_or_tampered_tenancy_evidence(
    tmp_path: Path, _recovery_ready: Path
) -> None:
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    generation_id = str(_stage(source, root, sha)["generation_id"])
    evidence_path = root / "tenancy-activation-evidence.json"
    evidence_path.chmod(0o600)

    with pytest.raises(GenerationContractError) as mutable:
        activate_generation(root, generation_id)
    assert mutable.value.reason_code == "generation.tenancy_evidence_not_private"

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["observations"][0]["process_count"] += 1
    evidence_path.write_text(
        json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    evidence_path.chmod(0o400)
    with pytest.raises(GenerationContractError) as tampered:
        activate_generation(root, generation_id)
    assert tampered.value.reason_code == "generation.tenancy_evidence_digest_mismatch"
    assert not (root / "current").exists()


def test_activation_gate_refuses_replay_evidence_for_another_generation(
    tmp_path: Path, _recovery_ready: Path
) -> None:
    source, first_sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    first_id = str(_stage(source, root, first_sha)["generation_id"])
    second_id, _ = _commit_generation(source, root, marker="fixture-two")
    assert first_id != second_id

    with pytest.raises(GenerationContractError) as refused:
        activate_generation(root, first_id)

    assert (
        refused.value.reason_code == "generation.tenancy_evidence_generation_mismatch"
    )
    assert not (root / "current").exists()


def test_activation_cli_reports_recovery_gate_failure_without_pointer_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "activation-bridge.db"
    _initialize_recovery_database(db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    generation_id = str(_stage(source, root, sha)["generation_id"])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bridge-db-execution-generation",
            "activate",
            "--root",
            str(root),
            "--generation-id",
            generation_id,
        ],
    )

    with pytest.raises(SystemExit) as exited:
        execution_generation.main()

    assert exited.value.code == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "reason_code": "generation.recovery_anchor_missing",
    }
    assert not (root / "current").exists()


def test_activation_gate_refuses_missing_recovery_seal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "activation-bridge.db"
    _initialize_recovery_database(db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    generation_id = str(_stage(source, root, sha)["generation_id"])

    with pytest.raises(GenerationContractError) as refused:
        activate_generation(root, generation_id)

    assert refused.value.reason_code == "generation.recovery_seal_missing"
    assert not (root / "current").exists()


def test_activation_gate_refuses_stale_recovery_lifecycle(
    tmp_path: Path, _recovery_ready: Path
) -> None:
    with sqlite3.connect(_recovery_ready) as changed:
        changed.execute(
            "INSERT INTO activity_log "
            "(source, timestamp, project_name, summary) VALUES (?, ?, ?, ?)",
            (
                "codex",
                "2026-08-05T00:00:00Z",
                "bridge-db",
                "source changed after recovery seal",
            ),
        )
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    generation_id = str(_stage(source, root, sha)["generation_id"])

    with pytest.raises(GenerationContractError) as refused:
        activate_generation(root, generation_id)

    assert refused.value.reason_code == "generation.recovery_anchor_stale"
    assert not (root / "current").exists()


def test_activation_gate_refuses_stale_recovery_seal(
    tmp_path: Path, _recovery_ready: Path
) -> None:
    with sqlite3.connect(_recovery_ready) as changed:
        changed.execute(
            "INSERT INTO activity_log "
            "(source, timestamp, project_name, summary) VALUES (?, ?, ?, ?)",
            (
                "codex",
                "2026-08-05T00:00:00Z",
                "bridge-db",
                "source changed before unsealed anchor rotation",
            ),
        )
    rotated = recovery.rotate_recovery_anchor(
        _recovery_ready,
        expected_schema_version=SCHEMA_VERSION,
    )
    assert rotated["ready"] is True
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    generation_id = str(_stage(source, root, sha)["generation_id"])

    with pytest.raises(GenerationContractError) as refused:
        activate_generation(root, generation_id)

    assert refused.value.reason_code == "generation.recovery_seal_stale"
    assert not (root / "current").exists()


def test_activation_gate_refuses_terminal_unsealed_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "activation-bridge.db"
    _initialize_recovery_database(db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    recovery.create_recovery_anchor(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
    )

    def fail_rotation(*_args: object, **_kwargs: object) -> None:
        raise OSError("fixture recovery rotation failure")

    monkeypatch.setattr(recovery_seal, "rotate_recovery_anchor", fail_rotation)
    unsealed = recovery_seal.seal_recovery_batch(
        db_path,
        expected_schema_version=SCHEMA_VERSION,
        batch_id="execution-generation-unsealed",
        owner="codex",
    )
    assert unsealed["outcome"] == "recovery_unsealed"
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    generation_id = str(_stage(source, root, sha)["generation_id"])

    with pytest.raises(GenerationContractError) as refused:
        activate_generation(root, generation_id)

    assert refused.value.reason_code == "generation.recovery_seal_unsealed"
    assert not (root / "current").exists()


def test_activation_gate_accepts_current_verified_anchor_and_seal(
    tmp_path: Path, _recovery_ready: Path
) -> None:
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    generation_id = str(_stage(source, root, sha)["generation_id"])

    activated = activate_generation(root, generation_id)

    assert activated["outcome"] == "activated"
    assert read_activation(root)["current_generation"] == generation_id
    evidence = activated["readback"]["tenancy_activation_evidence"]
    assert evidence["state"] == "verified"
    assert evidence["owners"] == ["claude", "codex", "hermes", "personal_ops"]
    assert len(evidence["evidence_sha256"]) == 64
    assert len(evidence["file_sha256"]) == 64


def test_activation_second_generation_and_rollback_have_exact_readback(
    tmp_path: Path,
    _recovery_ready: Path,
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
    assert read_activation(root)["tenancy_activation_evidence"] == {
        "state": "not_required_for_rollback"
    }


def test_bootstrap_adoption_replaces_unverified_legacy_with_two_verified_peers(
    tmp_path: Path, _recovery_ready: Path
) -> None:
    _, root, legacy_previous, legacy_current, target, rollback = _bootstrap_fixture(
        tmp_path
    )

    with pytest.raises(GenerationContractError) as ordinary:
        activate_generation(root, target)
    assert ordinary.value.reason_code == "generation.launcher_digest_mismatch"

    result = bootstrap_adopt_generation(
        root,
        target,
        rollback,
        expected_current_generation=legacy_current,
        expected_previous_generation=legacy_previous,
        expected_current_manifest_sha256=_manifest_sha256(root, legacy_current),
        expected_previous_manifest_sha256=_manifest_sha256(root, legacy_previous),
    )

    assert result["outcome"] == "bootstrap_adopted"
    assert result["legacy_generations_preserved"] is True
    assert result["legacy_generations_rollback_eligible"] is False
    assert read_activation(root)["current_generation"] == target
    assert read_activation(root)["previous_generation"] == rollback
    assert verify_generation(root, target)["state"] == "verified"
    assert verify_generation(root, rollback)["state"] == "verified"
    assert (root / "releases" / legacy_current).is_dir()
    assert (root / "releases" / legacy_previous).is_dir()
    evidence = result["legacy_pointer_evidence"]
    assert [item["pointer"] for item in evidence] == ["current", "previous"]
    assert all(
        item["verification_state"] == "identity_preserved_integrity_unverified"
        for item in evidence
    )


def test_bootstrap_adoption_refuses_wrong_legacy_digest_before_pointer_write(
    tmp_path: Path, _recovery_ready: Path
) -> None:
    _, root, legacy_previous, legacy_current, target, rollback = _bootstrap_fixture(
        tmp_path
    )

    with pytest.raises(GenerationContractError) as refused:
        bootstrap_adopt_generation(
            root,
            target,
            rollback,
            expected_current_generation=legacy_current,
            expected_previous_generation=legacy_previous,
            expected_current_manifest_sha256="0" * 64,
            expected_previous_manifest_sha256=_manifest_sha256(root, legacy_previous),
        )

    assert refused.value.reason_code == "generation.bootstrap_legacy_manifest_mismatch"
    assert execution_generation._pointer_target(root, "current") == legacy_current  # pyright: ignore[reportPrivateUsage]
    assert execution_generation._pointer_target(root, "previous") == legacy_previous  # pyright: ignore[reportPrivateUsage]
    assert not (root / ".activation.pending.json").exists()


def test_bootstrap_adoption_refuses_non_distinct_verified_rollback(
    tmp_path: Path, _recovery_ready: Path
) -> None:
    _, root, legacy_previous, legacy_current, target, _ = _bootstrap_fixture(tmp_path)

    with pytest.raises(GenerationContractError) as refused:
        bootstrap_adopt_generation(
            root,
            target,
            target,
            expected_current_generation=legacy_current,
            expected_previous_generation=legacy_previous,
            expected_current_manifest_sha256=_manifest_sha256(root, legacy_current),
            expected_previous_manifest_sha256=_manifest_sha256(root, legacy_previous),
        )

    assert refused.value.reason_code == "generation.bootstrap_rollback_not_distinct"


def test_bootstrap_adoption_restores_legacy_map_on_state_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _recovery_ready: Path,
) -> None:
    _, root, legacy_previous, legacy_current, target, rollback = _bootstrap_fixture(
        tmp_path
    )
    original_write = execution_generation._atomic_write  # pyright: ignore[reportPrivateUsage]
    failed = False

    def fail_state_once(path: Path, content: bytes, *, mode: int) -> None:
        nonlocal failed
        if path == root / "activation-state.json" and not failed:
            failed = True
            raise OSError("fixture state write failure")
        original_write(path, content, mode=mode)

    monkeypatch.setattr(execution_generation, "_atomic_write", fail_state_once)

    with pytest.raises(OSError, match="fixture state write failure"):
        bootstrap_adopt_generation(
            root,
            target,
            rollback,
            expected_current_generation=legacy_current,
            expected_previous_generation=legacy_previous,
            expected_current_manifest_sha256=_manifest_sha256(root, legacy_current),
            expected_previous_manifest_sha256=_manifest_sha256(root, legacy_previous),
        )

    assert execution_generation._pointer_target(root, "current") == legacy_current  # pyright: ignore[reportPrivateUsage]
    assert execution_generation._pointer_target(root, "previous") == legacy_previous  # pyright: ignore[reportPrivateUsage]
    assert not (root / ".activation.pending.json").exists()
    state = json.loads((root / "activation-state.json").read_text())
    assert state["current_generation"] == legacy_current
    assert state["previous_generation"] == legacy_previous


def test_bootstrap_adoption_finalizes_committed_journal_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _recovery_ready: Path,
) -> None:
    _, root, legacy_previous, legacy_current, target, rollback = _bootstrap_fixture(
        tmp_path
    )
    original_receipt = execution_generation._write_bootstrap_adoption_receipt  # pyright: ignore[reportPrivateUsage]

    def fail_receipt(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise OSError("fixture receipt failure")

    monkeypatch.setattr(
        execution_generation, "_write_bootstrap_adoption_receipt", fail_receipt
    )
    with pytest.raises(GenerationContractError) as pending:
        bootstrap_adopt_generation(
            root,
            target,
            rollback,
            expected_current_generation=legacy_current,
            expected_previous_generation=legacy_previous,
            expected_current_manifest_sha256=_manifest_sha256(root, legacy_current),
            expected_previous_manifest_sha256=_manifest_sha256(root, legacy_previous),
        )
    assert (
        pending.value.reason_code
        == "generation.bootstrap_committed_post_actions_pending"
    )
    assert (root / ".activation.pending.json").is_file()

    monkeypatch.setattr(
        execution_generation, "_write_bootstrap_adoption_receipt", original_receipt
    )
    recovered = bootstrap_adopt_generation(
        root,
        target,
        rollback,
        expected_current_generation=legacy_current,
        expected_previous_generation=legacy_previous,
        expected_current_manifest_sha256=_manifest_sha256(root, legacy_current),
        expected_previous_manifest_sha256=_manifest_sha256(root, legacy_previous),
    )

    assert recovered["outcome"] == "bootstrap_adopted_recovered"
    assert recovered["recovery_disposition"] == "committed_finalized"
    assert not (root / ".activation.pending.json").exists()
    assert read_activation(root)["current_generation"] == target
    assert read_activation(root)["previous_generation"] == rollback


def test_rollback_gate_refuses_stale_recovery_without_pointer_change(
    tmp_path: Path, _recovery_ready: Path
) -> None:
    source, first_sha = _source_repo(tmp_path)
    root = tmp_path / "runtime"
    first_id = str(_stage(source, root, first_sha)["generation_id"])
    activate_generation(root, first_id)
    second_id, _ = _commit_generation(source, root, marker="fixture-two")
    activate_generation(root, second_id)
    with sqlite3.connect(_recovery_ready) as changed:
        changed.execute(
            "INSERT INTO activity_log "
            "(source, timestamp, project_name, summary) VALUES (?, ?, ?, ?)",
            (
                "codex",
                "2026-08-05T00:00:00Z",
                "bridge-db",
                "source changed before rollback",
            ),
        )

    with pytest.raises(GenerationContractError) as refused:
        rollback_generation(root)

    assert refused.value.reason_code == "generation.recovery_anchor_stale"
    assert read_activation(root)["current_generation"] == second_id
    assert read_activation(root)["previous_generation"] == first_id


def test_pending_before_map_is_restored_then_activation_retried(
    tmp_path: Path, _recovery_ready: Path
) -> None:
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


def test_pending_partial_pointer_map_is_restored_then_retried(
    tmp_path: Path, _recovery_ready: Path
) -> None:
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


def test_pending_committed_map_finalizes_post_actions_once(
    tmp_path: Path, _recovery_ready: Path
) -> None:
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
    (root / "tenancy-activation-evidence.json").unlink()

    result = activate_generation(root, second_id)

    assert result["outcome"] == "activated_recovered"
    assert result["recovery_disposition"] == "committed_finalized"
    assert (root / "drain" / f"{first_id}.json").is_file()
    assert Path(str(result["receipt_path"])).is_file()
    assert result["readback"]["tenancy_activation_evidence"] == {
        "state": "legacy_unverified"
    }
    assert not (root / ".activation.pending.json").exists()


def test_pending_arbitrary_pointer_map_is_refused_and_retained(
    tmp_path: Path, _recovery_ready: Path
) -> None:
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _recovery_ready: Path,
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

    with sqlite3.connect(_recovery_ready) as changed:
        changed.execute(
            "INSERT INTO activity_log "
            "(source, timestamp, project_name, summary) VALUES (?, ?, ?, ?)",
            (
                "codex",
                "2026-08-05T00:00:00Z",
                "bridge-db",
                "source changed after pointer commit",
            ),
        )

    monkeypatch.setattr(execution_generation, "_mark_draining", original_mark)
    recovered = activate_generation(root, second_id)

    assert recovered["outcome"] == "activated_recovered"
    assert read_activation(root)["current_generation"] == second_id
    assert read_activation(root)["previous_generation"] == first_id


def test_committed_rollback_recovery_does_not_roll_forward(
    tmp_path: Path, _recovery_ready: Path
) -> None:
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


def test_readback_fails_closed_on_pending_or_pointer_mismatch(
    tmp_path: Path, _recovery_ready: Path
) -> None:
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
