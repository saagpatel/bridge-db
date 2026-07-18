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
from bridge_db import recovery
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


def _create_fixture_archive(root: Path) -> tuple[Path, str]:
    plan = policy.collect_evidence_plan()
    archive = root / "archive"
    policy.create_evidence_archive(
        archive,
        expected_snapshot_sha256=plan["snapshot_sha256"],
    )
    return archive, str(plan["snapshot_sha256"])


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


def test_plan_and_archive_include_recovery_anchor_bundle(
    evidence_paths: Path,
) -> None:
    anchor = recovery.recovery_anchor_path(config.DB_PATH)
    anchor.mkdir(mode=0o700)
    database = anchor / recovery.RECOVERY_DATABASE_NAME
    manifest = anchor / recovery.RECOVERY_MANIFEST_NAME
    database.write_bytes(b"recovery database")
    manifest.write_text('{"schema":"RecoveryAnchorV1"}\n', encoding="utf-8")
    database.chmod(0o600)
    manifest.chmod(0o600)

    plan = policy.collect_evidence_plan()
    anchor_artifacts = {
        item["kind"]: item
        for item in plan["artifacts"]
        if item["kind"].startswith("recovery_anchor_")
    }

    assert set(anchor_artifacts) == {
        "recovery_anchor_database",
        "recovery_anchor_manifest",
    }
    assert anchor_artifacts["recovery_anchor_database"]["source_path"] == str(database)
    assert anchor_artifacts["recovery_anchor_manifest"]["source_path"] == str(manifest)
    assert all(
        item["retention"] == "preserve"
        and item["destructive_action"] == "blocked"
        for item in anchor_artifacts.values()
    )

    archive = evidence_paths / "archive-with-anchor"
    result = policy.create_evidence_archive(
        archive,
        expected_snapshot_sha256=plan["snapshot_sha256"],
    )
    archived_manifest = json.loads((archive / "manifest.json").read_text())
    archived_anchor_sources = {
        item["source_path"]
        for item in archived_manifest["artifacts"]
        if item["kind"].startswith("recovery_anchor_")
    }

    assert result["ok"] is True
    assert archived_anchor_sources == {str(database), str(manifest)}
    assert database.read_bytes() == b"recovery database"
    assert manifest.read_text(encoding="utf-8") == '{"schema":"RecoveryAnchorV1"}\n'


def test_plan_preserves_existing_recovery_anchor_sidecars(
    evidence_paths: Path,
) -> None:
    anchor = recovery.recovery_anchor_path(config.DB_PATH)
    anchor.mkdir(mode=0o700)
    (anchor / recovery.RECOVERY_DATABASE_NAME).write_bytes(b"recovery database")
    (anchor / recovery.RECOVERY_MANIFEST_NAME).write_text(
        '{"schema":"RecoveryAnchorV1"}\n',
        encoding="utf-8",
    )
    sidecars = {
        anchor / name for name in recovery.RECOVERY_DATABASE_SIDECAR_NAMES
    }
    for sidecar in sidecars:
        sidecar.write_bytes(b"existing sidecar evidence")
    unexpected = anchor / "operator-note.bin"
    unexpected.write_bytes(b"unexpected recovery evidence")

    plan = policy.collect_evidence_plan()
    planned_sidecars = {
        Path(item["source_path"])
        for item in plan["artifacts"]
        if item["kind"] == "recovery_anchor_sidecar"
    }

    assert planned_sidecars == sidecars
    assert any(
        item["kind"] == "recovery_anchor_unexpected_artifact"
        and Path(item["source_path"]) == unexpected
        for item in plan["artifacts"]
    )


def test_plan_refuses_nonregular_unexpected_recovery_anchor_artifact(
    evidence_paths: Path,
) -> None:
    anchor = recovery.recovery_anchor_path(config.DB_PATH)
    anchor.mkdir(mode=0o700)
    (anchor / recovery.RECOVERY_DATABASE_NAME).write_bytes(b"recovery database")
    (anchor / recovery.RECOVERY_MANIFEST_NAME).write_text(
        '{"schema":"RecoveryAnchorV1"}\n',
        encoding="utf-8",
    )
    (anchor / "unexpected-directory").mkdir()

    with pytest.raises(policy.EvidencePolicyError, match="not a regular file"):
        policy.collect_evidence_plan()


def test_plan_refuses_partial_recovery_anchor_bundle(
    evidence_paths: Path,
) -> None:
    anchor = recovery.recovery_anchor_path(config.DB_PATH)
    anchor.mkdir(mode=0o700)
    (anchor / recovery.RECOVERY_DATABASE_NAME).write_bytes(b"incomplete")

    with pytest.raises(policy.EvidencePolicyError, match="evidence file unavailable"):
        policy.collect_evidence_plan()


def test_plan_refuses_dangling_recovery_anchor_symlink(
    evidence_paths: Path,
) -> None:
    anchor = recovery.recovery_anchor_path(config.DB_PATH)
    anchor.symlink_to(evidence_paths / "missing-anchor", target_is_directory=True)

    assert anchor.is_symlink()
    assert not anchor.exists()
    with pytest.raises(policy.EvidencePolicyError, match="not a regular directory"):
        policy.collect_evidence_plan()


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


