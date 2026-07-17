"""Non-destructive evidence policy, acknowledgement, and archive tests."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from bridge_db import config
from bridge_db import evidence_policy as policy
from bridge_db.evidence import append_jsonl_durable


@pytest.fixture
def evidence_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "bridge.db")
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(
        config, "AUDIT_FAILURE_LOG_PATH", tmp_path / "audit_failures.jsonl"
    )
    monkeypatch.setattr(
        config,
        "EVIDENCE_ACK_LOG_PATH",
        tmp_path / "evidence_acknowledgements.jsonl",
    )
    monkeypatch.setattr(policy, "RECALL_LOG_PATH", tmp_path / "recall.jsonl")
    append_jsonl_durable(
        config.AUDIT_LOG_PATH,
        {"tool": "health", "ok": True},
        rotate_bytes=1024,
    )
    append_jsonl_durable(
        policy.RECALL_LOG_PATH,
        {"query": "historical private query", "result_count": 1},
        rotate_bytes=1024,
    )
    backup = Path(f"{config.DB_PATH}.pre-v1.bak")
    backup.write_bytes(b"backup evidence")
    return tmp_path


def test_plan_is_content_bound_and_does_not_redisclose_queries(
    evidence_paths: Path,
) -> None:
    plan = policy.collect_evidence_plan()
    encoded = json.dumps(plan)

    assert plan["schema"] == policy.PLAN_SCHEMA
    assert plan["policy"]["destructive_actions"] == "blocked"
    assert plan["historical_raw_queries"]["raw_query_records"] == 1
    assert plan["migration_backups"]["count"] == 1
    assert plan["migration_backups"]["verified_count"] == 0
    assert "historical private query" not in encoded
    assert all(item["destructive_action"] == "blocked" for item in plan["artifacts"])
    assert policy.collect_evidence_plan()["snapshot_sha256"] == plan["snapshot_sha256"]

    original = plan["snapshot_sha256"]
    with open(config.AUDIT_LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write('{"tool":"status"}\n')
    assert policy.collect_evidence_plan()["snapshot_sha256"] != original


def test_acknowledgement_is_exact_and_never_authorizes_cleanup(
    evidence_paths: Path,
) -> None:
    plan = policy.collect_evidence_plan()
    receipt = policy.acknowledge_evidence_plan(
        expected_snapshot_sha256=plan["snapshot_sha256"],
        actor="operator",
        reason="reviewed preservation plan",
    )

    assert receipt["destructive_authority"] is False
    assert receipt["source_rewrite_authority"] is False
    persisted = json.loads(config.EVIDENCE_ACK_LOG_PATH.read_text())
    assert persisted == receipt
    assert config.AUDIT_LOG_PATH.exists()
    assert policy.RECALL_LOG_PATH.exists()

    with pytest.raises(policy.EvidencePolicyError, match="stale"):
        policy.acknowledge_evidence_plan(
            expected_snapshot_sha256=plan["snapshot_sha256"],
            actor="operator",
            reason="duplicate stale plan",
        )


def test_archive_is_verified_and_preserves_every_source(
    evidence_paths: Path,
) -> None:
    plan = policy.collect_evidence_plan()
    source_digests = {item["source_path"]: item["sha256"] for item in plan["artifacts"]}
    archive = evidence_paths / "archive"

    result = policy.create_evidence_archive(
        archive,
        expected_snapshot_sha256=plan["snapshot_sha256"],
    )

    assert result["ok"] is True
    assert result["artifact_count"] == len(plan["artifacts"])
    assert result["source_preserved"] is True
    assert result["destructive_authority"] is False
    assert (
        policy.verify_evidence_archive(
            archive,
            expected_snapshot_sha256=plan["snapshot_sha256"],
        )
        == result
    )
    for source, expected_digest in source_digests.items():
        assert Path(source).exists()
        assert hashlib.sha256(Path(source).read_bytes()).hexdigest() == expected_digest
    for archived in archive.rglob("*"):
        if archived.is_file() or archived.is_dir():
            assert archived.stat().st_mode & 0o077 == 0


def test_archive_refuses_stale_plan_without_creating_destination(
    evidence_paths: Path,
) -> None:
    plan = policy.collect_evidence_plan()
    with open(config.AUDIT_LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write('{"changed":true}\n')
    archive = evidence_paths / "archive"

    with pytest.raises(policy.EvidencePolicyError, match="stale"):
        policy.create_evidence_archive(
            archive,
            expected_snapshot_sha256=plan["snapshot_sha256"],
        )

    assert not archive.exists()


def test_archive_publication_failure_preserves_sources_and_cleans_temp(
    evidence_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = policy.collect_evidence_plan()
    archive = evidence_paths / "archive"
    original_replace = policy.os.replace

    def fail_publish(source: Path, destination: Path) -> None:
        if Path(destination) == archive:
            raise OSError("simulated publication crash")
        original_replace(source, destination)

    monkeypatch.setattr(policy.os, "replace", fail_publish)
    with pytest.raises(OSError, match="publication crash"):
        policy.create_evidence_archive(
            archive,
            expected_snapshot_sha256=plan["snapshot_sha256"],
        )

    assert not archive.exists()
    assert list(evidence_paths.glob(".archive.tmp.*")) == []
    assert config.AUDIT_LOG_PATH.exists()
    assert policy.RECALL_LOG_PATH.exists()


def test_archive_readback_rejects_tampering(evidence_paths: Path) -> None:
    plan = policy.collect_evidence_plan()
    archive = evidence_paths / "archive"
    policy.create_evidence_archive(
        archive,
        expected_snapshot_sha256=plan["snapshot_sha256"],
    )
    manifest = json.loads((archive / "manifest.json").read_text())
    artifact = archive / manifest["artifacts"][0]["archive_path"]
    os.chmod(artifact, 0o600)
    with open(artifact, "ab") as handle:
        handle.write(b"tamper")

    with pytest.raises(policy.EvidencePolicyError, match="digest mismatch"):
        policy.verify_evidence_archive(
            archive,
            expected_snapshot_sha256=plan["snapshot_sha256"],
        )


def test_archive_readback_requires_independent_plan_digest(
    evidence_paths: Path,
) -> None:
    plan = policy.collect_evidence_plan()
    archive = evidence_paths / "archive"
    policy.create_evidence_archive(
        archive,
        expected_snapshot_sha256=plan["snapshot_sha256"],
    )

    with pytest.raises(policy.EvidencePolicyError, match="expected evidence plan"):
        policy.verify_evidence_archive(
            archive,
            expected_snapshot_sha256="0" * 64,
        )


def test_plan_rejects_symlink_evidence(evidence_paths: Path, tmp_path: Path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_text("{}\n", encoding="utf-8")
    config.AUDIT_LOG_PATH.unlink()
    config.AUDIT_LOG_PATH.symlink_to(target)

    with pytest.raises(policy.EvidencePolicyError, match="not a regular file"):
        policy.collect_evidence_plan()


def test_plan_rejects_colliding_evidence_family_paths(
    evidence_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "EVIDENCE_ACK_LOG_PATH", config.AUDIT_LOG_PATH)

    with pytest.raises(policy.EvidencePolicyError, match="must be distinct"):
        policy.collect_evidence_plan()


def test_acknowledgement_rejects_oversized_reason(evidence_paths: Path) -> None:
    plan = policy.collect_evidence_plan()

    with pytest.raises(policy.EvidencePolicyError, match="reason exceeds"):
        policy.acknowledge_evidence_plan(
            expected_snapshot_sha256=plan["snapshot_sha256"],
            actor="operator",
            reason="x" * (config.EVIDENCE_ACK_REASON_MAX_BYTES + 1),
        )

    assert not config.EVIDENCE_ACK_LOG_PATH.exists()


def test_plan_cli_emits_machine_readable_contract(
    evidence_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["evidence_policy", "plan"])

    policy.main()

    output = json.loads(capsys.readouterr().out)
    assert output["schema"] == policy.PLAN_SCHEMA
    assert len(output["snapshot_sha256"]) == 64
    assert output["policy"]["destructive_actions"] == "blocked"


def test_verify_cli_requires_and_reports_independent_plan_digest(
    evidence_paths: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = policy.collect_evidence_plan()
    archive = evidence_paths / "archive"
    policy.create_evidence_archive(
        archive,
        expected_snapshot_sha256=plan["snapshot_sha256"],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evidence_policy",
            "verify",
            "--archive",
            str(archive),
            "--expected-snapshot-sha256",
            plan["snapshot_sha256"],
        ],
    )

    policy.main()

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["snapshot_sha256"] == plan["snapshot_sha256"]
