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
import tomllib
from collections.abc import Generator, Iterable
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Literal, cast

from bridge_db import clock

GENERATION_SCHEMA = "BridgeExecutionGenerationV1"
ACTIVATION_SCHEMA = "BridgeExecutionActivationV1"
ACTIVATION_RECEIPT_SCHEMA = "BridgeExecutionActivationReceiptV1"
BOOTSTRAP_ADOPTION_RECEIPT_SCHEMA = "BridgeExecutionBootstrapAdoptionReceiptV1"
RUNTIME_DEPENDENCY_EVIDENCE_SCHEMA = "BridgeRuntimeDependencyEvidenceV1"
RUNTIME_DEPENDENCY_BUNDLE_SCHEMA = "BridgeRuntimeDependencyBundleV1"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GENERATION_RE = re.compile(r"^[0-9a-f]{12}-[0-9a-f]{12}$")
_RESERVED_RELEASE_PATHS = frozenset({"generation-manifest.json", "bin/bridge-db-mcp"})
_TENANCY_EVIDENCE_NAME = "tenancy-activation-evidence.json"
_MAX_TENANCY_EVIDENCE_BYTES = 1024 * 1024
RUNTIME_DEPENDENCY_STATE = "immutable_generation_runtime_dependency_bundle_verified"
RUNTIME_DEPENDENCY_CLAIM_CEILING = (
    "source_interpreter_and_generation_immutable_runtime_dependencies_bound"
)
EXTERNAL_RUNTIME_DEPENDENCY_STATE = "external_runtime_dependency_files_verified"
SUPPORTED_RUNTIME_DEPENDENCY_STATES = (
    RUNTIME_DEPENDENCY_STATE,
    EXTERNAL_RUNTIME_DEPENDENCY_STATE,
)
EXTERNAL_RUNTIME_DEPENDENCY_CLAIM_CEILING = (
    "source_interpreter_and_runtime_dependencies_bound_external_os_unmanaged"
)
LEGACY_RUNTIME_DEPENDENCY_STATE = "external_unmanaged_lockfiles_only"
LEGACY_RUNTIME_DEPENDENCY_CLAIM_CEILING = (
    "source_and_interpreter_bound_external_environment_unmanaged"
)

DEFAULT_DEPENDENCY_PATHS = ("pyproject.toml", "uv.lock")
DEFAULT_RUNTIME_DEPENDENCY_DISTRIBUTIONS = (
    "aiosqlite",
    "annotated-types",
    "anyio",
    "attrs",
    "certifi",
    "cffi",
    "click",
    "cryptography",
    "h11",
    "httpcore",
    "httpx",
    "httpx-sse",
    "idna",
    "jsonschema",
    "jsonschema-specifications",
    "mcp",
    "pycparser",
    "pydantic",
    "pydantic-core",
    "pydantic-settings",
    "pyjwt",
    "python-dotenv",
    "python-multipart",
    "referencing",
    "rpds-py",
    "sse-starlette",
    "starlette",
    "typing-extensions",
    "typing-inspection",
    "uvicorn",
)
DEFAULT_CONTRACT_PATHS = (
    ".codex/verify.commands",
    "config/bridge-db-mcp-immutable",
    "config/com.saagar.bridge-db-checkpoint.plist",
    "integration-spec.md",
    "src/bridge_db/auth.py",
    "src/bridge_db/client_rebinding.py",
    "src/bridge_db/db.py",
    "src/bridge_db/execution_generation.py",
    "src/bridge_db/owner_delegation.py",
    "src/bridge_db/secure_binding.py",
    "src/bridge_db/server.py",
    "src/bridge_db/shared_runtime.py",
    "src/bridge_db/snapshot_service.py",
    "src/bridge_db/tenancy.py",
    "src/bridge_db/tools/__init__.py",
)
_SHARED_RUNTIME_CONTRACT_PATH = "src/bridge_db/shared_runtime.py"
_OWNER_DELEGATION_SOURCE_PATH = "src/bridge_db/owner_delegation.py"
_RUNTIME_BUNDLE_ROOT = Path("runtime/site-packages")
_PRE_SHARED_RUNTIME_CONTRACT_PATHS = tuple(
    path for path in DEFAULT_CONTRACT_PATHS if path != _SHARED_RUNTIME_CONTRACT_PATH
)
_PRE_OWNER_DELEGATION_CONTRACT_PATHS = tuple(
    path for path in DEFAULT_CONTRACT_PATHS if path != _OWNER_DELEGATION_SOURCE_PATH
)
_PRE_SHARED_AND_OWNER_DELEGATION_CONTRACT_PATHS = tuple(
    path
    for path in DEFAULT_CONTRACT_PATHS
    if path not in {_SHARED_RUNTIME_CONTRACT_PATH, _OWNER_DELEGATION_SOURCE_PATH}
)
_LEGACY_DATABASE_ROLLBACK_CONTRACT = {
    "core_user_version": 23,
    "previous_merged_generation_user_version": 23,
    "previous_merged_generation_sha": "d7272d489873faa5ed84c81734636ffc8cecb095",
    "snapshot_refusal_extension": "BridgeSnapshotRefusalSchemaV1",
    "compatibility": "additive_extension_ignored_by_previous_runtime",
}


