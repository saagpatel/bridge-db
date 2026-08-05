"""Immutable BridgeDB execution-generation staging and activation.

The runtime remains stdio/client-managed.  This module only owns immutable
source generations, atomic activation pointers, rollback evidence, and drain
requests for superseded generations.  It never installs dependencies, starts a
service, enumerates processes, or terminates a process.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Generator, Iterable
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Literal, cast

from bridge_db import clock

GENERATION_SCHEMA = "BridgeExecutionGenerationV1"
ACTIVATION_SCHEMA = "BridgeExecutionActivationV1"
ACTIVATION_RECEIPT_SCHEMA = "BridgeExecutionActivationReceiptV1"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GENERATION_RE = re.compile(r"^[0-9a-f]{12}-[0-9a-f]{12}$")
_RESERVED_RELEASE_PATHS = frozenset({"generation-manifest.json", "bin/bridge-db-mcp"})
_TENANCY_EVIDENCE_NAME = "tenancy-activation-evidence.json"
_MAX_TENANCY_EVIDENCE_BYTES = 1024 * 1024

DEFAULT_DEPENDENCY_PATHS = ("pyproject.toml", "uv.lock")
DEFAULT_CONTRACT_PATHS = (
    ".codex/verify.commands",
    "config/bridge-db-mcp-immutable",
    "config/com.saagar.bridge-db-checkpoint.plist",
    "integration-spec.md",
    "src/bridge_db/auth.py",
    "src/bridge_db/client_rebinding.py",
    "src/bridge_db/db.py",
    "src/bridge_db/execution_generation.py",
    "src/bridge_db/secure_binding.py",
    "src/bridge_db/server.py",
    "src/bridge_db/shared_runtime.py",
    "src/bridge_db/snapshot_service.py",
    "src/bridge_db/tenancy.py",
    "src/bridge_db/tools/__init__.py",
)
_SHARED_RUNTIME_CONTRACT_PATH = "src/bridge_db/shared_runtime.py"
_PRE_SHARED_RUNTIME_CONTRACT_PATHS = tuple(
    path for path in DEFAULT_CONTRACT_PATHS if path != _SHARED_RUNTIME_CONTRACT_PATH
)


class GenerationContractError(RuntimeError):
    """Fail-closed generation contract error with a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _assert_activation_recovery_ready() -> None:
    """Require the recovery subsystem's own current anchor and seal verdict."""
    from bridge_db import config
    from bridge_db.db import SCHEMA_VERSION
    from bridge_db.recovery import recovery_anchor_inventory
    from bridge_db.recovery_seal import recovery_seal_inventory

    try:
        anchor = recovery_anchor_inventory(
            config.DB_PATH,
            expected_schema_version=SCHEMA_VERSION,
        )
    except Exception as exc:
        raise GenerationContractError(
            "generation.recovery_anchor_unverified"
        ) from exc
    anchor_state = anchor.get("state")
    if anchor.get("ready") is not True or anchor_state != "verified":
        reason_by_state = {
            "missing": "generation.recovery_anchor_missing",
            "stale": "generation.recovery_anchor_stale",
            "invalid": "generation.recovery_anchor_invalid",
        }
        raise GenerationContractError(
            reason_by_state.get(
                str(anchor_state), "generation.recovery_anchor_unverified"
            )
        )
    try:
        seals = recovery_seal_inventory(
            config.DB_PATH,
            expected_schema_version=SCHEMA_VERSION,
            current_anchor=anchor,
        )
    except Exception as exc:
        raise GenerationContractError("generation.recovery_seal_unverified") from exc
    seal_state = seals.get("state")
    if seals.get("ready") is not True or seal_state != "verified":
        reason_by_state = {
            "missing": "generation.recovery_seal_missing",
            "stale": "generation.recovery_seal_stale",
            "recovery_unsealed": "generation.recovery_seal_unsealed",
            "invalid": "generation.recovery_seal_invalid",
        }
        raise GenerationContractError(
            reason_by_state.get(
                str(seal_state), "generation.recovery_seal_unverified"
            )
        )


def _normalize_tenancy_evidence_summary(value: object) -> dict[str, Any]:
    if value is None:
        return {"state": "legacy_unverified"}
    if not isinstance(value, dict):
        raise GenerationContractError("generation.tenancy_evidence_state_invalid")
    summary = {str(key): item for key, item in cast(dict[object, Any], value).items()}
    state = summary.get("state")
    if state in ("inactive", "legacy_unverified", "not_required_for_rollback"):
        if set(summary) != {"state"}:
            raise GenerationContractError("generation.tenancy_evidence_state_invalid")
        return summary
    expected_fields = {
        "schema",
        "state",
        "generation_id",
        "evidence_sha256",
        "policy_sha256",
        "file_sha256",
        "owners",
        "scenarios",
        "observation_counts",
    }
    if (
        state != "verified"
        or set(summary) != expected_fields
        or summary.get("schema") != "BridgeMcpTenancyActivationEvidenceV1"
        or not isinstance(summary.get("generation_id"), str)
        or not _GENERATION_RE.fullmatch(cast(str, summary["generation_id"]))
        or not isinstance(summary.get("evidence_sha256"), str)
        or not _SHA256_RE.fullmatch(cast(str, summary["evidence_sha256"]))
        or not isinstance(summary.get("policy_sha256"), str)
        or not _SHA256_RE.fullmatch(cast(str, summary["policy_sha256"]))
        or not isinstance(summary.get("file_sha256"), str)
        or not _SHA256_RE.fullmatch(cast(str, summary["file_sha256"]))
        or not isinstance(summary.get("owners"), list)
        or not isinstance(summary.get("scenarios"), list)
        or not isinstance(summary.get("observation_counts"), dict)
    ):
        raise GenerationContractError("generation.tenancy_evidence_state_invalid")
    owners = cast(list[object], summary["owners"])
    scenarios = cast(list[object], summary["scenarios"])
    counts = cast(dict[object, object], summary["observation_counts"])
    required_owners = {"claude", "codex", "hermes", "personal_ops"}
    required_scenarios = {
        "abrupt_exit",
        "app_restart",
        "generation_rollover",
        "normal_close",
    }
    if (
        any(not isinstance(owner, str) for owner in owners)
        or any(not isinstance(scenario, str) for scenario in scenarios)
        or owners != sorted(cast(list[str], owners))
        or scenarios != sorted(cast(list[str], scenarios))
        or not required_owners.issubset(set(owners))
        or not required_scenarios.issubset(set(scenarios))
        or set(counts) != set(owners)
        or any(
            not isinstance(count, int) or isinstance(count, bool) or count < 1
            for count in counts.values()
        )
    ):
        raise GenerationContractError("generation.tenancy_evidence_state_invalid")
    return summary


