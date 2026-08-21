"""Deterministic BridgeDB MCP tenancy lifecycle tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from bridge_db import clock, config
import bridge_db.tenancy as tenancy_module
import bridge_db.server as server_module
from bridge_db.server import (
    InstrumentedFastMCP,
    app_lifespan,
    mcp as server_mcp,
    monitor_tenancy_retirement,
)
from bridge_db.tenancy import (
    TenancyContractError,
    TenancyTracker,
    apply_lifecycle_plan,
    build_lifecycle_activation_evidence,
    derive_lifecycle_policy,
    plan_lifecycle,
    read_active_leases,
    tenancy_inventory,
    validate_lifecycle_activation_evidence,
)

_FIXTURE_GENERATION_ID = "0123456789ab-cdef01234567"


def _policy() -> dict[str, object]:
    return derive_lifecycle_policy(
        [
            {
                "owner": owner,
                "process_count": 1,
                "lifetime_seconds": 120,
                "rss_bytes": 32 * 1024 * 1024,
            }
            for owner in ("codex", "claude", "personal_ops", "hermes")
        ]
    )


def _activation_observations() -> list[dict[str, object]]:
    return [
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
    ]


def _tracker(
    root: Path,
    *,
    owner: str = "codex",
    generation: str = "generation-one",
    identity: str | None = "fixture-process-start",
) -> TenancyTracker:
    return TenancyTracker(
        root=root,
        owner=owner,
        principal="codex" if owner == "codex" else owner,
        generation=generation,
        pid=os.getpid(),
        process_identity=identity,
    )


def test_tracker_records_requests_rss_ancestry_and_normal_close(tmp_path: Path) -> None:
    root = tmp_path / "tenancy"
    tracker = _tracker(root)
    started = tracker.start()
    tracker.request_started("health")
    active = read_active_leases(root)[0][1]
    tracker.request_finished("health", outcome="succeeded")
    closed = tracker.close()

    assert started["owner"] == "codex"
    assert started["principal"] == "codex"
    assert started["generation"] == "generation-one"
    assert started["pid"] == os.getpid()
    assert isinstance(started["parent_pid"], int)
    assert isinstance(started["pid_ancestry"], list)
    assert started["rss_bytes"] > 0
    assert active["active_request_count"] == 1
    assert active["last_tool"] == "health"
    assert closed["ok"] is True
    assert read_active_leases(root) == []
    history = json.loads(Path(str(closed["history_path"])).read_text())
    assert history["lifecycle_reason"] == "normal_close"
    assert history["request_count"] == 1
    assert history["last_request_outcome"] == "succeeded"


def test_explicit_close_refuses_active_request(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path / "tenancy")
    tracker.start()
    tracker.request_started("save_snapshot")

    with pytest.raises(TenancyContractError) as refused:
        tracker.close()
    assert refused.value.reason_code == "tenancy.close_active_request_refused"

    tracker.request_finished("save_snapshot", outcome="failed")
    tracker.close(reason="client_close_after_failure")


def test_abrupt_exit_orphan_plan_and_apply_have_exact_readback(tmp_path: Path) -> None:
    root = tmp_path / "tenancy"
    tracker = _tracker(root)
    tracker.start()  # Deliberately no close: represents abrupt client exit.
    plan = plan_lifecycle(
        root=root,
        policy=_policy(),
        current_generation="generation-one",
        process_probe=lambda _pid, _identity: "missing",
    )

    assert plan["decisions"][0]["decision"] == "retire_orphan"
    receipt = apply_lifecycle_plan(
        root=root,
        plan=plan,
        process_probe=lambda _pid, _identity: "missing",
    )

    assert receipt["process_termination"] is False
    assert receipt["effects"] == [
        {
            "lease_id": tracker.lease_id,
            "effect": "retire_orphan",
            "readback": "active_absent_history_verified",
        }
    ]
    assert read_active_leases(root) == []


def test_crash_mid_request_retires_exact_missing_process(tmp_path: Path) -> None:
    root = tmp_path / "tenancy"
    tracker = _tracker(root)
    tracker.start()
    tracker.request_started("recall")
    plan = plan_lifecycle(
        root=root,
        policy=_policy(),
        current_generation="generation-one",
        process_probe=lambda _pid, _identity: "missing",
    )

    assert plan["decisions"][0]["decision"] == "retire_orphan"
    receipt = apply_lifecycle_plan(
        root=root,
        plan=plan,
        process_probe=lambda _pid, _identity: "missing",
    )
    assert receipt["effects"][0]["effect"] == "retire_orphan"
    assert read_active_leases(root) == []


def test_live_same_identity_active_request_is_never_drained(tmp_path: Path) -> None:
    root = tmp_path / "tenancy"
    tracker = _tracker(root)
    tracker.start()
    tracker.request_started("recall")
    plan = plan_lifecycle(
        root=root,
        policy=_policy(),
        current_generation="generation-new",
        process_probe=lambda _pid, _identity: "same",
    )

    assert plan["decisions"][0]["decision"] == "keep_active_request"
    assert (
        apply_lifecycle_plan(
            root=root,
            plan=plan,
            process_probe=lambda _pid, _identity: "same",
        )["effects"]
        == []
    )
    tracker.request_finished("recall", outcome="succeeded")
    tracker.close()


def test_pid_reuse_requires_rechecked_mismatch_before_retirement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tenancy"
    tracker = _tracker(root)
    tracker.start()
    plan = plan_lifecycle(
        root=root,
        policy=_policy(),
        current_generation="generation-one",
        process_probe=lambda _pid, _identity: "mismatch",
    )
    assert plan["decisions"][0]["decision"] == "retire_pid_reused"

    with pytest.raises(TenancyContractError) as changed:
        apply_lifecycle_plan(
            root=root,
            plan=plan,
            process_probe=lambda _pid, _identity: "same",
        )
    assert changed.value.reason_code == "tenancy.process_state_changed"


def test_generation_rollover_requests_cooperative_idle_close(tmp_path: Path) -> None:
    root = tmp_path / "tenancy"
    tracker = _tracker(root, generation="generation-old")
    tracker.start()
    plan = plan_lifecycle(
        root=root,
        policy=_policy(),
        current_generation="generation-new",
        process_probe=lambda _pid, _identity: "same",
    )

    assert plan["decisions"][0]["decision"] == "request_close_obsolete"
    receipt = apply_lifecycle_plan(
        root=root,
        plan=plan,
        process_probe=lambda _pid, _identity: "same",
    )
    assert receipt["effects"][0]["readback"] == "cooperative_close_request_verified"
    assert receipt["process_termination"] is False

    assert tracker.retirement_ready() is True
    record = read_active_leases(root)[0][1]
    assert record["retirement_requested"] is True
    assert record["retirement_ready"] is True
    assert record["lifecycle_reason"] == "obsolete_generation_idle"
    with pytest.raises(TenancyContractError) as refused:
        tracker.request_started("status")
    assert refused.value.reason_code == "tenancy.retirement_new_request_refused"
    tracker.close(reason="obsolete_generation_close")


@pytest.mark.asyncio
async def test_idle_marker_requests_server_shutdown_without_process_kill(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tenancy"
    tracker = _tracker(root)
    tracker.start()
    marker = root / "retire" / f"{tracker.lease_id}.json"
    marker.write_text(
        json.dumps({"lease_id": tracker.lease_id, "active_request_guard": 0}),
        encoding="utf-8",
    )
    marker.chmod(0o400)
    shutdown_requested = False

    def request_shutdown() -> None:
        nonlocal shutdown_requested
        shutdown_requested = True

    await monitor_tenancy_retirement(
        tracker,
        request_shutdown,
        poll_seconds=0.001,
    )

    assert shutdown_requested is True
    assert read_active_leases(root)[0][1]["retirement_ready"] is True
    tracker.close(reason="obsolete_generation_close")


@pytest.mark.asyncio
async def test_app_lifespan_close_removes_active_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tenancy"
    monkeypatch.setenv("BRIDGE_DB_TENANCY_ROOT", str(root))
    monkeypatch.delenv("BRIDGE_DB_PRINCIPAL_TOKEN", raising=False)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "bridge.db")
    monkeypatch.setattr(config, "PRINCIPALS_PATH", tmp_path / "principals.json")

    async with app_lifespan(server_mcp) as context:
        tracker = context.tenancy_tracker
        assert isinstance(tracker, TenancyTracker)
        lease_id = tracker.lease_id
        assert [record["lease_id"] for _, record in read_active_leases(root)] == [
            lease_id
        ]

    assert read_active_leases(root) == []
    histories = sorted((root / "history").glob(f"{lease_id}-*.json"))
    assert len(histories) == 1
    history = json.loads(histories[0].read_text(encoding="utf-8"))
    assert history["lifecycle_reason"] == "normal_close"


@pytest.mark.asyncio
async def test_obsolete_lifespan_cancels_own_idle_task_and_closes_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tenancy"
    monkeypatch.setenv("BRIDGE_DB_TENANCY_ROOT", str(root))
    monkeypatch.delenv("BRIDGE_DB_PRINCIPAL_TOKEN", raising=False)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "bridge.db")
    monkeypatch.setattr(config, "PRINCIPALS_PATH", tmp_path / "principals.json")
    original_monitor = server_module.monitor_tenancy_retirement

    async def fast_monitor(
        tracker: TenancyTracker,
        request_shutdown: Callable[[], None],
        *,
        poll_seconds: float = 1.0,
    ) -> None:
        await original_monitor(
            tracker,
            request_shutdown,
            poll_seconds=min(poll_seconds, 0.001),
        )

    def process_signal_forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cooperative lifespan closure must not signal a process")

    monkeypatch.setattr(server_module, "monitor_tenancy_retirement", fast_monitor)
    monkeypatch.setattr("bridge_db.tenancy.os.kill", process_signal_forbidden)
    started = asyncio.Event()
    observed_lease: str | None = None

    async def run_server_lifespan() -> str:
        nonlocal observed_lease
        try:
            async with app_lifespan(server_mcp) as context:
                tracker = context.tenancy_tracker
                assert isinstance(tracker, TenancyTracker)
                observed_lease = tracker.lease_id
                marker = root / "retire" / f"{tracker.lease_id}.json"
                marker.write_text(
                    json.dumps({"lease_id": tracker.lease_id}), encoding="utf-8"
                )
                marker.chmod(0o400)
                started.set()
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert observed_lease is not None
            return observed_lease
        raise AssertionError("obsolete lifespan did not cancel its own task")

    task = asyncio.create_task(run_server_lifespan())
    # SQLite/WAL initialization can exceed one second on a loaded host; the
    # lifecycle assertion is event-driven and does not depend on that latency.
    await asyncio.wait_for(started.wait(), timeout=10)
    lease_id = await asyncio.wait_for(task, timeout=10)

    assert read_active_leases(root) == []
    histories = sorted((root / "history").glob(f"{lease_id}-*.json"))
    assert len(histories) == 1
    history = json.loads(histories[0].read_text(encoding="utf-8"))
    assert history["lifecycle_reason"] == "obsolete_generation_close"


def test_active_request_finishes_before_cooperative_shutdown(tmp_path: Path) -> None:
    root = tmp_path / "tenancy"
    tracker = _tracker(root)
    tracker.start()
    tracker.request_started("health")
    marker = root / "retire" / f"{tracker.lease_id}.json"
    marker.write_text(json.dumps({"lease_id": tracker.lease_id}), encoding="utf-8")
    marker.chmod(0o400)

    assert tracker.retirement_ready() is False
    tracker.request_finished("health", outcome="succeeded")
    assert tracker.retirement_ready() is True
    tracker.close(reason="obsolete_generation_close")


def test_generation_rollover_rechecks_exact_pid_ancestry(tmp_path: Path) -> None:
    root = tmp_path / "tenancy"
    tracker = _tracker(root, generation="generation-old")
    tracker.start()
    recorded_ancestry = read_active_leases(root)[0][1]["pid_ancestry"]
    plan = plan_lifecycle(
        root=root,
        policy=_policy(),
        current_generation="generation-new",
        process_probe=lambda _pid, _identity: "same",
        ancestry_probe=lambda _pid: recorded_ancestry,
    )

    with pytest.raises(TenancyContractError) as changed:
        apply_lifecycle_plan(
            root=root,
            plan=plan,
            process_probe=lambda _pid, _identity: "same",
            ancestry_probe=lambda _pid: [*recorded_ancestry, 999_999],
        )

    assert changed.value.reason_code == "tenancy.ancestry_changed"
    assert not (root / "retire" / f"{tracker.lease_id}.json").exists()
    tracker.close()


def test_app_restart_preserves_both_close_history_records(tmp_path: Path) -> None:
    root = tmp_path / "tenancy"
    before = _tracker(root, identity=None)
    before.start()
    before.close(reason="app_restart")

    after = _tracker(root, identity=None)
    after.start()
    assert tenancy_inventory(root)["active_count"] == 1
    after.close(reason="normal_close_after_restart")

    histories = sorted((root / "history").glob("*.json"))
    assert len(histories) == 2
    reasons = {json.loads(path.read_text())["lifecycle_reason"] for path in histories}
    assert reasons == {"app_restart", "normal_close_after_restart"}


def test_alive_stale_lease_routes_to_owner_review_not_reaping(tmp_path: Path) -> None:
    root = tmp_path / "tenancy"
    fixed_now = datetime(2026, 8, 5, 0, 0, tzinfo=UTC)
    clock.install(lambda: fixed_now)
    try:
        tracker = _tracker(root)
        tracker.start()
        later = fixed_now + timedelta(hours=1)
        plan = plan_lifecycle(
            root=root,
            policy=_policy(),
            current_generation="generation-one",
            now=later,
            process_probe=lambda _pid, _identity: "same",
        )
        assert plan["decisions"][0]["decision"] == "owner_review_lifetime"
        assert (
            apply_lifecycle_plan(
                root=root,
                plan=plan,
                process_probe=lambda _pid, _identity: "same",
            )["effects"]
            == []
        )
        tracker.close()
    finally:
        clock.reset()


def test_replay_derived_budgets_cover_four_client_families() -> None:
    observations = [
        {
            "owner": "codex",
            "process_count": 3,
            "lifetime_seconds": 100,
            "rss_bytes": 1000,
            "scenario": "normal_close",
        },
        {
            "owner": "claude",
            "process_count": 4,
            "lifetime_seconds": 200,
            "rss_bytes": 2000,
            "scenario": "app_restart",
        },
        {
            "owner": "personal_ops",
            "process_count": 1,
            "lifetime_seconds": 50,
            "rss_bytes": 3000,
            "scenario": "abrupt_exit",
        },
        {
            "owner": "hermes",
            "process_count": 2,
            "lifetime_seconds": 80,
            "rss_bytes": 4000,
            "scenario": "generation_rollover",
        },
    ]

    policy = derive_lifecycle_policy(observations)

    assert set(policy["budgets"]) == {"codex", "claude", "personal_ops", "hermes"}
    assert policy["budgets"]["codex"]["max_processes"] == 4
    assert policy["budgets"]["claude"]["max_lifetime_seconds"] == 250
    assert len(policy["policy_sha256"]) == 64


def test_activation_evidence_binds_exact_replays_policy_and_coverage() -> None:
    evidence = build_lifecycle_activation_evidence(
        _activation_observations(), generation_id=_FIXTURE_GENERATION_ID
    )

    summary = validate_lifecycle_activation_evidence(evidence)

    assert summary["state"] == "verified"
    assert summary["generation_id"] == _FIXTURE_GENERATION_ID
    assert summary["owners"] == ["claude", "codex", "hermes", "personal_ops"]
    assert summary["scenarios"] == [
        "abrupt_exit",
        "app_restart",
        "generation_rollover",
        "normal_close",
    ]
    assert summary["observation_counts"] == {
        "claude": 1,
        "codex": 1,
        "hermes": 1,
        "personal_ops": 1,
    }


def test_activation_evidence_refuses_missing_owner_or_scenario_coverage() -> None:
    with pytest.raises(TenancyContractError) as refused:
        build_lifecycle_activation_evidence(
            _activation_observations()[:-1],
            generation_id=_FIXTURE_GENERATION_ID,
        )

    assert refused.value.reason_code == "tenancy.activation_evidence_coverage_missing"


def test_replay_policy_refuses_non_finite_derived_budget() -> None:
    observations = _activation_observations()
    observations[0]["lifetime_seconds"] = 1e308

    with pytest.raises(TenancyContractError) as refused:
        build_lifecycle_activation_evidence(
            observations, generation_id=_FIXTURE_GENERATION_ID
        )

    assert refused.value.reason_code == "tenancy.replay_value_invalid"


def test_activation_evidence_recomputes_policy_instead_of_trusting_digest() -> None:
    evidence = build_lifecycle_activation_evidence(
        _activation_observations(), generation_id=_FIXTURE_GENERATION_ID
    )
    evidence["policy"]["budgets"]["codex"]["max_processes"] = 999
    body = {key: value for key, value in evidence.items() if key != "evidence_sha256"}
    evidence["evidence_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    with pytest.raises(TenancyContractError) as refused:
        validate_lifecycle_activation_evidence(evidence)

    assert refused.value.reason_code == "tenancy.activation_policy_mismatch"


def test_derive_activation_evidence_cli_emits_valid_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observations_path = tmp_path / "observations.json"
    observations_path.write_text(
        json.dumps(
            {
                "schema": "BridgeMcpTenancyReplayObservationsV1",
                "observations": _activation_observations(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "bridge-db-tenancy",
            "derive-activation-evidence",
            "--observations",
            str(observations_path),
            "--generation-id",
            _FIXTURE_GENERATION_ID,
        ],
    )

    tenancy_module.main()

    emitted = json.loads(capsys.readouterr().out)
    assert validate_lifecycle_activation_evidence(emitted)["state"] == "verified"


@pytest.mark.parametrize(
    ("owner", "principal"),
    [("codex", "codex"), ("claude", "cc"), ("personal_ops", "personal_ops")],
)
def test_representative_client_close_replay(
    tmp_path: Path, owner: str, principal: str
) -> None:
    tracker = TenancyTracker(
        root=tmp_path / owner,
        owner=owner,
        principal=principal,
        generation="generation-replay",
        pid=os.getpid(),
        process_identity=f"fixture-{owner}",
    )
    tracker.start()
    tracker.request_started("health")
    tracker.request_finished("health", outcome="succeeded")
    result = tracker.close(reason="representative_client_close")

    history = json.loads(Path(str(result["history_path"])).read_text())
    assert history["owner"] == owner
    assert history["principal"] == principal
    assert history["request_count"] == 1


def test_inventory_refuses_insecure_children_without_chmod(tmp_path: Path) -> None:
    root = tmp_path / "tenancy"
    root.mkdir(mode=0o700)
    for name in ("active", "history", "retire"):
        (root / name).mkdir(mode=0o700)
    (root / "active").chmod(0o755)

    inventory = tenancy_inventory(root)

    assert inventory["state"] == "unverified"
    assert inventory["reason_code"] == "tenancy.child_not_private"
    assert (root / "active").stat().st_mode & 0o777 == 0o755


@pytest.mark.asyncio
async def test_instrumented_mcp_accounts_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, str]] = []

    class _Tracker:
        def request_started(self, name: str) -> None:
            events.append(("started", name))

        def request_finished(self, name: str, *, outcome: str) -> None:
            events.append((outcome, name))

    class _Lifespan:
        tenancy_tracker = _Tracker()

    class _Request:
        lifespan_context = _Lifespan()

    class _Context:
        request_context = _Request()

    async def _call_tool(
        _self: FastMCP, name: str, _arguments: dict[str, object]
    ) -> dict[str, object]:
        if name == "explode":
            raise RuntimeError("fixture failure")
        return {"ok": True}

    server = InstrumentedFastMCP("tenancy-test")
    monkeypatch.setattr(server, "get_context", lambda: _Context())
    monkeypatch.setattr(FastMCP, "call_tool", _call_tool)

    assert await server.call_tool("health", {}) == {"ok": True}
    with pytest.raises(RuntimeError, match="fixture failure"):
        await server.call_tool("explode", {})
    assert events == [
        ("started", "health"),
        ("succeeded", "health"),
        ("started", "explode"),
        ("failed", "explode"),
    ]


@pytest.mark.asyncio
async def test_shared_runtime_serializes_one_broker_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    highwater = 0

    class _Lifespan:
        tenancy_tracker = None

    class _Request:
        lifespan_context = _Lifespan()

    class _Context:
        request_context = _Request()

    async def _call_tool(
        _self: FastMCP, _name: str, _arguments: dict[str, object]
    ) -> dict[str, object]:
        nonlocal active, highwater
        active += 1
        highwater = max(highwater, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"ok": True}

    server = InstrumentedFastMCP("shared-serialization-test")
    server.enable_shared_runtime()
    monkeypatch.setattr(server, "get_context", lambda: _Context())
    monkeypatch.setattr(FastMCP, "call_tool", _call_tool)

    await asyncio.gather(
        server.call_tool("first", {}),
        server.call_tool("second", {}),
    )

    assert highwater == 1


def test_inventory_reports_owner_generation_requests_and_rss(tmp_path: Path) -> None:
    root = tmp_path / "tenancy"
    tracker = TenancyTracker(
        root=root,
        owner="hermes",
        principal="hermes",
        generation="generation-hermes",
        pid=os.getpid(),
    )
    tracker.start()
    tracker.request_started("health")

    inventory = tenancy_inventory(root)

    assert inventory["state"] == "observed"
    assert inventory["schema"] == "BridgeMcpTenancyInventoryV2"
    assert inventory["active_count"] == 1
    assert inventory["lease_count"] == 1
    assert inventory["stale_lease_count"] == 0
    assert inventory["process_states"] == {
        "same": 1,
        "missing": 0,
        "mismatch": 0,
        "unknown": 0,
    }
    assert inventory["owners"] == {"hermes": 1}
    assert inventory["generations"] == {"generation-hermes": 1}
    assert inventory["active_request_count"] == 1
    assert inventory["rss_total_bytes"] > 0
    assert inventory["rss_measurement_state"] == "observed"
    assert inventory["rss_observed_process_count"] == 1
    assert inventory["rss_unverified_process_count"] == 0
    assert inventory["lease_last_observed_rss_total_bytes"] > 0
    tracker.request_finished("health", outcome="succeeded")
    tracker.close()


def test_inventory_separates_stale_lease_from_live_process(tmp_path: Path) -> None:
    root = tmp_path / "tenancy"
    tracker = _tracker(root, identity="fixture-not-the-live-process-identity")
    tracker.start()

    inventory = tenancy_inventory(root)

    assert inventory["active_count"] == 0
    assert inventory["lease_count"] == 1
    assert inventory["stale_lease_count"] == 1
    assert inventory["process_states"]["mismatch"] == 1
    assert inventory["owners"] == {}
    assert inventory["lease_owners"] == {"codex": 1}
    assert inventory["rss_total_bytes"] == 0
    assert inventory["lease_last_observed_rss_total_bytes"] > 0
    tracker.close()


def test_inventory_preserves_live_and_reused_identity_leases_for_one_pid(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tenancy"
    live = TenancyTracker(
        root=root,
        owner="codex",
        principal="codex",
        generation="generation-live",
        pid=os.getpid(),
    )
    reused = _tracker(
        root,
        generation="generation-stale",
        identity="fixture-prior-process-with-reused-pid",
    )
    live.start()
    reused.start()

    inventory = tenancy_inventory(root)

    assert inventory["active_count"] == 1
    assert inventory["lease_count"] == 2
    assert inventory["stale_lease_count"] == 1
    assert inventory["process_states"] == {
        "same": 1,
        "missing": 0,
        "mismatch": 1,
        "unknown": 0,
    }
    assert inventory["generations"] == {"generation-live": 1}
    assert inventory["lease_generations"] == {
        "generation-live": 1,
        "generation-stale": 1,
    }
    reused.close()
    live.close()


def test_apply_rejects_changed_lease_and_tampered_plan(tmp_path: Path) -> None:
    root = tmp_path / "tenancy"
    tracker = _tracker(root)
    tracker.start()
    plan = plan_lifecycle(
        root=root,
        policy=_policy(),
        current_generation="generation-one",
        process_probe=lambda _pid, _identity: "missing",
    )
    tampered = {**plan, "current_generation": "attacker-change"}
    with pytest.raises(TenancyContractError) as digest:
        apply_lifecycle_plan(
            root=root,
            plan=tampered,
            process_probe=lambda _pid, _identity: "missing",
        )
    assert digest.value.reason_code == "tenancy.plan_digest_mismatch"

    path, record = read_active_leases(root)[0]
    record["last_tool"] = "changed-after-plan"
    path.write_text(json.dumps(record), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(TenancyContractError) as changed:
        apply_lifecycle_plan(
            root=root,
            plan=plan,
            process_probe=lambda _pid, _identity: "missing",
        )
    assert changed.value.reason_code == "tenancy.plan_lease_changed"


def test_apply_requires_exact_target_for_multiple_effects(tmp_path: Path) -> None:
    root = tmp_path / "tenancy"
    first = _tracker(root, identity="fixture-first")
    second = _tracker(root, identity="fixture-second")
    first.start()
    second.start()
    plan = plan_lifecycle(
        root=root,
        policy=_policy(),
        current_generation="generation-one",
        process_probe=lambda _pid, _identity: "missing",
    )

    with pytest.raises(TenancyContractError) as broad:
        apply_lifecycle_plan(
            root=root,
            plan=plan,
            process_probe=lambda _pid, _identity: "missing",
        )
    assert broad.value.reason_code == "tenancy.multiple_effects_require_exact_target"

    receipt = apply_lifecycle_plan(
        root=root,
        plan=plan,
        process_probe=lambda _pid, _identity: "missing",
        target_lease_id=first.lease_id,
    )
    assert receipt["effect_cardinality"] == 1
    assert receipt["target_lease_id"] == first.lease_id
    assert [record["lease_id"] for _, record in read_active_leases(root)] == [
        second.lease_id
    ]