def _database_rollback_contract(
    entries: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Bind additive v23 extensions while preserving old-manifest verification."""
    paths = {str(entry.get("path")) for entry in entries}
    if _OWNER_DELEGATION_SOURCE_PATH not in paths:
        return dict(_LEGACY_DATABASE_ROLLBACK_CONTRACT)
    return {
        **_LEGACY_DATABASE_ROLLBACK_CONTRACT,
        "owner_delegation_extension": "BridgeOwnerDelegationSchemaV1",
        "compatibility": "additive_extensions_ignored_by_previous_runtime",
    }


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
        raise GenerationContractError("generation.recovery_anchor_unverified") from exc
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
            reason_by_state.get(str(seal_state), "generation.recovery_seal_unverified")
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
        raise GenerationContractError(
            "generation.tenancy_evidence_path_invalid"
        ) from exc
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
        raise GenerationContractError(
            "generation.tenancy_evidence_json_invalid"
        ) from exc
    if not isinstance(parsed, dict):
        raise GenerationContractError("generation.tenancy_evidence_json_invalid")
    evidence = {str(key): item for key, item in cast(dict[object, Any], parsed).items()}
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
            reason_by_code.get(exc.reason_code, "generation.tenancy_evidence_invalid")
        ) from exc
    if summary.get("generation_id") != generation_id:
        raise GenerationContractError("generation.tenancy_evidence_generation_mismatch")
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


def _runtime_dependency_collector_script(distributions: tuple[str, ...]) -> str:
    names = json.dumps(list(distributions), sort_keys=True, separators=(",", ":"))
    return f"""
from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import stat
import sys

NAMES = {names}
SCHEMA = {RUNTIME_DEPENDENCY_EVIDENCE_SCHEMA!r}


def stable_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def file_identity(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def stat_identity(path):
    try:
        metadata = path.lstat()
    except OSError:
        fail("generation.runtime_dependency_file_missing")
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        fail("generation.runtime_dependency_file_invalid")
    return file_identity(metadata)


def open_runtime_file(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        fail("generation.runtime_dependency_file_missing")
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            fail("generation.runtime_dependency_file_invalid")
        opened_identity = file_identity(metadata)
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            fail("generation.runtime_dependency_file_missing")
        if (
            stat_identity(path) != opened_identity
            or stat_identity(resolved) != opened_identity
        ):
            fail("generation.runtime_dependency_file_changed_during_scan")
        return {{
            "descriptor": descriptor,
            "path": path,
            "resolved": resolved,
            "identity": opened_identity,
            "metadata": metadata,
        }}
    except Exception:
        os.close(descriptor)
        raise


def revalidate_open_file(opened):
    try:
        current_identity = file_identity(os.fstat(opened["descriptor"]))
    except OSError:
        fail("generation.runtime_dependency_file_changed_during_scan")
    if (
        current_identity != opened["identity"]
        or stat_identity(opened["path"]) != opened["identity"]
        or stat_identity(opened["resolved"]) != opened["identity"]
    ):
        fail("generation.runtime_dependency_file_changed_during_scan")


def sha256_open_file(opened):
    digest = hashlib.sha256()
    descriptor = opened["descriptor"]
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError:
        fail("generation.runtime_dependency_file_changed_during_scan")
    while True:
        try:
            chunk = os.read(descriptor, 1024 * 1024)
        except OSError:
            fail("generation.runtime_dependency_file_changed_during_scan")
        if not chunk:
            break
        digest.update(chunk)
    revalidate_open_file(opened)
    return digest.hexdigest()


def close_open_files(opened_files):
    for opened in opened_files:
        try:
            os.close(opened["descriptor"])
        except OSError:
            pass


def dependency_file_entry(record_path, opened):
    metadata = opened["metadata"]
    return {{
        "record_path": record_path,
        "path": str(opened["resolved"]),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": stat.S_IMODE(metadata.st_mode),
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "ctime_ns": metadata.st_ctime_ns,
        "sha256": sha256_open_file(opened),
    }}


def collect_distribution_open_files(dist, files):
    opened_files = []
    for package_path in files:
        located = Path(str(dist.locate_file(package_path)))
        opened_files.append(
            (str(package_path).replace(os.sep, "/"), open_runtime_file(located))
        )
    return opened_files


def fail(reason_code):
    print(json.dumps({{"ok": False, "reason_code": reason_code}}, sort_keys=True))
    raise SystemExit(1)


distribution_inputs = []
all_opened_files = []
try:
    for name in NAMES:
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            fail("generation.runtime_dependency_missing")
        files = dist.files
        if files is None:
            fail("generation.runtime_dependency_files_unavailable")
        opened_files = collect_distribution_open_files(dist, files)
        all_opened_files.extend(opened for _record_path, opened in opened_files)
        distribution_inputs.append(
            {{
                "distribution": name,
                "version": dist.version,
                "opened_files": opened_files,
            }}
        )

    for opened in all_opened_files:
        revalidate_open_file(opened)

    items = []
    for distribution_input in distribution_inputs:
        file_entries = [
            dependency_file_entry(record_path, opened)
            for record_path, opened in distribution_input["opened_files"]
        ]
        file_entries.sort(key=lambda item: (item["record_path"], item["path"]))
        for _record_path, opened in distribution_input["opened_files"]:
            revalidate_open_file(opened)
        distribution = {{
            "distribution": distribution_input["distribution"],
            "version": distribution_input["version"],
            "file_count": len(file_entries),
            "files": file_entries,
        }}
        distribution["sha256"] = hashlib.sha256(stable_json(distribution)).hexdigest()
        items.append(distribution)

    for opened in all_opened_files:
        revalidate_open_file(opened)

    evidence = {{
        "schema": SCHEMA,
        "state": "verified",
        "python_executable": str(Path(sys.executable).resolve(strict=True)),
        "distributions": items,
    }}
    evidence["sha256"] = hashlib.sha256(stable_json(evidence)).hexdigest()
    print(json.dumps({{"ok": True, "evidence": evidence}}, sort_keys=True))
finally:
    close_open_files(all_opened_files)
"""


def _runtime_dependency_evidence(
    python_executable: Path,
    distributions: tuple[str, ...] = DEFAULT_RUNTIME_DEPENDENCY_DISTRIBUTIONS,
) -> dict[str, Any]:
    completed = subprocess.run(
        [
            str(python_executable),
            "-I",
            "-c",
            _runtime_dependency_collector_script(distributions),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        payload = cast(object, json.loads(completed.stdout))
    except json.JSONDecodeError as exc:
        raise GenerationContractError(
            "generation.runtime_dependency_probe_failed"
        ) from exc
    if not isinstance(payload, dict):
        raise GenerationContractError("generation.runtime_dependency_probe_failed")
    result = {
        str(key): value for key, value in cast(dict[object, Any], payload).items()
    }
    if completed.returncode != 0 or result.get("ok") is not True:
        reason = result.get("reason_code")
        raise GenerationContractError(
            reason
            if isinstance(reason, str)
            else "generation.runtime_dependency_probe_failed"
        )
    evidence = result.get("evidence")
    if not isinstance(evidence, dict):
        raise GenerationContractError("generation.runtime_dependency_probe_failed")
    return _validate_runtime_dependency_evidence(cast(object, evidence))


def _validate_runtime_dependency_evidence(evidence: object) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        raise GenerationContractError("generation.runtime_dependency_evidence_invalid")
    parsed = {
        str(key): value for key, value in cast(dict[object, Any], evidence).items()
    }
    if set(parsed) != {
        "schema",
        "state",
        "python_executable",
        "distributions",
        "sha256",
    }:
        raise GenerationContractError("generation.runtime_dependency_evidence_invalid")
    distributions = parsed.get("distributions")
    digest = parsed.get("sha256")
    if (
        parsed.get("schema") != RUNTIME_DEPENDENCY_EVIDENCE_SCHEMA
        or parsed.get("state") != "verified"
        or not isinstance(parsed.get("python_executable"), str)
        or not isinstance(distributions, list)
        or not isinstance(digest, str)
        or not _SHA256_RE.fullmatch(digest)
    ):
        raise GenerationContractError("generation.runtime_dependency_evidence_invalid")
    for raw_distribution in cast(list[object], distributions):
        if not isinstance(raw_distribution, dict):
            raise GenerationContractError(
                "generation.runtime_dependency_evidence_invalid"
            )
        distribution = {
            str(key): value
            for key, value in cast(dict[object, Any], raw_distribution).items()
        }
        if set(distribution) != {
            "distribution",
            "version",
            "file_count",
            "files",
            "sha256",
        }:
            raise GenerationContractError(
                "generation.runtime_dependency_evidence_invalid"
            )
        files = distribution.get("files")
        if (
            not isinstance(distribution.get("distribution"), str)
            or not isinstance(distribution.get("version"), str)
            or not isinstance(distribution.get("file_count"), int)
            or isinstance(distribution.get("file_count"), bool)
            or not isinstance(files, list)
            or not isinstance(distribution.get("sha256"), str)
            or not _SHA256_RE.fullmatch(str(distribution["sha256"]))
        ):
            raise GenerationContractError(
                "generation.runtime_dependency_evidence_invalid"
            )
        file_items = cast(list[object], files)
        if distribution["file_count"] != len(file_items):
            raise GenerationContractError(
                "generation.runtime_dependency_evidence_invalid"
            )
        for raw_file in file_items:
            if not isinstance(raw_file, dict):
                raise GenerationContractError(
                    "generation.runtime_dependency_evidence_invalid"
                )
            file_entry = {
                str(key): value
                for key, value in cast(dict[object, Any], raw_file).items()
            }
            if set(file_entry) != {
                "record_path",
                "path",
                "device",
                "inode",
                "mode",
                "size",
                "mtime_ns",
                "ctime_ns",
                "sha256",
            }:
                raise GenerationContractError(
                    "generation.runtime_dependency_evidence_invalid"
                )
            if (
                not isinstance(file_entry["record_path"], str)
                or not isinstance(file_entry["path"], str)
                or not isinstance(file_entry["sha256"], str)
                or not _SHA256_RE.fullmatch(str(file_entry["sha256"]))
            ):
                raise GenerationContractError(
                    "generation.runtime_dependency_evidence_invalid"
                )
            for field_name in (
                "device",
                "inode",
                "mode",
                "size",
                "mtime_ns",
                "ctime_ns",
            ):
                value = file_entry[field_name]
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise GenerationContractError(
                        "generation.runtime_dependency_evidence_invalid"
                    )
        without_digest = {
            key: value for key, value in distribution.items() if key != "sha256"
        }
        if distribution["sha256"] != _sha256_bytes(
            _stable_json(without_digest).encode("utf-8")
        ):
            raise GenerationContractError(
                "generation.runtime_dependency_evidence_invalid"
            )
    without_digest = {key: value for key, value in parsed.items() if key != "sha256"}
    if parsed["sha256"] != _sha256_bytes(_stable_json(without_digest).encode("utf-8")):
        raise GenerationContractError("generation.runtime_dependency_evidence_invalid")
    return parsed


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _validate_locked_runtime_dependencies(
    source: Path, evidence: dict[str, Any]
) -> None:
    try:
        lock = cast(object, tomllib.loads((source / "uv.lock").read_text("utf-8")))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GenerationContractError("generation.dependency_lock_invalid") from exc
    if not isinstance(lock, dict):
        raise GenerationContractError("generation.dependency_lock_invalid")
    lock_mapping = cast(dict[object, object], lock)
    if not isinstance(lock_mapping.get("package"), list):
        raise GenerationContractError("generation.dependency_lock_invalid")
    locked: dict[str, set[str]] = {}
    for raw_package in cast(list[object], lock_mapping["package"]):
        if not isinstance(raw_package, dict):
            raise GenerationContractError("generation.dependency_lock_invalid")
        package = cast(dict[object, object], raw_package)
        name = package.get("name")
        version = package.get("version")
        if isinstance(name, str) and isinstance(version, str):
            locked.setdefault(_normalized_distribution_name(name), set()).add(version)
    for raw_distribution in cast(list[dict[str, Any]], evidence["distributions"]):
        name = _normalized_distribution_name(str(raw_distribution["distribution"]))
        version = str(raw_distribution["version"])
        if version not in locked.get(name, set()):
            raise GenerationContractError("generation.runtime_dependency_lock_mismatch")


def _runtime_bundle_entry_path(record_path: str) -> Path | None:
    candidate = Path(record_path)
    if candidate.is_absolute() or candidate == Path(".") or ".." in candidate.parts:
        return None
    if candidate.as_posix() != record_path:
        raise GenerationContractError("generation.runtime_dependency_path_invalid")
    return _RUNTIME_BUNDLE_ROOT / candidate


def _copy_bound_runtime_file(
    source: Path, destination: Path, expected: dict[str, Any]
) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise GenerationContractError(
            "generation.runtime_dependency_file_missing"
        ) from exc
    try:
        before = os.fstat(descriptor)
        expected_identity = (
            expected["device"],
            expected["inode"],
            expected["mode"],
            expected["size"],
            expected["mtime_ns"],
            expected["ctime_ns"],
        )
        observed_identity = (
            before.st_dev,
            before.st_ino,
            stat.S_IMODE(before.st_mode),
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if not stat.S_ISREG(before.st_mode) or observed_identity != expected_identity:
            raise GenerationContractError(
                "generation.runtime_dependency_file_changed_during_copy"
            )
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        digest = hashlib.sha256()
        with destination.open("wb") as target:
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
                digest.update(chunk)
        after = os.fstat(descriptor)
        after_identity = (
            after.st_dev,
            after.st_ino,
            stat.S_IMODE(after.st_mode),
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if (
            after_identity != expected_identity
            or digest.hexdigest() != expected["sha256"]
        ):
            raise GenerationContractError(
                "generation.runtime_dependency_file_changed_during_copy"
            )
        executable = bool(int(expected["mode"]) & stat.S_IXUSR)
        os.chmod(destination, 0o755 if executable else 0o644)
        return {
            "path": destination.as_posix(),
            "sha256": digest.hexdigest(),
            "executable": executable,
        }
    finally:
        os.close(descriptor)


def _copy_runtime_dependency_bundle(
    *, temporary: Path, source_evidence: dict[str, Any]
) -> dict[str, Any]:
    distributions: list[dict[str, Any]] = []
    unique_files: dict[str, dict[str, Any]] = {}
    for raw_distribution in cast(
        list[dict[str, Any]], source_evidence["distributions"]
    ):
        distribution_files: list[str] = []
        for raw_file in cast(list[dict[str, Any]], raw_distribution["files"]):
            relative = _runtime_bundle_entry_path(str(raw_file["record_path"]))
            if relative is None:
                continue
            relative_text = relative.as_posix()
            destination = temporary / relative
            prior = unique_files.get(relative_text)
            if prior is None:
                copied = _copy_bound_runtime_file(
                    Path(str(raw_file["path"])), destination, raw_file
                )
                copied["path"] = relative_text
                unique_files[relative_text] = copied
            elif prior["sha256"] != raw_file["sha256"] or prior[
                "executable"
            ] is not bool(int(raw_file["mode"]) & stat.S_IXUSR):
                raise GenerationContractError(
                    "generation.runtime_dependency_bundle_collision"
                )
            distribution_files.append(relative_text)
        distribution_files.sort()
        if not distribution_files:
            raise GenerationContractError(
                "generation.runtime_dependency_bundle_empty_distribution"
            )
        distributions.append(
            {
                "distribution": raw_distribution["distribution"],
                "version": raw_distribution["version"],
                "files": distribution_files,
            }
        )
    files = [unique_files[path] for path in sorted(unique_files)]
    body = {
        "schema": RUNTIME_DEPENDENCY_BUNDLE_SCHEMA,
        "state": "verified",
        "root": _RUNTIME_BUNDLE_ROOT.as_posix(),
        "distributions": distributions,
        "files": files,
    }
    return {
        **body,
        "sha256": _sha256_bytes(_stable_json(body).encode("utf-8")),
    }


def _validate_runtime_dependency_bundle(evidence: object) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        raise GenerationContractError("generation.runtime_dependency_evidence_invalid")
    parsed = {
        str(key): value for key, value in cast(dict[object, Any], evidence).items()
    }
    if set(parsed) != {
        "schema",
        "state",
        "root",
        "distributions",
        "files",
        "sha256",
    }:
        raise GenerationContractError("generation.runtime_dependency_evidence_invalid")
    if (
        parsed.get("schema") != RUNTIME_DEPENDENCY_BUNDLE_SCHEMA
        or parsed.get("state") != "verified"
        or parsed.get("root") != _RUNTIME_BUNDLE_ROOT.as_posix()
        or not isinstance(parsed.get("distributions"), list)
        or not isinstance(parsed.get("files"), list)
        or not isinstance(parsed.get("sha256"), str)
        or not _SHA256_RE.fullmatch(str(parsed["sha256"]))
    ):
        raise GenerationContractError("generation.runtime_dependency_evidence_invalid")
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_file in cast(list[object], parsed["files"]):
        if not isinstance(raw_file, dict):
            raise GenerationContractError(
                "generation.runtime_dependency_evidence_invalid"
            )
        item = {
            str(key): value for key, value in cast(dict[object, Any], raw_file).items()
        }
        if set(item) != {"path", "sha256", "executable"}:
            raise GenerationContractError(
                "generation.runtime_dependency_evidence_invalid"
            )
        path = item.get("path")
        digest = item.get("sha256")
        if (
            not isinstance(path, str)
            or not path.startswith(_RUNTIME_BUNDLE_ROOT.as_posix() + "/")
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or path in seen
            or not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
            or not isinstance(item.get("executable"), bool)
        ):
            raise GenerationContractError(
                "generation.runtime_dependency_evidence_invalid"
            )
        seen.add(path)
        files.append(item)
    if files != sorted(files, key=lambda item: str(item["path"])):
        raise GenerationContractError("generation.runtime_dependency_evidence_invalid")
    known_files = set(seen)
    for raw_distribution in cast(list[object], parsed["distributions"]):
        if not isinstance(raw_distribution, dict):
            raise GenerationContractError(
                "generation.runtime_dependency_evidence_invalid"
            )
        item = {
            str(key): value
            for key, value in cast(dict[object, Any], raw_distribution).items()
        }
        distribution_files = item.get("files")
        if (
            set(item) != {"distribution", "version", "files"}
            or not isinstance(item.get("distribution"), str)
            or not isinstance(item.get("version"), str)
            or not isinstance(distribution_files, list)
            or not distribution_files
            or any(
                not isinstance(path, str) or path not in known_files
                for path in cast(list[object], distribution_files)
            )
            or distribution_files != sorted(set(cast(list[str], distribution_files)))
        ):
            raise GenerationContractError(
                "generation.runtime_dependency_evidence_invalid"
            )
    without_digest = {key: value for key, value in parsed.items() if key != "sha256"}
    if parsed["sha256"] != _sha256_bytes(_stable_json(without_digest).encode("utf-8")):
        raise GenerationContractError("generation.runtime_dependency_evidence_invalid")
    return parsed


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
    bundled_runtime: bool = True,
) -> bytes:
    if any(character in str(python_executable) for character in ("\n", "\r", " ")):
        raise GenerationContractError("generation.python_path_not_shebang_safe")
    path_setup = (
        """os.environ.pop("PYTHONPATH", None)
stdlib_roots = {
    str(Path(value).resolve())
    for key in ("stdlib", "platstdlib")
    if (value := sysconfig.get_path(key))
}
stdlib_paths = []
for entry in sys.path:
    if not entry:
        continue
    candidate = Path(entry).resolve()
    if any(
        candidate == Path(root) or candidate.is_relative_to(Path(root))
        for root in stdlib_roots
    ) and "site-packages" not in candidate.parts and "dist-packages" not in candidate.parts:
        stdlib_paths.append(str(candidate))
sys.path[:] = [release + "/src", release + "/runtime/site-packages", *stdlib_paths]"""
        if bundled_runtime
        else 'sys.path.insert(0, release + "/src")'
    )
    sysconfig_import = "import sysconfig\n" if bundled_runtime else ""
    body = f"""#!{python_executable}
import hashlib
import os
from pathlib import Path
import runpy
import sys
{sysconfig_import.rstrip()}

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
{path_setup}
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
        legacy_contract_paths: tuple[str, ...] | None = None
        if kind == "contract":
            candidates = (
                (
                    _PRE_SHARED_RUNTIME_CONTRACT_PATHS,
                    {_SHARED_RUNTIME_CONTRACT_PATH},
                ),
                (
                    _PRE_OWNER_DELEGATION_CONTRACT_PATHS,
                    {_OWNER_DELEGATION_SOURCE_PATH},
                ),
                (
                    _PRE_SHARED_AND_OWNER_DELEGATION_CONTRACT_PATHS,
                    {
                        _SHARED_RUNTIME_CONTRACT_PATH,
                        _OWNER_DELEGATION_SOURCE_PATH,
                    },
                ),
            )
            legacy_contract_paths = next(
                (
                    candidate_paths
                    for candidate_paths, absent_paths in candidates
                    if paths == list(candidate_paths)
                    and absent_paths.isdisjoint(source_paths)
                ),
                None,
            )
        if legacy_contract_paths is None:
            raise GenerationContractError(f"generation.{kind}_paths_mismatch")
        selected_paths = legacy_contract_paths
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


def _verify_exact_release_tree(
    release: Path,
    entries: list[dict[str, Any]],
    runtime_entries: list[dict[str, Any]] | None = None,
) -> None:
    expected_file_modes = {
        str(entry["path"]): 0o555 if entry["executable"] else 0o444 for entry in entries
    }
    for entry in runtime_entries or []:
        path = str(entry["path"])
        if path in expected_file_modes:
            raise GenerationContractError("generation.release_path_collision")
        expected_file_modes[path] = 0o555 if entry["executable"] else 0o444
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
    legacy_manifest_fields = {
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
    expected_manifest_fields = legacy_manifest_fields | {
        "runtime_dependency_sha256",
        "runtime_dependency_evidence",
    }
    manifest_fields = set(manifest)
    if manifest_fields not in (legacy_manifest_fields, expected_manifest_fields):
        raise GenerationContractError("generation.manifest_shape_invalid")
    reviewed_sha = manifest.get("reviewed_source_sha")
    if not isinstance(reviewed_sha, str) or not _SHA_RE.fullmatch(reviewed_sha):
        raise GenerationContractError("generation.reviewed_sha_invalid")
    entries = _validated_manifest_entries(manifest)
    legacy_runtime_dependency_binding = manifest_fields == legacy_manifest_fields
    if legacy_runtime_dependency_binding and any(
        entry["path"] == _SHARED_RUNTIME_CONTRACT_PATH for entry in entries
    ):
        raise GenerationContractError("generation.manifest_shape_invalid")
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
    runtime_dependency_evidence: dict[str, Any] | None = None
    runtime_bundle_entries: list[dict[str, Any]] = []
    runtime_dependency_sha256: str | None = None
    runtime_dependency_state = "legacy_unverified"
    claim_ceiling = LEGACY_RUNTIME_DEPENDENCY_CLAIM_CEILING
    if legacy_runtime_dependency_binding:
        if (
            manifest.get("dependency_environment_state")
            != LEGACY_RUNTIME_DEPENDENCY_STATE
        ):
            raise GenerationContractError("generation.dependency_claim_invalid")
    else:
        raw_runtime_dependency_evidence = manifest.get("runtime_dependency_evidence")
        runtime_evidence_mapping = (
            cast(dict[object, object], raw_runtime_dependency_evidence)
            if isinstance(raw_runtime_dependency_evidence, dict)
            else None
        )
        if (
            runtime_evidence_mapping is not None
            and runtime_evidence_mapping.get("schema")
            == RUNTIME_DEPENDENCY_BUNDLE_SCHEMA
        ):
            runtime_dependency_evidence = _validate_runtime_dependency_bundle(
                cast(object, runtime_evidence_mapping)
            )
        else:
            runtime_dependency_evidence = _validate_runtime_dependency_evidence(
                cast(object, raw_runtime_dependency_evidence)
            )
        runtime_dependency_sha256_value = manifest.get("runtime_dependency_sha256")
        if (
            not isinstance(runtime_dependency_sha256_value, str)
            or not _SHA256_RE.fullmatch(runtime_dependency_sha256_value)
            or runtime_dependency_sha256_value != runtime_dependency_evidence["sha256"]
        ):
            raise GenerationContractError(
                "generation.runtime_dependency_digest_mismatch"
            )
        if runtime_dependency_evidence["schema"] == RUNTIME_DEPENDENCY_BUNDLE_SCHEMA:
            if manifest.get("dependency_environment_state") != RUNTIME_DEPENDENCY_STATE:
                raise GenerationContractError("generation.dependency_claim_invalid")
            runtime_bundle_entries = cast(
                list[dict[str, Any]], runtime_dependency_evidence["files"]
            )
            claim_ceiling = RUNTIME_DEPENDENCY_CLAIM_CEILING
        else:
            if (
                manifest.get("dependency_environment_state")
                != EXTERNAL_RUNTIME_DEPENDENCY_STATE
            ):
                raise GenerationContractError("generation.dependency_claim_invalid")
            claim_ceiling = EXTERNAL_RUNTIME_DEPENDENCY_CLAIM_CEILING
        runtime_dependency_sha256 = runtime_dependency_sha256_value
        runtime_dependency_state = str(runtime_dependency_evidence["state"])
    if (
        manifest.get("python_binding")
        != "external_executable_digest_verified_not_environment_immutable"
    ):
        raise GenerationContractError("generation.python_claim_invalid")
    if manifest.get("database_rollback_contract") != _database_rollback_contract(
        entries
    ):
        raise GenerationContractError("generation.database_rollback_claim_invalid")
    python_executable, python_sha256 = _verify_python_binding(manifest)
    if (
        runtime_dependency_evidence is not None
        and runtime_dependency_evidence["schema"] == RUNTIME_DEPENDENCY_EVIDENCE_SCHEMA
    ):
        if runtime_dependency_evidence["python_executable"] != manifest.get(
            "python_executable_resolved"
        ):
            raise GenerationContractError(
                "generation.runtime_dependency_python_mismatch"
            )
        observed_runtime_dependency_evidence = _runtime_dependency_evidence(
            python_executable
        )
        if observed_runtime_dependency_evidence != runtime_dependency_evidence:
            raise GenerationContractError(
                "generation.runtime_dependency_digest_mismatch"
            )
    _verify_exact_release_tree(release, entries, runtime_bundle_entries)
    for entry in entries:
        relative = Path(str(entry.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise GenerationContractError("generation.manifest_path_invalid")
        candidate = release / relative
        if not candidate.is_file() or candidate.is_symlink():
            raise GenerationContractError("generation.release_file_missing")
        if _sha256_file(candidate) != entry.get("sha256"):
            raise GenerationContractError("generation.release_digest_mismatch")
    for entry in runtime_bundle_entries:
        candidate = release / str(entry["path"])
        if not candidate.is_file() or candidate.is_symlink():
            raise GenerationContractError("generation.runtime_dependency_file_missing")
        if _sha256_file(candidate) != entry["sha256"]:
            raise GenerationContractError(
                "generation.runtime_dependency_digest_mismatch"
            )
    launcher = release / "bin" / "bridge-db-mcp"
    if not launcher.is_file() or launcher.is_symlink():
        raise GenerationContractError("generation.launcher_missing")
    expected_launcher = _make_launcher(
        release_path=release,
        python_executable=python_executable,
        python_resolved=Path(str(manifest["python_executable_resolved"])),
        python_sha256=python_sha256,
        generation_id=generation_id,
        bundled_runtime=bool(runtime_bundle_entries),
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
        "runtime_dependency_sha256": runtime_dependency_sha256,
        "runtime_dependency_state": runtime_dependency_state,
        "runtime_dependency_evidence": runtime_dependency_evidence,
        "contract_sha256": manifest["contract_sha256"],
        "launcher_sha256": manifest["launcher_sha256"],
        "python_executable": str(python_executable),
        "python_sha256": python_sha256,
        "dependency_environment_state": manifest["dependency_environment_state"],
        "database_rollback_contract": manifest["database_rollback_contract"],
        "claim_ceiling": claim_ceiling,
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
    if _RESERVED_RELEASE_PATHS.intersection(
        str(entry["path"]) for entry in entries
    ) or any(
        Path(str(entry["path"])).is_relative_to(_RUNTIME_BUNDLE_ROOT.parent)
        for entry in entries
    ):
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
        external_runtime_dependency_evidence = _runtime_dependency_evidence(executable)
        if external_runtime_dependency_evidence["python_executable"] != str(
            executable_resolved
        ):
            raise GenerationContractError(
                "generation.runtime_dependency_python_mismatch"
            )
        _validate_locked_runtime_dependencies(
            source, external_runtime_dependency_evidence
        )
        runtime_dependency_evidence = _copy_runtime_dependency_bundle(
            temporary=temporary,
            source_evidence=external_runtime_dependency_evidence,
        )
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
            "dependency_environment_state": RUNTIME_DEPENDENCY_STATE,
            "runtime_dependency_sha256": runtime_dependency_evidence["sha256"],
            "runtime_dependency_evidence": runtime_dependency_evidence,
            "database_rollback_contract": _database_rollback_contract(entries),
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
    generation_id = _pointer_target(root, name)
    if generation_id is not None:
        verify_generation(root, generation_id)
    return generation_id


def _pointer_target(root: Path, name: str) -> str | None:
    """Read only the syntactic pointer target without upgrading its integrity."""
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
    if not _GENERATION_RE.fullmatch(generation_id):
        raise GenerationContractError("generation.pointer_target_invalid")
    return generation_id


def _replace_pointer_target(root: Path, name: str, generation_id: str | None) -> None:
    """Restore an already digest-bound legacy pointer without claiming verification."""
    pointer = root / name
    if pointer.exists() and not pointer.is_symlink():
        raise GenerationContractError("generation.pointer_not_symlink")
    if generation_id is None:
        pointer.unlink(missing_ok=True)
        _fsync_directory(root)
        return
    if not _GENERATION_RE.fullmatch(generation_id):
        raise GenerationContractError("generation.pointer_target_invalid")
    temporary = root / f".{name}.pending-{os.getpid()}"
    temporary.unlink(missing_ok=True)
    os.symlink(f"releases/{generation_id}", temporary)
    os.replace(temporary, pointer)
    _fsync_directory(root)


def _legacy_pointer_evidence(
    root: Path, name: str, expected_generation: str, expected_manifest_sha256: str
) -> dict[str, str]:
    """Bind a legacy pointer as preserved evidence, never as executable rollback."""
    if not _GENERATION_RE.fullmatch(expected_generation) or not _SHA256_RE.fullmatch(
        expected_manifest_sha256
    ):
        raise GenerationContractError("generation.bootstrap_expected_identity_invalid")
    if _pointer_target(root, name) != expected_generation:
        raise GenerationContractError("generation.bootstrap_legacy_pointer_mismatch")
    manifest_path = (
        _release_path(root, expected_generation) / "generation-manifest.json"
    )
    try:
        metadata = manifest_path.lstat()
    except OSError as exc:
        raise GenerationContractError(
            "generation.bootstrap_legacy_manifest_invalid"
        ) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or manifest_path.is_symlink()
        or _sha256_file(manifest_path) != expected_manifest_sha256
    ):
        raise GenerationContractError("generation.bootstrap_legacy_manifest_mismatch")
    manifest = _read_manifest(manifest_path)
    if (
        manifest.get("schema") != GENERATION_SCHEMA
        or manifest.get("generation_id") != expected_generation
    ):
        raise GenerationContractError("generation.bootstrap_legacy_manifest_invalid")
    return {
        "pointer": name,
        "generation_id": expected_generation,
        "manifest_sha256": expected_manifest_sha256,
        "verification_state": "identity_preserved_integrity_unverified",
    }


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
    bootstrap_fields = extended_fields | {
        "new_previous",
        "legacy_current_evidence",
        "legacy_previous_evidence",
        "old_activation_state",
    }
    if (
        set(journal) not in (legacy_fields, extended_fields, bootstrap_fields)
        or journal.get("schema") != ACTIVATION_SCHEMA
    ):
        raise GenerationContractError("generation.activation_journal_invalid")
    body = {key: value for key, value in journal.items() if key != "journal_sha256"}
    if journal.get("journal_sha256") != _sha256_bytes(
        _stable_json(body).encode("utf-8")
    ):
        raise GenerationContractError("generation.activation_journal_digest_mismatch")
    if journal.get("operation") not in ("activate", "rollback", "bootstrap_adopt"):
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
    if set(journal) in (extended_fields, bootstrap_fields):
        old_evidence = _normalize_tenancy_evidence_summary(
            journal.get("old_tenancy_activation_evidence")
        )
        new_evidence = _normalize_tenancy_evidence_summary(
            journal.get("new_tenancy_activation_evidence")
        )
    else:
        old_evidence = {"state": "legacy_unverified"}
        new_evidence = {"state": "legacy_unverified"}
    if set(journal) == bootstrap_fields:
        new_previous = journal.get("new_previous")
        if (
            not isinstance(new_previous, str)
            or not _GENERATION_RE.fullmatch(new_previous)
            or new_previous == new_current
            or not isinstance(journal.get("legacy_current_evidence"), dict)
            or not isinstance(journal.get("legacy_previous_evidence"), dict)
            or not isinstance(journal.get("old_activation_state"), dict)
        ):
            raise GenerationContractError("generation.activation_journal_invalid")
    elif journal.get("operation") == "bootstrap_adopt":
        raise GenerationContractError("generation.activation_journal_invalid")
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


def _restore_exact_activation_state(root: Path, state: dict[str, Any]) -> None:
    _atomic_write(
        root / "activation-state.json",
        (json.dumps(state, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        mode=0o400,
    )


def _write_bootstrap_adoption_receipt(
    root: Path,
    *,
    current: str,
    previous: str,
    legacy_current: dict[str, Any],
    legacy_previous: dict[str, Any],
) -> dict[str, Any]:
    readback = _read_activation_without_pending(root)
    receipt = {
        "schema": BOOTSTRAP_ADOPTION_RECEIPT_SCHEMA,
        "operation": "bootstrap_adopt",
        "recorded_at": _utc_text(),
        "requested_generation": current,
        "rollback_generation": previous,
        "legacy_pointer_evidence": [legacy_current, legacy_previous],
        "legacy_generations_preserved": True,
        "legacy_generations_rollback_eligible": False,
        "readback": readback,
        "outcome": (
            "bootstrap_adopted"
            if readback.get("state") == "active"
            and readback.get("current_generation") == current
            and readback.get("previous_generation") == previous
            else "readback_failed"
        ),
    }
    encoded = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode("utf-8")
    path = _receipt_path(root, "bootstrap-adopt", current)
    _atomic_write(path, encoded, mode=0o400)
    return {
        **receipt,
        "receipt_path": str(path),
        "receipt_sha256": _sha256_bytes(encoded),
    }


def _recover_pending_bootstrap_adoption(
    root: Path, journal: dict[str, Any]
) -> dict[str, Any]:
    old_current = cast(str, journal["old_current"])
    old_previous = cast(str, journal["old_previous"])
    new_current = cast(str, journal["new_current"])
    new_previous = cast(str, journal["new_previous"])
    current = _pointer_target(root, "current")
    previous = _pointer_target(root, "previous")
    new_evidence = cast(dict[str, Any], journal["new_tenancy_activation_evidence"])

    if (
        current == new_current
        and previous == new_previous
        and _activation_state_matches(
            root,
            current=new_current,
            previous=new_previous,
            operation="bootstrap_adopt",
            tenancy_evidence=new_evidence,
        )
    ):
        receipt = _write_bootstrap_adoption_receipt(
            root,
            current=new_current,
            previous=new_previous,
            legacy_current=cast(dict[str, Any], journal["legacy_current_evidence"]),
            legacy_previous=cast(dict[str, Any], journal["legacy_previous_evidence"]),
        )
        if receipt["outcome"] != "bootstrap_adopted":
            raise GenerationContractError(
                "generation.bootstrap_recovery_readback_failed"
            )
        removal_verified = _remove_pending_journal(root)
        return {
            **receipt,
            "outcome": (
                "bootstrap_adopted_recovered"
                if removal_verified
                else "bootstrap_adopted_recovered_journal_fsync_unverified"
            ),
            "recovery_disposition": "committed_finalized",
            "journal_removal": "verified" if removal_verified else "fsync_unverified",
        }

    allowed_maps = {
        (old_current, old_previous),
        (new_current, old_previous),
        (new_current, new_previous),
    }
    if (current, previous) not in allowed_maps:
        raise GenerationContractError("generation.activation_journal_map_mismatch")
    _replace_pointer_target(root, "current", old_current)
    _replace_pointer_target(root, "previous", old_previous)
    _restore_exact_activation_state(
        root, cast(dict[str, Any], journal["old_activation_state"])
    )
    if (
        _pointer_target(root, "current") != old_current
        or _pointer_target(root, "previous") != old_previous
    ):
        raise GenerationContractError("generation.bootstrap_recovery_readback_failed")
    removal_verified = _remove_pending_journal(root)
    return {
        "schema": BOOTSTRAP_ADOPTION_RECEIPT_SCHEMA,
        "operation": "bootstrap_adopt",
        "outcome": "before_map_restored",
        "requested_generation": new_current,
        "rollback_generation": new_previous,
        "recovery_disposition": "before_map_restored",
        "journal_removal": "verified" if removal_verified else "fsync_unverified",
    }


def _recover_pending_activation(root: Path) -> dict[str, Any] | None:
    """Recover only exact before/partial/committed maps under the activation lock."""
    journal = _pending_journal(root)
    if journal is None:
        return None
    old_current = cast(str | None, journal["old_current"])
    old_previous = cast(str | None, journal["old_previous"])
    new_current = cast(str, journal["new_current"])
    operation = cast(
        Literal["activate", "rollback", "bootstrap_adopt"], journal["operation"]
    )
    old_tenancy_evidence = cast(
        dict[str, Any], journal["old_tenancy_activation_evidence"]
    )
    new_tenancy_evidence = cast(
        dict[str, Any], journal["new_tenancy_activation_evidence"]
    )
    if operation == "bootstrap_adopt":
        return _recover_pending_bootstrap_adoption(root, journal)
    current = _pointer_generation(root, "current")
    previous = _pointer_generation(root, "previous")

    if (
        current == new_current
        and previous == old_current
        and _activation_state_matches(
            root,
            current=new_current,
            previous=old_current,
            operation=operation,
            tenancy_evidence=new_tenancy_evidence,
        )
    ):
        _mark_draining(root, old_current, superseded_by=new_current)
        receipt = _write_activation_receipt(
            root,
            operation=operation,
            previous=old_current,
            current=new_current,
        )
        if receipt["outcome"] != "activated":
            raise GenerationContractError(
                "generation.activation_recovery_readback_failed"
            )
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
        "journal_sha256": _sha256_bytes(_stable_json(pending_body).encode("utf-8")),
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


def bootstrap_adopt_generation(
    root: Path,
    generation_id: str,
    rollback_generation_id: str,
    *,
    expected_current_generation: str,
    expected_previous_generation: str,
    expected_current_manifest_sha256: str,
    expected_previous_manifest_sha256: str,
    tenancy_evidence_path: Path | None = None,
) -> dict[str, Any]:
    """One-shot migration from digest-bound legacy pointers to verified peers.

    This intentionally does not make either legacy generation rollback eligible.
    Both post-adoption pointers must name distinct generations that pass the full
    current verifier before any mutation is journaled.
    """
    root = _guard_absolute_directory(root)
    with _activation_lock(root):
        recovery = _recover_pending_activation(root)
        if recovery is not None and (
            recovery.get("recovery_disposition") == "committed_finalized"
            or recovery.get("journal_removal") != "verified"
        ):
            return recovery
        if generation_id == rollback_generation_id:
            raise GenerationContractError("generation.bootstrap_rollback_not_distinct")
        verify_generation(root, generation_id)
        verify_generation(root, rollback_generation_id)
        if generation_id in (
            expected_current_generation,
            expected_previous_generation,
        ) or (
            rollback_generation_id
            in (expected_current_generation, expected_previous_generation)
        ):
            raise GenerationContractError("generation.bootstrap_candidate_is_legacy")
        legacy_current = _legacy_pointer_evidence(
            root,
            "current",
            expected_current_generation,
            expected_current_manifest_sha256,
        )
        legacy_previous = _legacy_pointer_evidence(
            root,
            "previous",
            expected_previous_generation,
            expected_previous_manifest_sha256,
        )
        old_state = _read_manifest(root / "activation-state.json")
        if (
            old_state.get("schema") != ACTIVATION_SCHEMA
            or old_state.get("current_generation") != expected_current_generation
            or old_state.get("previous_generation") != expected_previous_generation
        ):
            raise GenerationContractError("generation.bootstrap_legacy_state_mismatch")
        tenancy_evidence = _load_tenancy_activation_evidence(
            root, tenancy_evidence_path, generation_id
        )
        _assert_activation_recovery_ready()
        pending_body = {
            "schema": ACTIVATION_SCHEMA,
            "operation": "bootstrap_adopt",
            "old_current": expected_current_generation,
            "old_previous": expected_previous_generation,
            "new_current": generation_id,
            "new_previous": rollback_generation_id,
            "created_at": _utc_text(),
            "old_tenancy_activation_evidence": _normalize_tenancy_evidence_summary(
                old_state.get("tenancy_activation_evidence")
            ),
            "new_tenancy_activation_evidence": tenancy_evidence,
            "legacy_current_evidence": legacy_current,
            "legacy_previous_evidence": legacy_previous,
            "old_activation_state": old_state,
        }
        pending = {
            **pending_body,
            "journal_sha256": _sha256_bytes(_stable_json(pending_body).encode("utf-8")),
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
            _replace_pointer(root, "previous", rollback_generation_id)
            state = {
                "schema": ACTIVATION_SCHEMA,
                "current_generation": generation_id,
                "previous_generation": rollback_generation_id,
                "activated_at": _utc_text(),
                "operation": "bootstrap_adopt",
                "tenancy_activation_evidence": tenancy_evidence,
            }
            _atomic_write(
                root / "activation-state.json",
                (json.dumps(state, sort_keys=True, indent=2) + "\n").encode("utf-8"),
                mode=0o400,
            )
        except Exception:
            try:
                _replace_pointer_target(root, "current", expected_current_generation)
                _replace_pointer_target(root, "previous", expected_previous_generation)
                _restore_exact_activation_state(root, old_state)
                pending_path.unlink(missing_ok=True)
                _fsync_directory(root)
            except Exception:
                pass
            raise
        try:
            receipt = _write_bootstrap_adoption_receipt(
                root,
                current=generation_id,
                previous=rollback_generation_id,
                legacy_current=legacy_current,
                legacy_previous=legacy_previous,
            )
            if receipt["outcome"] != "bootstrap_adopted":
                raise GenerationContractError("generation.bootstrap_readback_failed")
            removal_verified = _remove_pending_journal(root)
            if not removal_verified:
                return {
                    **receipt,
                    "outcome": "bootstrap_adopted_journal_fsync_unverified",
                    "journal_removal": "fsync_unverified",
                }
        except Exception as exc:
            raise GenerationContractError(
                "generation.bootstrap_committed_post_actions_pending"
            ) from exc
        return receipt


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
        "runtime_dependency_sha256": verified["runtime_dependency_sha256"],
        "runtime_dependency_state": verified["runtime_dependency_state"],
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
        "dependency_sha256": verified["dependency_sha256"],
        "runtime_dependency_sha256": verified["runtime_dependency_sha256"],
        "runtime_dependency_state": verified["runtime_dependency_state"],
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
    bootstrap = subparsers.add_parser("bootstrap-adopt")
    bootstrap.add_argument("--root", type=Path, required=True)
    bootstrap.add_argument("--generation-id", required=True)
    bootstrap.add_argument("--rollback-generation-id", required=True)
    bootstrap.add_argument("--expected-current-generation", required=True)
    bootstrap.add_argument("--expected-previous-generation", required=True)
    bootstrap.add_argument("--expected-current-manifest-sha256", required=True)
    bootstrap.add_argument("--expected-previous-manifest-sha256", required=True)
    bootstrap.add_argument("--tenancy-evidence", type=Path)
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
        elif args.command == "bootstrap-adopt":
            result = bootstrap_adopt_generation(
                args.root,
                args.generation_id,
                args.rollback_generation_id,
                expected_current_generation=args.expected_current_generation,
                expected_previous_generation=args.expected_previous_generation,
                expected_current_manifest_sha256=(
                    args.expected_current_manifest_sha256
                ),
                expected_previous_manifest_sha256=(
                    args.expected_previous_manifest_sha256
                ),
                tenancy_evidence_path=args.tenancy_evidence,
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
