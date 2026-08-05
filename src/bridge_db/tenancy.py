"""BridgeDB MCP tenancy telemetry and guarded lifecycle planning.

Each stdio server writes only its own lease.  Lifecycle application may retire
an exact dead/reused-PID lease or request cooperative closure of an obsolete
idle generation.  It never enumerates arbitrary processes and has no process
termination primitive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import resource
import stat
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from bridge_db import clock, config

LEASE_SCHEMA = "BridgeMcpTenancyLeaseV1"
PLAN_SCHEMA = "BridgeMcpTenancyPlanV1"
POLICY_SCHEMA = "BridgeMcpTenancyPolicyV1"
APPLY_SCHEMA = "BridgeMcpTenancyApplyReceiptV1"
_OWNER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

ProcessState = Literal["same", "missing", "mismatch", "unknown"]
ProcessProbe = Callable[[int, str], ProcessState]
AncestryState = Literal["same", "changed", "unknown", "not_applicable"]
AncestryProbe = Callable[[int], list[int] | None]


class TenancyContractError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def tenancy_root() -> Path:
    override = os.environ.get("BRIDGE_DB_TENANCY_ROOT")
    return Path(override) if override else config.DB_PATH.parent / "tenancy"


def _utc_text(now: datetime | None = None) -> str:
    return (now or clock.now()).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise TenancyContractError("tenancy.timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TenancyContractError("tenancy.timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise TenancyContractError("tenancy.timestamp_invalid")
    return parsed.astimezone(UTC)


def _stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _guard_root(root: Path, *, create: bool) -> Path:
    if not root.is_absolute() or root in (Path("/"), Path.home()):
        raise TenancyContractError("tenancy.root_invalid")
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not root.exists() or not root.is_dir() or root.is_symlink():
        raise TenancyContractError("tenancy.root_missing_or_symlink")
    if root.resolve(strict=True) != root:
        raise TenancyContractError("tenancy.root_symlink_refused")
    metadata = root.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise TenancyContractError("tenancy.root_not_private")
    for name in ("active", "history", "retire"):
        child = root / name
        if create:
            child.mkdir(exist_ok=True, mode=0o700)
        if not child.exists():
            raise TenancyContractError("tenancy.child_missing")
        if child.is_symlink() or not child.is_dir():
            raise TenancyContractError("tenancy.child_invalid")
        child_metadata = child.stat()
        if child_metadata.st_uid != os.getuid():
            raise TenancyContractError("tenancy.child_owner_mismatch")
        if stat.S_IMODE(child_metadata.st_mode) & 0o077:
            raise TenancyContractError("tenancy.child_not_private")
    return root


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(
    path: Path,
    payload: dict[str, Any],
    *,
    mode: int = 0o600,
    replace: bool = True,
) -> None:
    encoded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.pending-"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise TenancyContractError("tenancy.record_invalid")
    if path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise TenancyContractError("tenancy.record_not_private")
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise TenancyContractError("tenancy.record_invalid") from exc
    if not isinstance(raw, dict):
        raise TenancyContractError("tenancy.record_invalid")
    return {str(key): value for key, value in cast(dict[object, Any], raw).items()}


def process_identity(pid: int) -> str | None:
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        if proc_stat.is_file():
            fields = proc_stat.read_text(encoding="utf-8").split()
            return f"proc-start:{fields[21]}" if len(fields) > 21 else None
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    value = result.stdout.strip()
    return f"ps-start:{value}" if result.returncode == 0 and value else None


def _parent_pid(pid: int) -> int | None:
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        if proc_stat.is_file():
            fields = proc_stat.read_text(encoding="utf-8").split()
            return int(fields[3]) if len(fields) > 3 else None
        result = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, ValueError):
        return None
    value = result.stdout.strip()
    try:
        return int(value) if result.returncode == 0 and value else None
    except ValueError:
        return None


def _pid_ancestry(pid: int, *, limit: int = 8) -> list[int]:
    ancestry: list[int] = []
    seen = {pid}
    current = pid
    for _ in range(limit):
        parent = _parent_pid(current)
        if parent is None or parent <= 0 or parent in seen:
            break
        ancestry.append(parent)
        seen.add(parent)
        current = parent
    return ancestry


def _rss_bytes(pid: int | None = None) -> int:
    """Return current RSS when observable, with max-RSS as a safe fallback."""
    selected_pid = pid or os.getpid()
    proc_status = Path(f"/proc/{selected_pid}/status")
    try:
        if proc_status.is_file():
            for line in proc_status.read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(selected_pid)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        value = result.stdout.strip()
        if result.returncode == 0 and value:
            return int(value) * 1024
    except (OSError, ValueError):
        pass
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes; Linux reports KiB.
    return int(usage if sys_platform_is_macos() else usage * 1024)


def _process_observations_by_lease(
    leases: list[tuple[Path, dict[str, Any]]],
) -> dict[str, tuple[ProcessState, int | None]]:
    """Bind leases to live identities and RSS in one bounded process-table pass."""
    expected = {
        int(record["pid"]): (str(record["lease_id"]), str(record["process_identity"]))
        for _, record in leases
    }
    observations: dict[str, tuple[ProcessState, int | None]] = {}
    if not expected:
        return observations
    if sys_platform_is_macos():
        try:
            result = subprocess.run(
                ["ps", "-axo", "pid=,lstart=,rss="],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError:
            return {
                lease_id: ("unknown", None)
                for lease_id, _identity in expected.values()
            }
        if result.returncode != 0:
            return {
                lease_id: ("unknown", None)
                for lease_id, _identity in expected.values()
            }
        pattern = re.compile(r"^\s*(\d+)\s+(.{24})\s+(\d+)\s*$")
        table: dict[int, tuple[str, int]] = {}
        for line in result.stdout.splitlines():
            match = pattern.match(line)
            if match is None:
                continue
            pid = int(match.group(1))
            table[pid] = (f"ps-start:{match.group(2)}", int(match.group(3)) * 1024)
        for pid, (lease_id, identity) in expected.items():
            row = table.get(pid)
            if row is not None:
                observations[lease_id] = (
                    ("same", row[1]) if row[0] == identity else ("mismatch", None)
                )
                continue
            state = probe_process(pid, identity)
            observations[lease_id] = (
                ("same", _rss_bytes(pid)) if state == "same" else (state, None)
            )
        return observations

    for pid, (lease_id, identity) in expected.items():
        state = probe_process(pid, identity)
        observations[lease_id] = (
            ("same", _rss_bytes(pid)) if state == "same" else (state, None)
        )
    return observations


def sys_platform_is_macos() -> bool:
    return os.uname().sysname == "Darwin"


def probe_process(pid: int, expected_identity: str) -> ProcessState:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "missing"
    except PermissionError:
        return "unknown"
    except OSError:
        return "unknown"
    observed = process_identity(pid)
    if observed is None:
        return "unknown"
    return "same" if observed == expected_identity else "mismatch"


def owner_for_principal(principal: str | None) -> str:
    explicit = os.environ.get("BRIDGE_DB_CLIENT_OWNER")
    if explicit:
        owner = explicit.strip().lower()
    else:
        principal_owners: dict[str, str] = {
            "codex": "codex",
            "cc": "claude",
            "claude_ai": "claude",
            "personal_ops": "personal_ops",
            "notion_os": "notion_os",
        }
        owner = principal_owners.get(principal, "unknown") if principal else "unknown"
    if not _OWNER_RE.fullmatch(owner):
        raise TenancyContractError("tenancy.owner_invalid")
    return owner


def _validate_lease_record(path: Path, record: dict[str, Any]) -> None:
    lease_id = record.get("lease_id")
    if (
        record.get("schema") != LEASE_SCHEMA
        or not isinstance(lease_id, str)
        or not re.fullmatch(r"[0-9a-f]{24}", lease_id)
        or lease_id != path.stem
    ):
        raise TenancyContractError("tenancy.lease_identity_mismatch")
    owner = record.get("owner")
    if not isinstance(owner, str) or not _OWNER_RE.fullmatch(owner):
        raise TenancyContractError("tenancy.lease_owner_invalid")
    for field_name in ("principal", "generation"):
        value = record.get(field_name)
        if value is not None and (not isinstance(value, str) or len(value) > 256):
            raise TenancyContractError("tenancy.lease_identity_field_invalid")
    for field_name in ("pid", "parent_pid"):
        value = record.get(field_name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise TenancyContractError("tenancy.lease_process_identity_invalid")
    identity = record.get("process_identity")
    if not isinstance(identity, str) or not identity or len(identity) > 1024:
        raise TenancyContractError("tenancy.lease_process_identity_invalid")
    ancestry = record.get("pid_ancestry")
    if not isinstance(ancestry, list):
        raise TenancyContractError("tenancy.lease_ancestry_invalid")
    if any(
        not isinstance(item, int) or isinstance(item, bool) or item < 1
        for item in cast(list[object], ancestry)
    ):
        raise TenancyContractError("tenancy.lease_ancestry_invalid")
    for field_name in ("active_request_count", "request_count", "rss_bytes"):
        value = record.get(field_name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise TenancyContractError("tenancy.lease_counter_invalid")
    for field_name in ("retirement_requested", "retirement_ready"):
        if not isinstance(record.get(field_name), bool):
            raise TenancyContractError("tenancy.lease_retirement_state_invalid")
    _parse_utc(record.get("created_at"))
    _parse_utc(record.get("last_heartbeat_at"))
    last_request = record.get("last_request_at")
    if last_request is not None:
        _parse_utc(last_request)


@dataclass
class TenancyTracker:
    root: Path
    owner: str
    principal: str | None
    generation: str | None
    execution_root: Path | None = None
    pid: int = 0
    process_identity: str | None = None
    lease_id: str = field(init=False)
    path: Path = field(init=False)
    _lock: threading.RLock = field(init=False, repr=False)
    _record: dict[str, Any] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = _guard_root(self.root, create=True)
        if not _OWNER_RE.fullmatch(self.owner):
            raise TenancyContractError("tenancy.owner_invalid")
        self.pid = self.pid or os.getpid()
        self.process_identity = self.process_identity or process_identity(self.pid)
        if self.process_identity is None:
            raise TenancyContractError("tenancy.process_identity_unknown")
        identity_seed = f"{self.owner}\0{self.pid}\0{self.process_identity}\0{self.generation or 'mutable'}"
        self.lease_id = _sha256_bytes(identity_seed.encode("utf-8"))[:24]
        self.path = self.root / "active" / f"{self.lease_id}.json"
        self._lock = threading.RLock()
        self._record: dict[str, Any] = {}

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.path.exists():
                raise TenancyContractError("tenancy.lease_collision")
            now = _utc_text()
            ancestry = _pid_ancestry(self.pid)
            self._record = {
                "schema": LEASE_SCHEMA,
                "lease_id": self.lease_id,
                "owner": self.owner,
                "principal": self.principal,
                "generation": self.generation,
                "pid": self.pid,
                "parent_pid": ancestry[0] if ancestry else os.getppid(),
                "pid_ancestry": ancestry,
                "process_identity": self.process_identity,
                "created_at": now,
                "last_request_at": None,
                "last_heartbeat_at": now,
                "active_request_count": 0,
                "request_count": 0,
                "last_tool": None,
                "last_request_outcome": None,
                "lifecycle_reason": "idle",
                "rss_bytes": _rss_bytes(),
                "retirement_requested": self._retirement_requested(),
                "retirement_ready": False,
            }
            _atomic_write(self.path, self._record, replace=False)
            return dict(self._record)

    def _retirement_requested(self) -> bool:
        exact = self.root / "retire" / f"{self.lease_id}.json"
        if exact.is_file() and not exact.is_symlink():
            return True
        if self.execution_root is not None and self.generation is not None:
            marker = self.execution_root / "drain" / f"{self.generation}.json"
            return marker.is_file() and not marker.is_symlink()
        return False

    def _persist(self) -> None:
        self._record["last_heartbeat_at"] = _utc_text()
        self._record["rss_bytes"] = _rss_bytes(self.pid)
        self._record["retirement_requested"] = self._retirement_requested()
        self._record["retirement_ready"] = bool(
            self._record["retirement_requested"]
            and self._record["active_request_count"] == 0
        )
        _atomic_write(self.path, self._record)

    def request_started(self, tool: str) -> None:
        with self._lock:
            if not self._record:
                raise TenancyContractError("tenancy.tracker_not_started")
            if self._retirement_requested():
                self._record["retirement_requested"] = True
                self._record["retirement_ready"] = True
                self._record["lifecycle_reason"] = "retirement_refused_new_request"
                self._persist()
                raise TenancyContractError("tenancy.retirement_new_request_refused")
            self._record["active_request_count"] += 1
            self._record["request_count"] += 1
            self._record["last_request_at"] = _utc_text()
            self._record["last_tool"] = tool
            self._record["last_request_outcome"] = None
            self._record["lifecycle_reason"] = "request_active"
            self._persist()

    def request_finished(
        self, tool: str, *, outcome: Literal["succeeded", "failed"]
    ) -> None:
        with self._lock:
            if self._record.get("active_request_count", 0) < 1:
                raise TenancyContractError("tenancy.active_request_underflow")
            self._record["active_request_count"] -= 1
            self._record["last_request_at"] = _utc_text()
            self._record["last_tool"] = tool
            self._record["last_request_outcome"] = outcome
            self._record["lifecycle_reason"] = (
                "request_completed" if outcome == "succeeded" else "request_failed"
            )
            self._persist()
            if self._record["retirement_ready"]:
                self._record["lifecycle_reason"] = "obsolete_generation_idle"
                self._persist()

    def retirement_ready(self) -> bool:
        """Observe an exact drain marker and report safe cooperative shutdown."""
        with self._lock:
            if not self._record:
                raise TenancyContractError("tenancy.tracker_not_started")
            requested = self._retirement_requested()
            if not requested:
                return False
            ready = self._record["active_request_count"] == 0
            changed = (
                self._record.get("retirement_requested") is not True
                or self._record.get("retirement_ready") is not ready
                or (
                    ready
                    and self._record.get("lifecycle_reason")
                    != "obsolete_generation_idle"
                )
            )
            self._record["retirement_requested"] = True
            self._record["retirement_ready"] = ready
            if ready:
                self._record["lifecycle_reason"] = "obsolete_generation_idle"
            if changed:
                self._persist()
            return ready

    def close(self, *, reason: str = "normal_close") -> dict[str, Any]:
        with self._lock:
            if not self._record:
                raise TenancyContractError("tenancy.tracker_not_started")
            if self._record["active_request_count"] != 0:
                raise TenancyContractError("tenancy.close_active_request_refused")
            retirement_requested = self._retirement_requested()
            closed_at = _utc_text()
            closed = {
                **self._record,
                "lifecycle_reason": reason,
                "closed_at": closed_at,
                "active_request_count": 0,
                "retirement_requested": retirement_requested,
                "retirement_ready": retirement_requested,
            }
            event_id = _sha256_bytes(
                f"{self.lease_id}\0closed\0{closed_at}".encode("utf-8")
            )[:12]
            history = self.root / "history" / f"{self.lease_id}-closed-{event_id}.json"
            _atomic_write(history, closed, mode=0o400, replace=False)
            readback = _read_json(history)
            if readback.get("closed_at") != closed["closed_at"]:
                raise TenancyContractError("tenancy.close_readback_failed")
            self.path.unlink()
            _fsync_directory(self.path.parent)
            retirement_marker = self.root / "retire" / f"{self.lease_id}.json"
            if retirement_marker.exists():
                if retirement_marker.is_symlink() or not retirement_marker.is_file():
                    raise TenancyContractError("tenancy.close_request_invalid")
                retirement_marker.unlink()
                _fsync_directory(retirement_marker.parent)
            self._record = closed
            return {"ok": True, "lease_id": self.lease_id, "history_path": str(history)}


def read_active_leases(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    root = _guard_root(root, create=False)
    records: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted((root / "active").glob("*.json")):
        record = _read_json(path)
        _validate_lease_record(path, record)
        records.append((path, record))
    return records


def derive_lifecycle_policy(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive deterministic per-owner budgets from representative replay rows."""
    if not observations:
        raise TenancyContractError("tenancy.replay_observations_missing")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in observations:
        owner = row.get("owner")
        if not isinstance(owner, str) or not _OWNER_RE.fullmatch(owner):
            raise TenancyContractError("tenancy.replay_owner_invalid")
        for metric_name in ("process_count", "lifetime_seconds", "rss_bytes"):
            value = row.get(metric_name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value < 0
            ):
                raise TenancyContractError("tenancy.replay_value_invalid")
        grouped.setdefault(owner, []).append(row)
    budgets: dict[str, Any] = {}
    for owner, rows in sorted(grouped.items()):
        process_highwater = max(int(row["process_count"]) for row in rows)
        lifetime_highwater = max(float(row["lifetime_seconds"]) for row in rows)
        rss_highwater = max(int(row["rss_bytes"]) for row in rows)
        budgets[owner] = {
            "max_processes": max(process_highwater + 1, 1),
            "max_lifetime_seconds": max(math.ceil(lifetime_highwater * 1.25), 60),
            "max_rss_bytes": max(math.ceil(rss_highwater * 1.25), 16 * 1024 * 1024),
            "idle_review_seconds": max(math.ceil(lifetime_highwater * 2), 300),
            "derived_from": {
                "observations": len(rows),
                "process_highwater": process_highwater,
                "lifetime_highwater_seconds": lifetime_highwater,
                "rss_highwater_bytes": rss_highwater,
            },
        }
    policy = {"schema": POLICY_SCHEMA, "budgets": budgets}
    return {**policy, "policy_sha256": _sha256_bytes(_stable_json(policy).encode())}