def _load_tenancy_activation_evidence(
    root: Path, selected_path: Path | None, generation_id: str
) -> dict[str, Any]:
    from bridge_db.tenancy import (
        TenancyContractError,
        validate_lifecycle_activation_evidence,
    )

    path = selected_path or root / _TENANCY_EVIDENCE_NAME
    if not path.is_absolute():
        raise GenerationContractError("generation.tenancy_evidence_path_invalid")
    try:
        if path.resolve(strict=True) != path:
            raise GenerationContractError("generation.tenancy_evidence_path_invalid")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError as exc:
        raise GenerationContractError("generation.tenancy_evidence_missing") from exc
    except GenerationContractError:
        raise
    except OSError as exc:
        raise GenerationContractError("generation.tenancy_evidence_path_invalid") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o400
        ):
            raise GenerationContractError("generation.tenancy_evidence_not_private")
        if metadata.st_size < 1 or metadata.st_size > _MAX_TENANCY_EVIDENCE_BYTES:
            raise GenerationContractError("generation.tenancy_evidence_size_invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            encoded = handle.read(_MAX_TENANCY_EVIDENCE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(encoded) > _MAX_TENANCY_EVIDENCE_BYTES:
        raise GenerationContractError("generation.tenancy_evidence_size_invalid")
    try:
        parsed = cast(
            object,
            json.loads(
                encoded,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"invalid JSON constant: {value}")
                ),
            ),
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise GenerationContractError("generation.tenancy_evidence_json_invalid") from exc
    if not isinstance(parsed, dict):
        raise GenerationContractError("generation.tenancy_evidence_json_invalid")
    evidence = {
        str(key): item for key, item in cast(dict[object, Any], parsed).items()
    }
    try:
        summary = validate_lifecycle_activation_evidence(evidence)
    except TenancyContractError as exc:
        reason_by_code = {
            "tenancy.activation_evidence_coverage_missing": (
                "generation.tenancy_evidence_coverage_missing"
            ),
            "tenancy.activation_evidence_digest_mismatch": (
                "generation.tenancy_evidence_digest_mismatch"
            ),
            "tenancy.activation_evidence_requirements_mismatch": (
                "generation.tenancy_evidence_requirements_mismatch"
            ),
            "tenancy.activation_policy_mismatch": (
                "generation.tenancy_evidence_policy_mismatch"
            ),
        }
        raise GenerationContractError(
            reason_by_code.get(
                exc.reason_code, "generation.tenancy_evidence_invalid"
            )
        ) from exc
    if summary.get("generation_id") != generation_id:
        raise GenerationContractError(
            "generation.tenancy_evidence_generation_mismatch"
        )
    return _normalize_tenancy_evidence_summary(
        {**summary, "file_sha256": _sha256_bytes(encoded)}
    )


def _utc_text() -> str:
    return clock.now().isoformat().replace("+00:00", "Z")


def _stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, content: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.pending-"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _guard_absolute_directory(
    path: Path, *, create: bool = False, require_private: bool = True
) -> Path:
    if not path.is_absolute():
        raise GenerationContractError("generation.path_not_absolute")
    if path in (Path("/"), Path.home()):
        raise GenerationContractError("generation.path_too_broad")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists() or not path.is_dir():
        raise GenerationContractError("generation.directory_missing")
    if path.is_symlink() or path.resolve(strict=True) != path:
        raise GenerationContractError("generation.directory_symlink_refused")
    metadata = path.stat()
    if metadata.st_uid != os.getuid():
        raise GenerationContractError("generation.directory_owner_mismatch")
    if require_private and stat.S_IMODE(metadata.st_mode) & 0o022:
        raise GenerationContractError("generation.directory_not_private")
    return path


