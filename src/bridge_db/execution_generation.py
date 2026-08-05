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
_GENERATION_RE = re.compile(r"^[0-9a-f]{12}-[0-9a-f]{12}$")

DEFAULT_DEPENDENCY_PATHS = ("pyproject.toml", "uv.lock")
DEFAULT_CONTRACT_PATHS = (
    ".codex/verify.commands",
    "integration-spec.md",
    "src/bridge_db/auth.py",
    "src/bridge_db/execution_generation.py",
    "src/bridge_db/secure_binding.py",
    "src/bridge_db/server.py",
    "src/bridge_db/tools/__init__.py",
)


class GenerationContractError(RuntimeError):
    """Fail-closed generation contract error with a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


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
    *, release_path: Path, python_executable: Path, generation_id: str
) -> bytes:
    if any(character in str(python_executable) for character in ("\n", "\r", " ")):
        raise GenerationContractError("generation.python_path_not_shebang_safe")
    body = f"""#!{python_executable}
import os
import runpy
import sys

release = {str(release_path)!r}
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


def verify_generation(root: Path, generation_id: str) -> dict[str, Any]:
    root = _guard_absolute_directory(root)
    release = _release_path(root, generation_id)
    if not release.exists() or not release.is_dir() or release.is_symlink():
        raise GenerationContractError("generation.release_missing")
    if release.resolve(strict=True).parent != (root / "releases").resolve(strict=True):
        raise GenerationContractError("generation.release_escape_refused")
    manifest = _read_manifest(release / "generation-manifest.json")
    if (
        manifest.get("schema") != GENERATION_SCHEMA
        or manifest.get("generation_id") != generation_id
    ):
        raise GenerationContractError("generation.manifest_identity_mismatch")
    source_files = manifest.get("source_files")
    if not isinstance(source_files, list):
        raise GenerationContractError("generation.manifest_files_invalid")
    entries = cast(list[dict[str, Any]], source_files)
    for entry in entries:
        relative = Path(str(entry.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise GenerationContractError("generation.manifest_path_invalid")
        candidate = release / relative
        if not candidate.is_file() or candidate.is_symlink():
            raise GenerationContractError("generation.release_file_missing")
        if _sha256_file(candidate) != entry.get("sha256"):
            raise GenerationContractError("generation.release_digest_mismatch")
    if _entries_digest(entries) != manifest.get("source_tree_sha256"):
        raise GenerationContractError("generation.source_tree_digest_mismatch")
    launcher = release / "bin" / "bridge-db-mcp"
    if not launcher.is_file() or launcher.is_symlink():
        raise GenerationContractError("generation.launcher_missing")
    if _sha256_file(launcher) != manifest.get("launcher_sha256"):
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
    lexical_root = root.absolute()
    if lexical_root == source or lexical_root.is_relative_to(source) or source.is_relative_to(
        lexical_root
    ):
        raise GenerationContractError("generation.source_root_overlap")
    root = _guard_absolute_directory(root, create=True)
    releases = _guard_absolute_directory(root / "releases", create=True)
    _guard_absolute_directory(root / "receipts", create=True)
    _guard_absolute_directory(root / "drain", create=True)

    executable = (python_executable or Path(sys.executable)).resolve(strict=True)
    if not executable.is_file():
        raise GenerationContractError("generation.python_executable_invalid")
    tracked = _tracked_paths(source)
    entries = _file_entries(source, tracked)
    source_digest = _entries_digest(entries)
    dependency_entries = _selected_entries(
        entries, dependency_paths, kind="dependency"
    )
    contract_entries = _selected_entries(entries, contract_paths, kind="contract")
    dependency_digest = _entries_digest(dependency_entries)
    contract_digest = _entries_digest(contract_entries)
    generation_id = f"{reviewed_sha[:12]}-{source_digest[:12]}"
    release = _release_path(root, generation_id)

    if release.exists():
        verified = verify_generation(root, generation_id)
        return {**verified, "disposition": "preserved_existing"}

    temporary = Path(tempfile.mkdtemp(dir=releases, prefix=f".{generation_id}.staging-"))
    try:
        _copy_tracked_source(source, temporary, entries)
        launcher_bytes = _make_launcher(
            release_path=release,
            python_executable=executable,
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
            "python_sha256": _sha256_file(executable),
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
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
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
    readback = read_activation(root)
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
    _atomic_write(
        root / "drain" / f"{generation_id}.json",
        (json.dumps(marker, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        mode=0o400,
    )


def _activate_locked(
    root: Path,
    *,
    generation_id: str,
    operation: Literal["activate", "rollback"],
) -> dict[str, Any]:
    verify_generation(root, generation_id)
    old_current = _pointer_generation(root, "current")
    old_previous = _pointer_generation(root, "previous")
    if old_current == generation_id:
        return {
            "schema": ACTIVATION_RECEIPT_SCHEMA,
            "operation": operation,
            "outcome": "preserved_existing",
            "requested_generation": generation_id,
            "previous_generation": old_previous,
            "readback": read_activation(root),
        }

    pending = {
        "schema": ACTIVATION_SCHEMA,
        "operation": operation,
        "old_current": old_current,
        "old_previous": old_previous,
        "new_current": generation_id,
        "created_at": _utc_text(),
    }
    pending_path = root / ".activation.pending.json"
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
        }
        _atomic_write(
            root / "activation-state.json",
            (json.dumps(state, sort_keys=True, indent=2) + "\n").encode("utf-8"),
            mode=0o400,
        )
        pending_path.unlink()
        _fsync_directory(root)
    except Exception:
        # Restore both exact pointers.  If restoration itself fails the pending
        # journal remains and readback stays explicitly interrupted.
        try:
            _replace_pointer(root, "current", old_current)
            _replace_pointer(root, "previous", old_previous)
            pending_path.unlink(missing_ok=True)
            _fsync_directory(root)
        except Exception:
            pass
        raise

    _mark_draining(root, old_current, superseded_by=generation_id)
    receipt = _write_activation_receipt(
        root,
        operation=operation,
        previous=old_current,
        current=generation_id,
    )
    if receipt["outcome"] != "activated":
        raise GenerationContractError("generation.activation_readback_failed")
    return receipt


def activate_generation(root: Path, generation_id: str) -> dict[str, Any]:
    root = _guard_absolute_directory(root)
    with _activation_lock(root):
        return _activate_locked(root, generation_id=generation_id, operation="activate")


def rollback_generation(root: Path) -> dict[str, Any]:
    root = _guard_absolute_directory(root)
    with _activation_lock(root):
        previous = _pointer_generation(root, "previous")
        if previous is None:
            raise GenerationContractError("generation.rollback_unavailable")
        return _activate_locked(root, generation_id=previous, operation="rollback")


def read_activation(root: Path) -> dict[str, Any]:
    root = _guard_absolute_directory(root)
    pending_path = root / ".activation.pending.json"
    if pending_path.exists():
        return {
            "schema": ACTIVATION_SCHEMA,
            "state": "interrupted",
            "current_generation": None,
            "previous_generation": None,
            "reason_code": "generation.activation_pending",
        }
    current = _pointer_generation(root, "current")
    previous = _pointer_generation(root, "previous")
    if current is None:
        return {
            "schema": ACTIVATION_SCHEMA,
            "state": "inactive",
            "current_generation": None,
            "previous_generation": previous,
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
    return {
        "schema": ACTIVATION_SCHEMA,
        "state": "active",
        "current_generation": current,
        "previous_generation": previous,
        "reviewed_source_sha": verified["reviewed_source_sha"],
        "dependency_sha256": verified["dependency_sha256"],
        "contract_sha256": verified["contract_sha256"],
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
        verify_generation(release.parent.parent, generation_id)
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
            result = activate_generation(args.root, args.generation_id)
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