def _validate_policy(policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if policy.get("schema") != POLICY_SCHEMA or not isinstance(
        policy.get("budgets"), dict
    ):
        raise TenancyContractError("tenancy.policy_invalid")
    supplied_digest = policy.get("policy_sha256")
    policy_body = {
        key: value for key, value in policy.items() if key != "policy_sha256"
    }
    if supplied_digest != _sha256_bytes(_stable_json(policy_body).encode("utf-8")):
        raise TenancyContractError("tenancy.policy_digest_mismatch")
    raw_budgets = cast(dict[object, object], policy["budgets"])
    if any(
        not isinstance(owner, str) or not isinstance(value, dict)
        for owner, value in raw_budgets.items()
    ):
        raise TenancyContractError("tenancy.policy_invalid")
    budgets = cast(dict[str, dict[str, Any]], raw_budgets)
    required = {
        "max_processes",
        "max_lifetime_seconds",
        "max_rss_bytes",
        "idle_review_seconds",
    }
    for owner, budget in budgets.items():
        if not _OWNER_RE.fullmatch(owner) or not required.issubset(budget):
            raise TenancyContractError("tenancy.policy_invalid")
        if any(
            not isinstance(budget[name], (int, float))
            or isinstance(budget[name], bool)
            or budget[name] < 1
            for name in required
        ):
            raise TenancyContractError("tenancy.policy_invalid")
    return budgets


def plan_lifecycle(
    *,
    root: Path,
    policy: dict[str, Any],
    current_generation: str | None,
    now: datetime | None = None,
    process_probe: ProcessProbe = probe_process,
    ancestry_probe: AncestryProbe = _pid_ancestry,
) -> dict[str, Any]:
    budgets = _validate_policy(policy)
    fixed_now = (now or clock.now()).astimezone(UTC)
    leases = read_active_leases(root)
    owner_counts: dict[str, int] = {}
    for _, record in leases:
        owner = str(record.get("owner"))
        owner_counts[owner] = owner_counts.get(owner, 0) + 1

    decisions: list[dict[str, Any]] = []
    for path, record in leases:
        owner = str(record.get("owner"))
        budget = budgets.get(owner)
        if budget is None:
            decision = "keep_policy_missing"
            process_state: ProcessState = "unknown"
            ancestry_state: AncestryState = "not_applicable"
        else:
            pid = record.get("pid")
            identity = record.get("process_identity")
            if (
                not isinstance(pid, int)
                or isinstance(pid, bool)
                or not isinstance(identity, str)
            ):
                raise TenancyContractError("tenancy.lease_process_identity_invalid")
            process_state = process_probe(pid, identity)
            recorded_ancestry = cast(list[int], record["pid_ancestry"])
            observed_ancestry = ancestry_probe(pid) if process_state == "same" else None
            if process_state != "same":
                ancestry_state = "not_applicable"
            elif observed_ancestry is None:
                ancestry_state = "unknown"
            elif observed_ancestry == recorded_ancestry:
                ancestry_state = "same"
            else:
                ancestry_state = "changed"
            active = record.get("active_request_count")
            if not isinstance(active, int) or isinstance(active, bool) or active < 0:
                raise TenancyContractError("tenancy.active_request_count_invalid")
            created = _parse_utc(record.get("created_at"))
            last = _parse_utc(record.get("last_heartbeat_at"))
            lifetime = max((fixed_now - created).total_seconds(), 0)
            idle = max((fixed_now - last).total_seconds(), 0)
            obsolete = bool(
                current_generation
                and record.get("generation")
                and record.get("generation") != current_generation
            )
            marker = root / "retire" / f"{record['lease_id']}.json"
            marker_requested = False
            if marker.exists():
                marker_record = _read_json(marker)
                if marker_record.get("lease_id") != record["lease_id"]:
                    raise TenancyContractError(
                        "tenancy.close_request_identity_mismatch"
                    )
                marker_requested = True
            requested = (
                bool(record.get("retirement_requested")) or marker_requested or obsolete
            )
            if process_state == "missing":
                decision = "retire_orphan"
            elif process_state == "mismatch":
                decision = "retire_pid_reused"
            elif process_state == "unknown":
                decision = "keep_process_unknown"
            elif active > 0:
                decision = "keep_active_request"
            elif ancestry_state == "unknown":
                decision = "keep_ancestry_unknown"
            elif ancestry_state == "changed":
                decision = "keep_ancestry_changed"
            elif requested:
                decision = "request_close_obsolete"
            elif owner_counts[owner] > int(budget["max_processes"]):
                decision = "owner_review_excess_processes"
            elif lifetime > float(budget["max_lifetime_seconds"]):
                decision = "owner_review_lifetime"
            elif int(record.get("rss_bytes", 0)) > int(budget["max_rss_bytes"]):
                decision = "owner_review_rss"
            elif idle > float(budget["idle_review_seconds"]):
                decision = "owner_review_idle"
            else:
                decision = "keep_within_budget"
        decisions.append(
            {
                "lease_id": record["lease_id"],
                "owner": owner,
                "generation": record.get("generation"),
                "pid": record.get("pid"),
                "active_request_count": record.get("active_request_count"),
                "process_state": process_state,
                "ancestry_state": ancestry_state,
                "decision": decision,
                "lease_sha256": _sha256_file(path),
            }
        )
    plan_body = {
        "schema": PLAN_SCHEMA,
        "created_at": _utc_text(fixed_now),
        "root": str(root),
        "current_generation": current_generation,
        "policy_sha256": policy["policy_sha256"],
        "decisions": decisions,
        "effects": "dead_lease_retirement_or_cooperative_close_request_only",
        "process_termination": False,
    }
    return {
        **plan_body,
        "plan_sha256": _sha256_bytes(_stable_json(plan_body).encode("utf-8")),
    }


def apply_lifecycle_plan(
    *,
    root: Path,
    plan: dict[str, Any],
    process_probe: ProcessProbe = probe_process,
    ancestry_probe: AncestryProbe = _pid_ancestry,
    target_lease_id: str | None = None,
) -> dict[str, Any]:
    root = _guard_root(root, create=False)
    supplied_digest = plan.get("plan_sha256")
    body = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if plan.get("schema") != PLAN_SCHEMA or supplied_digest != _sha256_bytes(
        _stable_json(body).encode("utf-8")
    ):
        raise TenancyContractError("tenancy.plan_digest_mismatch")
    if plan.get("root") != str(root) or not isinstance(plan.get("decisions"), list):
        raise TenancyContractError("tenancy.plan_binding_mismatch")

    effects: list[dict[str, Any]] = []
    raw_decisions = cast(list[object], plan["decisions"])
    if any(not isinstance(decision, dict) for decision in raw_decisions):
        raise TenancyContractError("tenancy.plan_decision_invalid")
    actionable = [
        decision
        for decision in cast(list[dict[str, Any]], raw_decisions)
        if decision.get("decision")
        in ("retire_orphan", "retire_pid_reused", "request_close_obsolete")
    ]
    if target_lease_id is not None:
        if not re.fullmatch(r"[0-9a-f]{24}", target_lease_id):
            raise TenancyContractError("tenancy.target_lease_id_invalid")
        actionable = [
            decision
            for decision in actionable
            if decision.get("lease_id") == target_lease_id
        ]
        if len(actionable) != 1:
            raise TenancyContractError("tenancy.target_lease_not_actionable")
    elif len(actionable) > 1:
        raise TenancyContractError("tenancy.multiple_effects_require_exact_target")

    for decision in actionable:
        action = decision.get("decision")
        lease_id = str(decision.get("lease_id"))
        if not re.fullmatch(r"[0-9a-f]{24}", lease_id):
            raise TenancyContractError("tenancy.plan_lease_id_invalid")
        path = root / "active" / f"{lease_id}.json"
        history: Path | None = None
        if action in ("retire_orphan", "retire_pid_reused"):
            event_id = _sha256_bytes(
                f"{lease_id}\0{action}\0{supplied_digest}".encode("utf-8")
            )[:12]
            history = root / "history" / f"{lease_id}-{action}-{event_id}.json"
            if not path.exists():
                completed = _read_json(history)
                if (
                    completed.get("plan_sha256") != supplied_digest
                    or completed.get("source_lease_sha256")
                    != decision.get("lease_sha256")
                    or completed.get("lifecycle_reason") != action
                ):
                    raise TenancyContractError("tenancy.retire_receipt_mismatch")
                effects.append(
                    {
                        "lease_id": lease_id,
                        "effect": action,
                        "readback": "already_complete_history_verified",
                    }
                )
                continue
        record = _read_json(path)
        if _sha256_file(path) != decision.get("lease_sha256"):
            raise TenancyContractError("tenancy.plan_lease_changed")
        if (
            action == "request_close_obsolete"
            and record.get("active_request_count") != 0
        ):
            raise TenancyContractError("tenancy.apply_active_request_refused")
        pid = record.get("pid")
        identity = record.get("process_identity")
        if not isinstance(pid, int) or not isinstance(identity, str):
            raise TenancyContractError("tenancy.lease_process_identity_invalid")
        state = process_probe(pid, identity)
        if action in ("retire_orphan", "retire_pid_reused"):
            expected = "missing" if action == "retire_orphan" else "mismatch"
            if state != expected:
                raise TenancyContractError("tenancy.process_state_changed")
            retired_at = _utc_text()
            retired = {
                **record,
                "lifecycle_reason": action,
                "retired_at": retired_at,
                "retired_process_state": state,
                "plan_sha256": supplied_digest,
                "source_lease_sha256": decision.get("lease_sha256"),
            }
            if history is None:
                raise TenancyContractError("tenancy.retire_receipt_path_missing")
            if history.exists():
                previous = _read_json(history)
                if (
                    previous.get("plan_sha256") != supplied_digest
                    or previous.get("source_lease_sha256")
                    != decision.get("lease_sha256")
                    or previous.get("lifecycle_reason") != action
                ):
                    raise TenancyContractError("tenancy.retire_receipt_mismatch")
            else:
                _atomic_write(history, retired, mode=0o400, replace=False)
            if _read_json(history).get("retired_process_state") != state:
                raise TenancyContractError("tenancy.retire_readback_failed")
            path.unlink()
            _fsync_directory(path.parent)
            effects.append(
                {
                    "lease_id": lease_id,
                    "effect": action,
                    "readback": "active_absent_history_verified",
                }
            )
        else:
            if state != "same":
                raise TenancyContractError("tenancy.process_state_changed")
            recorded_ancestry = cast(list[int], record["pid_ancestry"])
            observed_ancestry = ancestry_probe(pid)
            if (
                decision.get("ancestry_state") != "same"
                or observed_ancestry is None
                or observed_ancestry != recorded_ancestry
            ):
                raise TenancyContractError("tenancy.ancestry_changed")
            request = {
                "schema": "BridgeMcpCooperativeCloseRequestV1",
                "lease_id": lease_id,
                "generation": record.get("generation"),
                "requested_at": _utc_text(),
                "active_request_guard": 0,
                "process_termination": False,
            }
            marker = root / "retire" / f"{lease_id}.json"
            if marker.exists():
                existing = _read_json(marker)
                if (
                    existing.get("lease_id") != lease_id
                    or existing.get("generation") != record.get("generation")
                    or existing.get("active_request_guard") != 0
                ):
                    raise TenancyContractError(
                        "tenancy.close_request_identity_mismatch"
                    )
                marker_state = "cooperative_close_request_already_verified"
            else:
                _atomic_write(marker, request, mode=0o400, replace=False)
                marker_state = "cooperative_close_request_verified"
            if _read_json(marker).get("lease_id") != lease_id:
                raise TenancyContractError("tenancy.close_request_readback_failed")
            effects.append(
                {
                    "lease_id": lease_id,
                    "effect": action,
                    "readback": marker_state,
                }
            )
    return {
        "schema": APPLY_SCHEMA,
        "ok": True,
        "plan_sha256": supplied_digest,
        "effects": effects,
        "target_lease_id": target_lease_id,
        "effect_cardinality": len(effects),
        "process_termination": False,
    }


def tenancy_inventory(root: Path | None = None) -> dict[str, Any]:
    selected = root or tenancy_root()
    if not selected.exists():
        return {
            "schema": "BridgeMcpTenancyInventoryV2",
            "state": "missing",
            "root": str(selected),
            "active_count": 0,
            "lease_count": 0,
            "owners": {},
            "generations": {},
            "active_request_count": 0,
        }
    try:
        leases = read_active_leases(selected)
    except TenancyContractError as exc:
        return {
            "schema": "BridgeMcpTenancyInventoryV2",
            "state": "unverified",
            "root": str(selected),
            "active_count": None,
            "reason_code": exc.reason_code,
        }
    owners: dict[str, int] = {}
    generations: dict[str, int] = {}
    lease_owners: dict[str, int] = {}
    lease_generations: dict[str, int] = {}
    active_requests = 0
    lease_active_requests = 0
    oldest: datetime | None = None
    oldest_lease: datetime | None = None
    observations = _process_observations_by_lease(leases)
    process_states: dict[str, int] = {
        "same": 0,
        "missing": 0,
        "mismatch": 0,
        "unknown": 0,
    }
    rss_total = 0
    lease_rss_total = 0
    for _, record in leases:
        owner = str(record.get("owner"))
        generation = str(record.get("generation") or "mutable")
        lease_owners[owner] = lease_owners.get(owner, 0) + 1
        lease_generations[generation] = lease_generations.get(generation, 0) + 1
        lease_active_requests += int(record.get("active_request_count", 0))
        lease_id = str(record["lease_id"])
        process_state, current_rss = observations[lease_id]
        process_states[process_state] += 1
        if process_state == "same":
            owners[owner] = owners.get(owner, 0) + 1
            generations[generation] = generations.get(generation, 0) + 1
            active_requests += int(record.get("active_request_count", 0))
            if current_rss is not None:
                rss_total += current_rss
        lease_rss_total += int(record.get("rss_bytes", 0))
        created = _parse_utc(record.get("created_at"))
        oldest_lease = (
            created if oldest_lease is None or created < oldest_lease else oldest_lease
        )
        if process_state == "same":
            oldest = created if oldest is None or created < oldest else oldest
    age = (
        max((clock.now().astimezone(UTC) - oldest).total_seconds(), 0)
        if oldest is not None
        else None
    )
    lease_age = (
        max((clock.now().astimezone(UTC) - oldest_lease).total_seconds(), 0)
        if oldest_lease is not None
        else None
    )
    live_count = process_states["same"]
    unknown_count = process_states["unknown"]
    return {
        "schema": "BridgeMcpTenancyInventoryV2",
        "state": "observed",
        "root": str(selected),
        "active_count": live_count,
        "lease_count": len(leases),
        "stale_lease_count": process_states["missing"]
        + process_states["mismatch"],
        "unknown_process_count": unknown_count,
        "process_states": process_states,
        "owners": owners,
        "generations": generations,
        "lease_owners": lease_owners,
        "lease_generations": lease_generations,
        "active_request_count": active_requests,
        "lease_active_request_count": lease_active_requests,
        "rss_total_bytes": rss_total,
        "rss_measurement_state": "observed" if unknown_count == 0 else "partial",
        "rss_observed_process_count": live_count,
        "rss_unverified_process_count": unknown_count,
        "lease_last_observed_rss_total_bytes": lease_rss_total,
        "oldest_age_seconds": age,
        "oldest_lease_age_seconds": lease_age,
    }


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise TenancyContractError("tenancy.input_json_invalid") from exc
    if not isinstance(raw, dict):
        raise TenancyContractError("tenancy.input_json_invalid")
    return {str(key): value for key, value in cast(dict[object, Any], raw).items()}


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bridge_db.tenancy")
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status")
    status.add_argument("--root", type=Path, required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--root", type=Path, required=True)
    plan.add_argument("--policy", type=Path, required=True)
    plan.add_argument("--current-generation")
    apply = subparsers.add_parser("apply")
    apply.add_argument("--root", type=Path, required=True)
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--lease-id")
    args = parser.parse_args()
    try:
        if args.command == "status":
            result = tenancy_inventory(args.root)
        elif args.command == "plan":
            result = plan_lifecycle(
                root=args.root,
                policy=_load_json_file(args.policy),
                current_generation=args.current_generation,
            )
        else:
            result = apply_lifecycle_plan(
                root=args.root,
                plan=_load_json_file(args.plan),
                target_lease_id=args.lease_id,
            )
    except TenancyContractError as exc:
        print(json.dumps({"ok": False, "reason_code": exc.reason_code}, sort_keys=True))
        raise SystemExit(1) from exc
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