def _run_git(source: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(source), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise GenerationContractError("generation.git_command_failed")
    return completed.stdout


def _validated_source(source: Path, reviewed_sha: str) -> Path:
    source = _guard_absolute_directory(source, require_private=False)
    if not _SHA_RE.fullmatch(reviewed_sha):
        raise GenerationContractError("generation.reviewed_sha_invalid")
    root = Path(
        _run_git(source, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    ).resolve(strict=True)
    if root != source:
        raise GenerationContractError("generation.source_not_git_root")
    head = _run_git(source, "rev-parse", "HEAD").decode("ascii").strip()
    if head != reviewed_sha:
        raise GenerationContractError("generation.reviewed_sha_mismatch")
    if _run_git(source, "status", "--porcelain=v1", "--untracked-files=all"):
        raise GenerationContractError("generation.source_not_clean")
    return source


def _tracked_paths(source: Path) -> list[Path]:
    raw = _run_git(source, "ls-files", "-z")
    paths: list[Path] = []
    for encoded in raw.split(b"\0"):
        if not encoded:
            continue
        relative = Path(encoded.decode("utf-8"))
        if relative.is_absolute() or ".." in relative.parts:
            raise GenerationContractError("generation.tracked_path_invalid")
        source_path = source / relative
        metadata = source_path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise GenerationContractError("generation.tracked_non_regular_refused")
        paths.append(relative)
    return sorted(paths, key=lambda value: value.as_posix())


def _file_entries(source: Path, paths: Iterable[Path]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for relative in paths:
        source_path = source / relative
        executable = bool(source_path.stat().st_mode & stat.S_IXUSR)
        entries.append(
            {
                "path": relative.as_posix(),
                "sha256": _sha256_file(source_path),
                "executable": executable,
            }
        )
    return entries


def _entries_digest(entries: list[dict[str, Any]]) -> str:
    return _sha256_bytes(_stable_json(entries).encode("utf-8"))


def _selected_entries(
    entries: list[dict[str, Any]], selected_paths: Iterable[str], *, kind: str
) -> list[dict[str, Any]]:
    by_path = {str(entry["path"]): entry for entry in entries}
    selected: list[dict[str, Any]] = []
    for name in selected_paths:
        entry = by_path.get(name)
        if entry is None:
            raise GenerationContractError(f"generation.{kind}_path_missing")
        selected.append(entry)
    return selected


def _copy_tracked_source(
    source: Path, target: Path, entries: list[dict[str, Any]]
) -> None:
    for entry in entries:
        relative = Path(str(entry["path"]))
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        shutil.copyfile(source / relative, destination, follow_symlinks=False)
        os.chmod(destination, 0o755 if entry["executable"] else 0o644)


def _make_launcher(
    *,
    release_path: Path,
    python_executable: Path,
    python_resolved: Path,
    python_sha256: str,
    generation_id: str,
) -> bytes:
    if any(character in str(python_executable) for character in ("\n", "\r", " ")):
        raise GenerationContractError("generation.python_path_not_shebang_safe")
    body = f"""#!{python_executable}
import hashlib
import os
from pathlib import Path
import runpy
import sys

release = {str(release_path)!r}
expected_python = {str(python_resolved)!r}
expected_python_sha256 = {python_sha256!r}
observed_python = Path(sys.executable).resolve(strict=True)
digest = hashlib.sha256()
with observed_python.open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
if str(observed_python) != expected_python or digest.hexdigest() != expected_python_sha256:
    sys.stderr.write("generation.python_identity_mismatch\\n")
    raise SystemExit(78)
os.environ["BRIDGE_DB_GENERATION_MANIFEST"] = release + "/generation-manifest.json"
os.environ["BRIDGE_DB_GENERATION_ID"] = {generation_id!r}
sys.path.insert(0, release + "/src")
runpy.run_module("bridge_db", run_name="__main__")
"""
    return body.encode("utf-8")


def _make_tree_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            raise GenerationContractError("generation.release_symlink_refused")
        if path.is_dir():
            os.chmod(path, 0o555)
        elif path.is_file():
            executable = bool(path.stat().st_mode & stat.S_IXUSR)
            os.chmod(path, 0o555 if executable else 0o444)
        else:
            raise GenerationContractError("generation.release_special_file_refused")
    os.chmod(root, 0o555)


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise GenerationContractError("generation.manifest_unreadable") from exc
    if not isinstance(raw, dict):
        raise GenerationContractError("generation.manifest_invalid")
    return {str(key): value for key, value in cast(dict[object, Any], raw).items()}


def _release_path(root: Path, generation_id: str) -> Path:
    if not _GENERATION_RE.fullmatch(generation_id):
        raise GenerationContractError("generation.id_invalid")
    return root / "releases" / generation_id


def _validated_manifest_entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    source_files = manifest.get("source_files")
    if not isinstance(source_files, list):
        raise GenerationContractError("generation.manifest_files_invalid")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_entry in cast(list[object], source_files):
        if not isinstance(raw_entry, dict):
            raise GenerationContractError("generation.manifest_files_invalid")
        entry = {
            str(key): value for key, value in cast(dict[object, Any], raw_entry).items()
        }
        if set(entry) != {"path", "sha256", "executable"}:
            raise GenerationContractError("generation.manifest_file_shape_invalid")
        path_value = entry["path"]
        digest = entry["sha256"]
        executable = entry["executable"]
        if not isinstance(path_value, str) or not path_value:
            raise GenerationContractError("generation.manifest_path_invalid")
        relative = Path(path_value)
        if (
            relative.is_absolute()
            or relative == Path(".")
            or ".." in relative.parts
            or relative.as_posix() != path_value
            or path_value in _RESERVED_RELEASE_PATHS
        ):
            raise GenerationContractError("generation.manifest_path_invalid")
        if path_value in seen:
            raise GenerationContractError("generation.manifest_path_duplicate")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise GenerationContractError("generation.manifest_digest_invalid")
        if not isinstance(executable, bool):
            raise GenerationContractError("generation.manifest_mode_invalid")
        seen.add(path_value)
        entries.append(entry)
    if entries != sorted(entries, key=lambda item: str(item["path"])):
        raise GenerationContractError("generation.manifest_order_invalid")
    return entries


def _verify_selected_digest(
    manifest: dict[str, Any],
    entries: list[dict[str, Any]],
    *,
    kind: Literal["dependency", "contract"],
    expected_paths: tuple[str, ...],
) -> None:
    paths = manifest.get(f"{kind}_paths")
    selected_paths = expected_paths
    if paths != list(expected_paths):
        source_paths = {str(entry["path"]) for entry in entries}
        legacy_contract = (
            kind == "contract"
            and paths == list(_PRE_SHARED_RUNTIME_CONTRACT_PATHS)
            and _SHARED_RUNTIME_CONTRACT_PATH not in source_paths
        )
        if not legacy_contract:
            raise GenerationContractError(f"generation.{kind}_paths_mismatch")
        selected_paths = _PRE_SHARED_RUNTIME_CONTRACT_PATHS
    selected = _selected_entries(entries, selected_paths, kind=kind)
    if _entries_digest(selected) != manifest.get(f"{kind}_sha256"):
        raise GenerationContractError(f"generation.{kind}_digest_mismatch")


def _verify_python_binding(manifest: dict[str, Any]) -> tuple[Path, str]:
    executable_value = manifest.get("python_executable")
    resolved_value = manifest.get("python_executable_resolved")
    expected_digest = manifest.get("python_sha256")
    if (
        not isinstance(executable_value, str)
        or not isinstance(resolved_value, str)
        or not isinstance(expected_digest, str)
        or not _SHA256_RE.fullmatch(expected_digest)
    ):
        raise GenerationContractError("generation.python_binding_invalid")
    executable = Path(executable_value)
    expected_resolved = Path(resolved_value)
    if (
        not executable.is_absolute()
        or not expected_resolved.is_absolute()
        or any(character in executable_value for character in ("\n", "\r", " "))
    ):
        raise GenerationContractError("generation.python_binding_invalid")
    try:
        observed_resolved = executable.resolve(strict=True)
        metadata = observed_resolved.lstat()
    except OSError as exc:
        raise GenerationContractError("generation.python_executable_missing") from exc
    if observed_resolved != expected_resolved or not stat.S_ISREG(metadata.st_mode):
        raise GenerationContractError("generation.python_identity_mismatch")
    if not metadata.st_mode & stat.S_IXUSR:
        raise GenerationContractError("generation.python_not_executable")
    if _sha256_file(observed_resolved) != expected_digest:
        raise GenerationContractError("generation.python_digest_mismatch")
    return executable, expected_digest


def _verify_exact_release_tree(release: Path, entries: list[dict[str, Any]]) -> None:
    expected_file_modes = {
        str(entry["path"]): 0o555 if entry["executable"] else 0o444 for entry in entries
    }
    expected_file_modes["generation-manifest.json"] = 0o444
    expected_file_modes["bin/bridge-db-mcp"] = 0o555
    expected_directories: set[str] = set()
    for name in expected_file_modes:
        parent = Path(name).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent

    release_metadata = release.lstat()
    if (
        release_metadata.st_uid != os.getuid()
        or stat.S_IMODE(release_metadata.st_mode) != 0o555
    ):
        raise GenerationContractError("generation.release_root_metadata_mismatch")
    observed_files: set[str] = set()
    observed_directories: set[str] = set()
    for candidate in release.rglob("*"):
        relative = candidate.relative_to(release).as_posix()
        metadata = candidate.lstat()
        if metadata.st_uid != os.getuid():
            raise GenerationContractError("generation.release_owner_mismatch")
        if stat.S_ISLNK(metadata.st_mode):
            raise GenerationContractError("generation.release_symlink_refused")
        if stat.S_ISDIR(metadata.st_mode):
            observed_directories.add(relative)
            if stat.S_IMODE(metadata.st_mode) != 0o555:
                raise GenerationContractError(
                    "generation.release_directory_mode_mismatch"
                )
        elif stat.S_ISREG(metadata.st_mode):
            observed_files.add(relative)
            expected_mode = expected_file_modes.get(relative)
            if expected_mode is None:
                raise GenerationContractError("generation.release_extra_file")
            if stat.S_IMODE(metadata.st_mode) != expected_mode:
                raise GenerationContractError("generation.release_file_mode_mismatch")
        else:
            raise GenerationContractError("generation.release_special_file_refused")
    if observed_files != set(expected_file_modes):
        raise GenerationContractError("generation.release_file_set_mismatch")
    if observed_directories != expected_directories:
        raise GenerationContractError("generation.release_directory_set_mismatch")


def verify_generation(root: Path, generation_id: str) -> dict[str, Any]:
    root = _guard_absolute_directory(root)
    _guard_absolute_directory(root / "releases")
    release = _release_path(root, generation_id)
    if not release.exists() or not release.is_dir() or release.is_symlink():
        raise GenerationContractError("generation.release_missing")
    if release.resolve(strict=True).parent != (root / "releases").resolve(strict=True):
        raise GenerationContractError("generation.release_escape_refused")
    manifest_path = release / "generation-manifest.json"
    try:
        manifest_metadata = manifest_path.lstat()
    except OSError as exc:
        raise GenerationContractError("generation.manifest_unreadable") from exc
    if (
        not stat.S_ISREG(manifest_metadata.st_mode)
        or manifest_metadata.st_uid != os.getuid()
        or stat.S_IMODE(manifest_metadata.st_mode) != 0o444
    ):
        raise GenerationContractError("generation.manifest_metadata_mismatch")
    manifest = _read_manifest(manifest_path)
    if (
        manifest.get("schema") != GENERATION_SCHEMA
        or manifest.get("generation_id") != generation_id
    ):
        raise GenerationContractError("generation.manifest_identity_mismatch")
    expected_manifest_fields = {
        "schema",
        "generation_id",
        "reviewed_source_sha",
        "source_tree_sha256",
        "dependency_sha256",
        "dependency_paths",
        "contract_sha256",
        "contract_paths",
        "python_executable",
        "python_executable_resolved",
        "python_sha256",
        "python_binding",
        "dependency_environment_state",
        "database_rollback_contract",
        "launcher_sha256",
        "source_files",
    }
    if set(manifest) != expected_manifest_fields:
        raise GenerationContractError("generation.manifest_shape_invalid")
    reviewed_sha = manifest.get("reviewed_source_sha")
    if not isinstance(reviewed_sha, str) or not _SHA_RE.fullmatch(reviewed_sha):
        raise GenerationContractError("generation.reviewed_sha_invalid")
    entries = _validated_manifest_entries(manifest)
    source_tree_digest = _entries_digest(entries)
    if source_tree_digest != manifest.get("source_tree_sha256"):
        raise GenerationContractError("generation.source_tree_digest_mismatch")
    expected_generation_id = f"{reviewed_sha[:12]}-{source_tree_digest[:12]}"
    if generation_id != expected_generation_id:
        raise GenerationContractError("generation.id_content_mismatch")
    _verify_selected_digest(
        manifest,
        entries,
        kind="dependency",
        expected_paths=DEFAULT_DEPENDENCY_PATHS,
    )
    _verify_selected_digest(
        manifest,
        entries,
        kind="contract",
        expected_paths=DEFAULT_CONTRACT_PATHS,
    )
    if (
        manifest.get("dependency_environment_state")
        != "external_unmanaged_lockfiles_only"
    ):
        raise GenerationContractError("generation.dependency_claim_invalid")
    if (
        manifest.get("python_binding")
        != "external_executable_digest_verified_not_environment_immutable"
    ):
        raise GenerationContractError("generation.python_claim_invalid")
    if manifest.get("database_rollback_contract") != {
        "core_user_version": 23,
        "previous_merged_generation_user_version": 23,
        "previous_merged_generation_sha": (
            "d7272d489873faa5ed84c81734636ffc8cecb095"
        ),
        "snapshot_refusal_extension": "BridgeSnapshotRefusalSchemaV1",
        "compatibility": "additive_extension_ignored_by_previous_runtime",
    }:
        raise GenerationContractError("generation.database_rollback_claim_invalid")
    python_executable, python_sha256 = _verify_python_binding(manifest)
    _verify_exact_release_tree(release, entries)
    for entry in entries:
        relative = Path(str(entry.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise GenerationContractError("generation.manifest_path_invalid")
        candidate = release / relative
        if not candidate.is_file() or candidate.is_symlink():
            raise GenerationContractError("generation.release_file_missing")
        if _sha256_file(candidate) != entry.get("sha256"):
            raise GenerationContractError("generation.release_digest_mismatch")
    launcher = release / "bin" / "bridge-db-mcp"
    if not launcher.is_file() or launcher.is_symlink():
        raise GenerationContractError("generation.launcher_missing")
    expected_launcher = _make_launcher(
        release_path=release,
        python_executable=python_executable,
        python_resolved=Path(str(manifest["python_executable_resolved"])),
        python_sha256=python_sha256,
        generation_id=generation_id,
    )
    expected_launcher_sha256 = _sha256_bytes(expected_launcher)
    if (
        manifest.get("launcher_sha256") != expected_launcher_sha256
        or launcher.read_bytes() != expected_launcher
    ):
        raise GenerationContractError("generation.launcher_digest_mismatch")
    return {
        "ok": True,
        "state": "verified",
        "generation_id": generation_id,
        "reviewed_source_sha": manifest["reviewed_source_sha"],
        "source_tree_sha256": manifest["source_tree_sha256"],
        "dependency_sha256": manifest["dependency_sha256"],
        "contract_sha256": manifest["contract_sha256"],
        "launcher_sha256": manifest["launcher_sha256"],
        "python_executable": str(python_executable),
        "python_sha256": python_sha256,
        "dependency_environment_state": manifest["dependency_environment_state"],
        "database_rollback_contract": manifest["database_rollback_contract"],
        "claim_ceiling": "source_and_interpreter_bound_external_environment_unmanaged",
        "release_path": str(release),
    }


def stage_generation(
    *,
    source: Path,
    root: Path,
    reviewed_sha: str,
    python_executable: Path | None = None,
    dependency_paths: Iterable[str] = DEFAULT_DEPENDENCY_PATHS,
    contract_paths: Iterable[str] = DEFAULT_CONTRACT_PATHS,
) -> dict[str, Any]:
    source = _validated_source(source, reviewed_sha)
    dependency_path_tuple = tuple(dependency_paths)
    contract_path_tuple = tuple(contract_paths)
    if dependency_path_tuple != DEFAULT_DEPENDENCY_PATHS:
        raise GenerationContractError("generation.custom_dependency_contract_refused")
    if contract_path_tuple != DEFAULT_CONTRACT_PATHS:
        raise GenerationContractError("generation.custom_contract_contract_refused")
    lexical_root = root.absolute()
    if (
        lexical_root == source
        or lexical_root.is_relative_to(source)
        or source.is_relative_to(lexical_root)
    ):
        raise GenerationContractError("generation.source_root_overlap")
    root = _guard_absolute_directory(root, create=True)
    releases = _guard_absolute_directory(root / "releases", create=True)
    _guard_absolute_directory(root / "receipts", create=True)
    _guard_absolute_directory(root / "drain", create=True)

    executable = python_executable or Path(sys.executable)
    if not executable.is_absolute():
        raise GenerationContractError("generation.python_executable_not_absolute")
    executable_resolved = executable.resolve(strict=True)
    if not executable_resolved.is_file():
        raise GenerationContractError("generation.python_executable_invalid")
    tracked = _tracked_paths(source)
    entries = _file_entries(source, tracked)
    if _RESERVED_RELEASE_PATHS.intersection(str(entry["path"]) for entry in entries):
        raise GenerationContractError("generation.tracked_path_reserved")
    source_digest = _entries_digest(entries)
    dependency_entries = _selected_entries(
        entries, dependency_path_tuple, kind="dependency"
    )
    contract_entries = _selected_entries(entries, contract_path_tuple, kind="contract")
    dependency_digest = _entries_digest(dependency_entries)
    contract_digest = _entries_digest(contract_entries)
    generation_id = f"{reviewed_sha[:12]}-{source_digest[:12]}"
    release = _release_path(root, generation_id)

    if release.exists():
        verified = verify_generation(root, generation_id)
        return {**verified, "disposition": "preserved_existing"}

    temporary = Path(
        tempfile.mkdtemp(dir=releases, prefix=f".{generation_id}.staging-")
    )
    try:
        _copy_tracked_source(source, temporary, entries)
        if any(
            _sha256_file(temporary / str(entry["path"])) != entry["sha256"]
            for entry in entries
        ):
            raise GenerationContractError("generation.source_changed_during_stage")
        if (
            _tracked_paths(source) != tracked
            or _file_entries(source, tracked) != entries
        ):
            raise GenerationContractError("generation.source_changed_during_stage")
        _validated_source(source, reviewed_sha)
        python_sha256 = _sha256_file(executable_resolved)
        launcher_bytes = _make_launcher(
            release_path=release,
            python_executable=executable,
            python_resolved=executable_resolved,
            python_sha256=python_sha256,
            generation_id=generation_id,
        )
        launcher = temporary / "bin" / "bridge-db-mcp"
        launcher.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        launcher.write_bytes(launcher_bytes)
        os.chmod(launcher, 0o755)
        manifest = {
            "schema": GENERATION_SCHEMA,
            "generation_id": generation_id,
            "reviewed_source_sha": reviewed_sha,
            "source_tree_sha256": source_digest,
            "dependency_sha256": dependency_digest,
            "dependency_paths": [entry["path"] for entry in dependency_entries],
            "contract_sha256": contract_digest,
            "contract_paths": [entry["path"] for entry in contract_entries],
            "python_executable": str(executable),
            "python_executable_resolved": str(executable_resolved),
            "python_sha256": python_sha256,
            "python_binding": "external_executable_digest_verified_not_environment_immutable",
            "dependency_environment_state": "external_unmanaged_lockfiles_only",
            "database_rollback_contract": {
                "core_user_version": 23,
                "previous_merged_generation_user_version": 23,
                "previous_merged_generation_sha": (
                    "d7272d489873faa5ed84c81734636ffc8cecb095"
                ),
                "snapshot_refusal_extension": "BridgeSnapshotRefusalSchemaV1",
                "compatibility": "additive_extension_ignored_by_previous_runtime",
            },
            "launcher_sha256": _sha256_bytes(launcher_bytes),
            "source_files": entries,
        }
        manifest_path = temporary / "generation-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        os.chmod(manifest_path, 0o444)
        _make_tree_read_only(temporary)
        os.replace(temporary, release)
        _fsync_directory(releases)
    except Exception:
        if temporary.exists():
            os.chmod(temporary, 0o700)
            for path in temporary.rglob("*"):
                with suppress(OSError):
                    os.chmod(path, 0o700 if path.is_dir() else 0o600)
            shutil.rmtree(temporary)
        raise

    verified = verify_generation(root, generation_id)
    return {**verified, "disposition": "staged"}


@contextmanager
def _activation_lock(root: Path) -> Generator[None, None, None]:
    lock = root / ".activation.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock, flags, 0o600)
    except OSError as exc:
        raise GenerationContractError("generation.activation_lock_invalid") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise GenerationContractError("generation.activation_lock_invalid")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _pointer_generation(root: Path, name: str) -> str | None:
    pointer = root / name
    if not pointer.exists() and not pointer.is_symlink():
        return None
    if not pointer.is_symlink():
        raise GenerationContractError("generation.pointer_not_symlink")
    target = os.readlink(pointer)
    expected_prefix = "releases/"
    if not target.startswith(expected_prefix) or "/" in target[len(expected_prefix) :]:
        raise GenerationContractError("generation.pointer_target_invalid")
    generation_id = target[len(expected_prefix) :]
    verify_generation(root, generation_id)
    return generation_id


def _replace_pointer(root: Path, name: str, generation_id: str | None) -> None:
    pointer = root / name
    if pointer.exists() and not pointer.is_symlink():
        raise GenerationContractError("generation.pointer_not_symlink")
    if generation_id is None:
        pointer.unlink(missing_ok=True)
        _fsync_directory(root)
        return
    verify_generation(root, generation_id)
    temporary = root / f".{name}.pending-{os.getpid()}"
    temporary.unlink(missing_ok=True)
    os.symlink(f"releases/{generation_id}", temporary)
    os.replace(temporary, pointer)
    _fsync_directory(root)


def _receipt_path(root: Path, operation: str, generation_id: str) -> Path:
    timestamp = clock.now().strftime("%Y%m%dT%H%M%S.%fZ")
    return root / "receipts" / f"{timestamp}-{operation}-{generation_id}.json"


def _write_activation_receipt(
    root: Path, *, operation: str, previous: str | None, current: str
) -> dict[str, Any]:
    readback = _read_activation_without_pending(root)
    receipt = {
        "schema": ACTIVATION_RECEIPT_SCHEMA,
        "operation": operation,
        "recorded_at": _utc_text(),
        "requested_generation": current,
        "previous_generation": previous,
        "readback": readback,
        "outcome": "activated" if readback["state"] == "active" else "readback_failed",
    }
    encoded = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode("utf-8")
    path = _receipt_path(root, operation, current)
    _atomic_write(path, encoded, mode=0o400)
    return {
        **receipt,
        "receipt_path": str(path),
        "receipt_sha256": _sha256_bytes(encoded),
    }


def _mark_draining(
    root: Path, generation_id: str | None, *, superseded_by: str
) -> None:
    if generation_id is None or generation_id == superseded_by:
        return
    verify_generation(root, generation_id)
    marker = {
        "schema": "BridgeGenerationDrainRequestV1",
        "generation_id": generation_id,
        "superseded_by": superseded_by,
        "requested_at": _utc_text(),
        "policy": "cooperative_no_process_termination",
    }
    marker_path = root / "drain" / f"{generation_id}.json"
    if marker_path.exists():
        existing = _read_manifest(marker_path)
        if (
            existing.get("schema") != marker["schema"]
            or existing.get("generation_id") != generation_id
            or existing.get("superseded_by") != superseded_by
            or existing.get("policy") != marker["policy"]
        ):
            raise GenerationContractError("generation.drain_marker_mismatch")
        return
    _atomic_write(
        marker_path,
        (json.dumps(marker, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        mode=0o400,
    )


def _pending_journal(root: Path) -> dict[str, Any] | None:
    path = root / ".activation.pending.json"
    if not path.exists() and not path.is_symlink():
        return None
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GenerationContractError("generation.activation_journal_invalid") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise GenerationContractError("generation.activation_journal_invalid")
    journal = _read_manifest(path)
    legacy_fields = {
        "schema",
        "operation",
        "old_current",
        "old_previous",
        "new_current",
        "created_at",
        "journal_sha256",
    }
    extended_fields = legacy_fields | {
        "old_tenancy_activation_evidence",
        "new_tenancy_activation_evidence",
    }
    if set(journal) not in (legacy_fields, extended_fields) or journal.get(
        "schema"
    ) != ACTIVATION_SCHEMA:
        raise GenerationContractError("generation.activation_journal_invalid")
    body = {key: value for key, value in journal.items() if key != "journal_sha256"}
    if journal.get("journal_sha256") != _sha256_bytes(
        _stable_json(body).encode("utf-8")
    ):
        raise GenerationContractError("generation.activation_journal_digest_mismatch")
    if journal.get("operation") not in ("activate", "rollback"):
        raise GenerationContractError("generation.activation_journal_invalid")
    for field_name in ("old_current", "old_previous"):
        value = journal.get(field_name)
        if value is not None and (
            not isinstance(value, str) or not _GENERATION_RE.fullmatch(value)
        ):
            raise GenerationContractError("generation.activation_journal_invalid")
    new_current = journal.get("new_current")
    if not isinstance(new_current, str) or not _GENERATION_RE.fullmatch(new_current):
        raise GenerationContractError("generation.activation_journal_invalid")
    if set(journal) == extended_fields:
        old_evidence = _normalize_tenancy_evidence_summary(
            journal.get("old_tenancy_activation_evidence")
        )
        new_evidence = _normalize_tenancy_evidence_summary(
            journal.get("new_tenancy_activation_evidence")
        )
    else:
        old_evidence = {"state": "legacy_unverified"}
        new_evidence = {"state": "legacy_unverified"}
    return {
        **journal,
        "old_tenancy_activation_evidence": old_evidence,
        "new_tenancy_activation_evidence": new_evidence,
    }


def _activation_state_matches(
    root: Path,
    *,
    current: str,
    previous: str | None,
    operation: str,
    tenancy_evidence: dict[str, Any],
) -> bool:
    state_path = root / "activation-state.json"
    if not state_path.is_file() or state_path.is_symlink():
        return False
    try:
        state = _read_manifest(state_path)
    except GenerationContractError:
        return False
    try:
        state_evidence = _normalize_tenancy_evidence_summary(
            state.get("tenancy_activation_evidence")
        )
    except GenerationContractError:
        return False
    return bool(
        state.get("schema") == ACTIVATION_SCHEMA
        and state.get("current_generation") == current
        and state.get("previous_generation") == previous
        and state.get("operation") == operation
        and state_evidence == tenancy_evidence
    )


def _remove_pending_journal(root: Path) -> bool:
    """Remove the journal and report whether its directory fsync was verified."""
    (root / ".activation.pending.json").unlink()
    try:
        _fsync_directory(root)
    except OSError:
        return False
    return True


def _restore_activation_state(
    root: Path,
    *,
    current: str | None,
    previous: str | None,
    tenancy_evidence: dict[str, Any],
) -> None:
    state_path = root / "activation-state.json"
    if current is None:
        state_path.unlink(missing_ok=True)
        _fsync_directory(root)
        return
    state = {
        "schema": ACTIVATION_SCHEMA,
        "current_generation": current,
        "previous_generation": previous,
        "activated_at": _utc_text(),
        "operation": "journal_before_map_restore",
        "tenancy_activation_evidence": tenancy_evidence,
    }
    _atomic_write(
        state_path,
        (json.dumps(state, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        mode=0o400,
    )


def _recover_pending_activation(root: Path) -> dict[str, Any] | None:
    """Recover only exact before/partial/committed maps under the activation lock."""
    journal = _pending_journal(root)
    if journal is None:
        return None
    old_current = cast(str | None, journal["old_current"])
    old_previous = cast(str | None, journal["old_previous"])
    new_current = cast(str, journal["new_current"])
    operation = cast(Literal["activate", "rollback"], journal["operation"])
    old_tenancy_evidence = cast(
        dict[str, Any], journal["old_tenancy_activation_evidence"]
    )
    new_tenancy_evidence = cast(
        dict[str, Any], journal["new_tenancy_activation_evidence"]
    )
    current = _pointer_generation(root, "current")
    previous = _pointer_generation(root, "previous")

    if current == new_current and previous == old_current and _activation_state_matches(
        root,
        current=new_current,
        previous=old_current,
        operation=operation,
        tenancy_evidence=new_tenancy_evidence,
    ):
        _mark_draining(root, old_current, superseded_by=new_current)
        receipt = _write_activation_receipt(
            root,
            operation=operation,
            previous=old_current,
            current=new_current,
        )
        if receipt["outcome"] != "activated":
            raise GenerationContractError("generation.activation_recovery_readback_failed")
        journal_removal_verified = _remove_pending_journal(root)
        return {
            **receipt,
            "outcome": (
                "activated_recovered"
                if journal_removal_verified
                else "activated_recovered_journal_fsync_unverified"
            ),
            "recovery_disposition": "committed_finalized",
            "journal_removal": (
                "verified" if journal_removal_verified else "fsync_unverified"
            ),
        }

    allowed_maps = {
        (old_current, old_previous),
        (new_current, old_previous),
        (new_current, old_current),
    }
    if (current, previous) not in allowed_maps:
        raise GenerationContractError("generation.activation_journal_map_mismatch")
    _replace_pointer(root, "current", old_current)
    _replace_pointer(root, "previous", old_previous)
    _restore_activation_state(
        root,
        current=old_current,
        previous=old_previous,
        tenancy_evidence=old_tenancy_evidence,
    )
    if (
        _pointer_generation(root, "current") != old_current
        or _pointer_generation(root, "previous") != old_previous
    ):
        raise GenerationContractError("generation.activation_recovery_readback_failed")
    journal_removal_verified = _remove_pending_journal(root)
    return {
        "schema": ACTIVATION_RECEIPT_SCHEMA,
        "operation": operation,
        "outcome": "before_map_restored",
        "requested_generation": new_current,
        "previous_generation": old_previous,
        "readback": _read_activation_without_pending(root),
        "recovery_disposition": "before_map_restored",
        "journal_removal": (
            "verified" if journal_removal_verified else "fsync_unverified"
        ),
    }


def _activate_locked(
    root: Path,
    *,
    generation_id: str,
    operation: Literal["activate", "rollback"],
    tenancy_evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    verify_generation(root, generation_id)
    old_current = _pointer_generation(root, "current")
    old_previous = _pointer_generation(root, "previous")
    old_readback = _read_activation_without_pending(root)
    if old_readback.get("state") not in ("active", "inactive"):
        raise GenerationContractError("generation.activation_state_mismatch")
    old_tenancy_evidence = cast(
        dict[str, Any], old_readback["tenancy_activation_evidence"]
    )
    new_tenancy_evidence = (
        _normalize_tenancy_evidence_summary(tenancy_evidence)
        if operation == "activate"
        else {"state": "not_required_for_rollback"}
    )
    if old_current == generation_id:
        return {
            "schema": ACTIVATION_RECEIPT_SCHEMA,
            "operation": operation,
            "outcome": "preserved_existing",
            "requested_generation": generation_id,
            "previous_generation": old_previous,
            "readback": read_activation(root),
        }

    pending_body = {
        "schema": ACTIVATION_SCHEMA,
        "operation": operation,
        "old_current": old_current,
        "old_previous": old_previous,
        "new_current": generation_id,
        "created_at": _utc_text(),
        "old_tenancy_activation_evidence": old_tenancy_evidence,
        "new_tenancy_activation_evidence": new_tenancy_evidence,
    }
    pending = {
        **pending_body,
        "journal_sha256": _sha256_bytes(
            _stable_json(pending_body).encode("utf-8")
        ),
    }
    pending_path = root / ".activation.pending.json"
    if pending_path.exists() or pending_path.is_symlink():
        raise GenerationContractError("generation.activation_pending_unrecovered")
    _atomic_write(
        pending_path,
        (json.dumps(pending, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        mode=0o600,
    )
    try:
        _replace_pointer(root, "current", generation_id)
        _replace_pointer(root, "previous", old_current)
        state = {
            "schema": ACTIVATION_SCHEMA,
            "current_generation": generation_id,
            "previous_generation": old_current,
            "activated_at": _utc_text(),
            "operation": operation,
            "tenancy_activation_evidence": new_tenancy_evidence,
        }
        _atomic_write(
            root / "activation-state.json",
            (json.dumps(state, sort_keys=True, indent=2) + "\n").encode("utf-8"),
            mode=0o400,
        )
    except Exception:
        # Restore both exact pointers.  If restoration itself fails the pending
        # journal remains and readback stays explicitly interrupted.
        try:
            _replace_pointer(root, "current", old_current)
            _replace_pointer(root, "previous", old_previous)
            _restore_activation_state(
                root,
                current=old_current,
                previous=old_previous,
                tenancy_evidence=old_tenancy_evidence,
            )
            pending_path.unlink(missing_ok=True)
            _fsync_directory(root)
        except Exception:
            pass
        raise

    try:
        _mark_draining(root, old_current, superseded_by=generation_id)
        receipt = _write_activation_receipt(
            root,
            operation=operation,
            previous=old_current,
            current=generation_id,
        )
        if receipt["outcome"] != "activated":
            raise GenerationContractError("generation.activation_readback_failed")
        journal_removal_verified = _remove_pending_journal(root)
        if not journal_removal_verified:
            return {
                **receipt,
                "outcome": "activated_journal_fsync_unverified",
                "journal_removal": "fsync_unverified",
            }
    except Exception as exc:
        raise GenerationContractError(
            "generation.activation_committed_post_actions_pending"
        ) from exc
    return receipt


def activate_generation(
    root: Path,
    generation_id: str,
    tenancy_evidence_path: Path | None = None,
) -> dict[str, Any]:
    root = _guard_absolute_directory(root)
    with _activation_lock(root):
        recovery = _recover_pending_activation(root)
        if recovery is not None and (
            recovery.get("recovery_disposition") == "committed_finalized"
            or recovery.get("journal_removal") != "verified"
        ):
            return recovery
        tenancy_evidence = _load_tenancy_activation_evidence(
            root, tenancy_evidence_path, generation_id
        )
        _assert_activation_recovery_ready()
        return _activate_locked(
            root,
            generation_id=generation_id,
            operation="activate",
            tenancy_evidence=tenancy_evidence,
        )


def rollback_generation(root: Path) -> dict[str, Any]:
    root = _guard_absolute_directory(root)
    with _activation_lock(root):
        recovery = _recover_pending_activation(root)
        if recovery is not None and (
            recovery.get("recovery_disposition") == "committed_finalized"
            or recovery.get("journal_removal") != "verified"
        ):
            return recovery
        _assert_activation_recovery_ready()
        previous = _pointer_generation(root, "previous")
        if previous is None:
            raise GenerationContractError("generation.rollback_unavailable")
        return _activate_locked(
            root,
            generation_id=previous,
            operation="rollback",
            tenancy_evidence=None,
        )


def read_activation(root: Path) -> dict[str, Any]:
    root = _guard_absolute_directory(root)
    pending_path = root / ".activation.pending.json"
    if pending_path.exists() or pending_path.is_symlink():
        return {
            "schema": ACTIVATION_SCHEMA,
            "state": "interrupted",
            "current_generation": None,
            "previous_generation": None,
            "reason_code": "generation.activation_pending",
        }
    return _read_activation_without_pending(root)


def _read_activation_without_pending(root: Path) -> dict[str, Any]:
    root = _guard_absolute_directory(root)
    current = _pointer_generation(root, "current")
    previous = _pointer_generation(root, "previous")
    if current is None:
        return {
            "schema": ACTIVATION_SCHEMA,
            "state": "inactive",
            "current_generation": None,
            "previous_generation": previous,
            "tenancy_activation_evidence": {"state": "inactive"},
        }
    state_path = root / "activation-state.json"
    state = _read_manifest(state_path)
    if (
        state.get("schema") != ACTIVATION_SCHEMA
        or state.get("current_generation") != current
        or state.get("previous_generation") != previous
    ):
        return {
            "schema": ACTIVATION_SCHEMA,
            "state": "identity_mismatch",
            "current_generation": current,
            "previous_generation": previous,
            "reason_code": "generation.activation_state_mismatch",
        }
    verified = verify_generation(root, current)
    tenancy_evidence = _normalize_tenancy_evidence_summary(
        state.get("tenancy_activation_evidence")
    )
    return {
        "schema": ACTIVATION_SCHEMA,
        "state": "active",
        "current_generation": current,
        "previous_generation": previous,
        "reviewed_source_sha": verified["reviewed_source_sha"],
        "dependency_sha256": verified["dependency_sha256"],
        "contract_sha256": verified["contract_sha256"],
        "python_sha256": verified["python_sha256"],
        "dependency_environment_state": verified["dependency_environment_state"],
        "database_rollback_contract": verified["database_rollback_contract"],
        "claim_ceiling": verified["claim_ceiling"],
        "tenancy_activation_evidence": tenancy_evidence,
        "launcher_path": str(root / "current" / "bin" / "bridge-db-mcp"),
    }


def runtime_generation_identity() -> dict[str, Any]:
    manifest_value = os.environ.get("BRIDGE_DB_GENERATION_MANIFEST")
    manifest_path = (
        Path(manifest_value)
        if manifest_value
        else Path(__file__).resolve().parents[2] / "generation-manifest.json"
    )
    if not manifest_path.is_absolute() or not manifest_path.is_file():
        return {
            "schema": GENERATION_SCHEMA,
            "state": "mutable_direct_path",
            "generation_id": None,
            "reviewed_source_sha": None,
            "manifest_path": None,
        }
    try:
        manifest = _read_manifest(manifest_path)
    except GenerationContractError as exc:
        return {
            "schema": GENERATION_SCHEMA,
            "state": "unverified",
            "generation_id": None,
            "reviewed_source_sha": None,
            "manifest_path": str(manifest_path),
            "reason_code": exc.reason_code,
        }
    generation_id = manifest.get("generation_id")
    if (
        manifest.get("schema") != GENERATION_SCHEMA
        or not isinstance(generation_id, str)
        or os.environ.get("BRIDGE_DB_GENERATION_ID", generation_id) != generation_id
    ):
        return {
            "schema": GENERATION_SCHEMA,
            "state": "identity_mismatch",
            "generation_id": None,
            "reviewed_source_sha": None,
            "manifest_path": str(manifest_path),
        }
    release = manifest_path.parent
    if release.parent.name != "releases" or release.name != generation_id:
        return {
            "schema": GENERATION_SCHEMA,
            "state": "unverified",
            "generation_id": generation_id,
            "reviewed_source_sha": manifest.get("reviewed_source_sha"),
            "manifest_path": str(manifest_path),
            "reason_code": "generation.runtime_layout_invalid",
        }
    try:
        verified = verify_generation(release.parent.parent, generation_id)
    except GenerationContractError as exc:
        return {
            "schema": GENERATION_SCHEMA,
            "state": "unverified",
            "generation_id": generation_id,
            "reviewed_source_sha": manifest.get("reviewed_source_sha"),
            "manifest_path": str(manifest_path),
            "reason_code": exc.reason_code,
        }
    return {
        "schema": GENERATION_SCHEMA,
        "state": "verified",
        "generation_id": generation_id,
        "reviewed_source_sha": manifest.get("reviewed_source_sha"),
        "dependency_sha256": manifest.get("dependency_sha256"),
        "contract_sha256": manifest.get("contract_sha256"),
        "python_sha256": verified["python_sha256"],
        "dependency_environment_state": verified["dependency_environment_state"],
        "database_rollback_contract": verified["database_rollback_contract"],
        "claim_ceiling": verified["claim_ceiling"],
        "manifest_path": str(manifest_path),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m bridge_db.execution_generation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    stage = subparsers.add_parser("stage")
    stage.add_argument("--source", type=Path, required=True)
    stage.add_argument("--root", type=Path, required=True)
    stage.add_argument("--reviewed-sha", required=True)
    stage.add_argument("--python-executable", type=Path)
    activate = subparsers.add_parser("activate")
    activate.add_argument("--root", type=Path, required=True)
    activate.add_argument("--generation-id", required=True)
    activate.add_argument("--tenancy-evidence", type=Path)
    readback = subparsers.add_parser("readback")
    readback.add_argument("--root", type=Path, required=True)
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--root", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--generation-id", required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        if args.command == "stage":
            result = stage_generation(
                source=args.source,
                root=args.root,
                reviewed_sha=args.reviewed_sha,
                python_executable=args.python_executable,
            )
        elif args.command == "activate":
            result = activate_generation(
                args.root, args.generation_id, args.tenancy_evidence
            )
        elif args.command == "readback":
            result = read_activation(args.root)
        elif args.command == "rollback":
            result = rollback_generation(args.root)
        else:
            result = verify_generation(args.root, args.generation_id)
    except GenerationContractError as exc:
        print(json.dumps({"ok": False, "reason_code": exc.reason_code}, sort_keys=True))
        raise SystemExit(1) from exc
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
