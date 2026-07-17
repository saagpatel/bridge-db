"""Tests for the private Codex baseline seed entrypoint."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bridge_db import config
from bridge_db import clock
from bridge_db.codex_seed import (
    CURRENT_FINGERPRINT_VERSION,
    LEGACY_FINGERPRINT_VERSION,
    apply_manifest,
    fingerprint_manifest_v2,
    load_manifest,
)
from bridge_db.db import open_db


def make_manifest() -> dict[str, object]:
    snapshot_payload = {
        "infrastructure": "- Automations: 17 active",
        "automation_digest": "- Runtime health: healthy",
        "active_projects": "- ResumeEvolver",
    }
    return {
        "fingerprint": "f393a8e9e5fee06654af9e28f7b3a3b33a850911c22394e30ebe272c7c0c1f5a",
        "snapshot_date": "2026-04-14",
        "snapshot_payload": snapshot_payload,
        "baseline_activity": {
            "caller": "codex",
            "timestamp": "2026-04-14",
            "project_name": "bridge-baseline-seed",
            "summary": "Seeded Codex baseline from reconciled truth.",
            "tags": ["BASELINE", "CODEX-STATE", "TRUTH-RECONCILED"],
        },
    }


def make_variant_manifest() -> dict[str, object]:
    manifest = make_manifest()
    manifest["snapshot_payload"] = {
        "infrastructure": "- Automations: 17 active\n- MCP servers: 13 connected",
        "automation_digest": "- Runtime health: healthy",
        "active_projects": "- ResumeEvolver",
    }
    manifest["baseline_activity"] = {
        "caller": "codex",
        "timestamp": "2026-04-14",
        "project_name": "bridge-baseline-seed",
        "summary": "Seeded Codex baseline from corrected reconciled truth.",
        "tags": ["BASELINE", "CODEX-STATE", "TRUTH-RECONCILED"],
    }
    manifest["fingerprint"] = (
        "0fca697e6281b8c47e2232a8e29a38760d614e13994470c4766ee18a6f5a0d10"
    )
    return manifest


def make_v2_manifest() -> dict[str, object]:
    manifest = make_manifest()
    manifest["fingerprint_version"] = CURRENT_FINGERPRINT_VERSION
    manifest["fingerprint"] = fingerprint_manifest_v2(manifest)
    return manifest


def test_load_manifest_requires_keys(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"fingerprint": "x"}), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest missing required keys"):
        load_manifest(path)


def test_load_manifest_rejects_mismatched_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    manifest = make_manifest()
    manifest["fingerprint"] = "wrong"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint does not match"):
        load_manifest(path)


def test_load_manifest_reports_implicit_legacy_v1(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(make_manifest()), encoding="utf-8")

    loaded = load_manifest(path)

    assert loaded["_fingerprint_compatibility"] == {
        "version": LEGACY_FINGERPRINT_VERSION,
        "state": "legacy_implicit_v1",
        "covered_fields": ["snapshot_payload"],
        "upgrade_required": True,
        "sunset_at": "2026-08-18T00:00:00Z",
    }


def test_legacy_manifest_warns_before_cutoff(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(make_manifest()), encoding="utf-8")
    clock.install(lambda: datetime(2026, 8, 17, 23, 59, tzinfo=UTC))
    try:
        with caplog.at_level(logging.WARNING):
            load_manifest(path)
    finally:
        clock.reset()
    assert "legacy seed fingerprint" in caplog.text
    assert "2026-08-18T00:00:00Z" in caplog.text


def test_legacy_manifest_fails_closed_after_cutoff(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(make_manifest()), encoding="utf-8")
    clock.install(lambda: datetime(2026, 8, 18, 0, 0, tzinfo=UTC))
    try:
        with pytest.raises(ValueError, match="legacy fingerprint compatibility expired"):
            load_manifest(path)
    finally:
        clock.reset()


def test_load_manifest_reports_explicit_legacy_v1(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    manifest = make_manifest()
    manifest["fingerprint_version"] = LEGACY_FINGERPRINT_VERSION
    path.write_text(json.dumps(manifest), encoding="utf-8")

    loaded = load_manifest(path)

    assert loaded["_fingerprint_compatibility"]["state"] == "legacy_explicit_v1"


def test_load_manifest_accepts_current_v2(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(make_v2_manifest()), encoding="utf-8")

    loaded = load_manifest(path)

    assert loaded["_fingerprint_compatibility"] == {
        "version": CURRENT_FINGERPRINT_VERSION,
        "state": "current_v2",
        "covered_fields": [
            "fingerprint_version",
            "snapshot_date",
            "snapshot_payload",
            "baseline_activity",
        ],
        "upgrade_required": False,
    }


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("snapshot_date", "2026-04-15"),
        (
            "baseline_activity",
            {
                "caller": "codex",
                "timestamp": "2026-04-14",
                "project_name": "bridge-baseline-seed",
                "summary": "Tampered after review.",
                "tags": ["BASELINE"],
            },
        ),
    ],
)
def test_load_manifest_v2_rejects_reviewed_field_tampering(
    tmp_path: Path, field: str, replacement: object
) -> None:
    path = tmp_path / "manifest.json"
    manifest = make_v2_manifest()
    manifest[field] = replacement
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest-v2 signed content"):
        load_manifest(path)


@pytest.mark.parametrize("version", ["manifest-v3", 2, {"version": 2}])
def test_load_manifest_rejects_unknown_fingerprint_versions(
    tmp_path: Path, version: object
) -> None:
    path = tmp_path / "manifest.json"
    manifest = make_manifest()
    manifest["fingerprint_version"] = version
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported fingerprint_version"):
        load_manifest(path)


@pytest.mark.asyncio
async def test_apply_manifest_rejects_unknown_version_before_opening_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "bridge.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    manifest = make_manifest()
    manifest["fingerprint_version"] = "manifest-v3"

    with pytest.raises(ValueError, match="unsupported fingerprint_version"):
        await apply_manifest(manifest, dry_run=False)

    assert not db_path.exists()


@pytest.mark.asyncio
async def test_codex_seed_dry_run_reports_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "bridge.db"
    bridge_path = tmp_path / "bridge.md"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge_path)

    db = await open_db(db_path)
    await db.close()

    result = await apply_manifest(make_manifest(), dry_run=True)
    assert result["snapshot_write"] == "would_insert"
    assert result["activity_write"] == "would_insert"
    assert result["fingerprint_compatibility"]["state"] == "legacy_implicit_v1"


@pytest.mark.asyncio
async def test_codex_seed_apply_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "bridge.db"
    bridge_path = tmp_path / "bridge.md"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge_path)

    db = await open_db(db_path)
    await db.close()

    first = await apply_manifest(make_manifest(), dry_run=False)
    second = await apply_manifest(make_manifest(), dry_run=False)

    assert first["snapshot_write"] == "inserted"
    assert first["activity_write"] == "inserted"
    assert second["snapshot_write"] == "skipped_identical"
    assert second["activity_write"] == "skipped_identical"
    assert bridge_path.exists()

    db = await open_db(db_path)
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM system_snapshots WHERE system='codex'")
        snapshot_row = await cursor.fetchone()
        assert snapshot_row is not None
        assert snapshot_row[0] == 1

        cursor = await db.execute(
            "SELECT COUNT(*) FROM activity_log WHERE source='codex' AND project_name='bridge-baseline-seed'"
        )
        activity_row = await cursor.fetchone()
        assert activity_row is not None
        assert activity_row[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_codex_seed_refuses_conflicting_baseline_activity_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "bridge.db"
    bridge_path = tmp_path / "bridge.md"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge_path)

    db = await open_db(db_path)
    await db.close()

    first = await apply_manifest(make_manifest(), dry_run=False)
    second = await apply_manifest(make_variant_manifest(), dry_run=False)

    assert first["activity_write"] == "inserted"
    assert second["ok"] is False
    assert second["activity_write"] == "conflict"
    assert second["snapshot_write"] == "blocked_conflict"

    db = await open_db(db_path)
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM activity_log WHERE source='codex' AND timestamp='2026-04-14' AND project_name='bridge-baseline-seed'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1
        snapshot_cursor = await db.execute(
            "SELECT COUNT(*) FROM system_snapshots WHERE system='codex'"
        )
        snapshot_row = await snapshot_cursor.fetchone()
        assert snapshot_row is not None
        assert snapshot_row[0] == 1
        activity_cursor = await db.execute(
            "SELECT summary FROM activity_log WHERE source='codex' "
            "AND timestamp='2026-04-14' AND project_name='bridge-baseline-seed'"
        )
        activity_row = await activity_cursor.fetchone()
        assert activity_row is not None
        assert activity_row["summary"] == (
            "Seeded Codex baseline from reconciled truth."
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_codex_seed_populates_content_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apply_manifest's direct INSERTs must also populate content_index.

    codex_seed bypasses the tool layer, so it hooks FTS5 inline rather than
    relying on the per-tool helpers. Regression test for that wiring.
    """
    db_path = tmp_path / "bridge.db"
    bridge_path = tmp_path / "bridge.md"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "BRIDGE_FILE_PATH", bridge_path)

    db = await open_db(db_path)
    await db.close()

    result = await apply_manifest(make_manifest(), dry_run=False)
    assert result["snapshot_write"] == "inserted"
    assert result["activity_write"] == "inserted"

    db = await open_db(db_path)
    try:
        cursor = await db.execute(
            "SELECT source_type, COUNT(*) FROM content_index GROUP BY source_type"
        )
        counts = {r[0]: r[1] for r in await cursor.fetchall()}
        assert counts.get("snapshot") == 1, f"expected 1 snapshot FTS row, got {counts}"
        assert counts.get("activity") == 1, f"expected 1 activity FTS row, got {counts}"

        # The manifest's baseline activity mentions "bridge-baseline-seed" in
        # project_name — it should be findable via MATCH.
        cursor = await db.execute(
            "SELECT COUNT(*) FROM content_index WHERE content_index MATCH 'baseline'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] >= 1
    finally:
        await db.close()