def test_recall_redaction_dry_run_is_archive_bound_and_non_mutating(
    evidence_paths: Path,
) -> None:
    archive, snapshot = _create_fixture_archive(evidence_paths)
    before = policy.RECALL_LOG_PATH.read_bytes()

    result = policy.redact_legacy_recall_queries(
        archive_path=archive,
        expected_snapshot_sha256=snapshot,
        expected_raw_query_records=1,
        actor="operator",
        reason="approved archive-and-redact",
        apply=False,
    )

    assert result["status"] == "would_redact"
    assert result["raw_query_records"] == 1
    assert result["record_count"] == 1
    assert result["records_deleted"] == 0
    assert policy.RECALL_LOG_PATH.read_bytes() == before
    assert not config.EVIDENCE_DISPOSITION_LOG_PATH.exists()


def test_recall_redaction_apply_preserves_records_and_empty_semantics(
    evidence_paths: Path,
) -> None:
    append_jsonl_durable(
        policy.RECALL_LOG_PATH,
        {"query": "   ", "result_count": 0},
        rotate_bytes=1024,
    )
    append_jsonl_durable(
        policy.RECALL_LOG_PATH,
        {"query_empty": False, "result_count": 2},
        rotate_bytes=1024,
    )
    archive, snapshot = _create_fixture_archive(evidence_paths)

    result = policy.redact_legacy_recall_queries(
        archive_path=archive,
        expected_snapshot_sha256=snapshot,
        expected_raw_query_records=2,
        actor="operator",
        reason="approved archive-and-redact",
        apply=True,
    )

    assert result["status"] == "completed"
    assert result["record_count"] == 3
    assert result["records_deleted"] == 0
    records = [
        json.loads(line) for line in policy.RECALL_LOG_PATH.read_text().splitlines()
    ]
    assert len(records) == 3
    assert all("query" not in record for record in records)
    assert records[0]["query_empty"] is False
    assert records[1]["query_empty"] is True
    assert records[2]["query_empty"] is False
    assert records[0]["query_text_redacted"] is True
    assert records[1]["query_text_redacted"] is True
    receipts = [
        json.loads(line)
        for line in config.EVIDENCE_DISPOSITION_LOG_PATH.read_text().splitlines()
    ]
    assert [receipt["status"] for receipt in receipts] == ["prepared", "completed"]
    assert all(receipt["records_deleted"] == 0 for receipt in receipts)
    assert "historical private query" not in json.dumps(receipts)


def test_recall_redaction_refuses_count_drift_without_receipt(
    evidence_paths: Path,
) -> None:
    archive, snapshot = _create_fixture_archive(evidence_paths)

    with pytest.raises(policy.EvidencePolicyError, match="count changed"):
        policy.redact_legacy_recall_queries(
            archive_path=archive,
            expected_snapshot_sha256=snapshot,
            expected_raw_query_records=2,
            actor="operator",
            reason="approved archive-and-redact",
            apply=True,
        )

    assert "query" in json.loads(policy.RECALL_LOG_PATH.read_text())
    assert not config.EVIDENCE_DISPOSITION_LOG_PATH.exists()


def test_recall_redaction_replace_failure_preserves_source_and_aborts_receipt(
    evidence_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, snapshot = _create_fixture_archive(evidence_paths)
    before = policy.RECALL_LOG_PATH.read_bytes()
    original_replace = policy.os.replace

    def fail_recall_replace(source: Path, destination: Path) -> None:
        if Path(destination) == policy.RECALL_LOG_PATH:
            raise OSError("simulated redaction publication crash")
        original_replace(source, destination)

    monkeypatch.setattr(policy.os, "replace", fail_recall_replace)
    with pytest.raises(OSError, match="publication crash"):
        policy.redact_legacy_recall_queries(
            archive_path=archive,
            expected_snapshot_sha256=snapshot,
            expected_raw_query_records=1,
            actor="operator",
            reason="approved archive-and-redact",
            apply=True,
        )

    assert policy.RECALL_LOG_PATH.read_bytes() == before
    receipts = [
        json.loads(line)
        for line in config.EVIDENCE_DISPOSITION_LOG_PATH.read_text().splitlines()
    ]
    assert [receipt["status"] for receipt in receipts] == ["prepared", "aborted"]
    assert list(evidence_paths.glob(".*.redact.*")) == []


def test_recall_redaction_completion_failure_leaves_open_prepared_evidence(
    evidence_paths: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, snapshot = _create_fixture_archive(evidence_paths)
    original_append = policy.append_jsonl_durable

    def fail_completed(
        path: Path, event: dict[str, object], *, rotate_bytes: int
    ) -> object:
        if event["status"] == "completed":
            raise OSError("simulated completion receipt failure")
        return original_append(path, event, rotate_bytes=rotate_bytes)

    monkeypatch.setattr(policy, "append_jsonl_durable", fail_completed)
    with pytest.raises(OSError, match="completion receipt failure"):
        policy.redact_legacy_recall_queries(
            archive_path=archive,
            expected_snapshot_sha256=snapshot,
            expected_raw_query_records=1,
            actor="operator",
            reason="approved archive-and-redact",
            apply=True,
        )

    assert "query" not in json.loads(policy.RECALL_LOG_PATH.read_text())
    receipt = json.loads(config.EVIDENCE_DISPOSITION_LOG_PATH.read_text())
    assert receipt["status"] == "prepared"
