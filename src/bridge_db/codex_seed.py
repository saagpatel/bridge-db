"""Private/manual Codex baseline seed entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from bridge_db import config
from bridge_db.db import (
    fts_text_for_activity,
    fts_text_for_snapshot,
    gc_fts_orphans,
    open_db,
    upsert_fts_entry,
)
from bridge_db.tools.export import ContextExportSnapshot, build_markdown, export_bridge_file

LEGACY_FINGERPRINT_VERSION = "snapshot-v1"
CURRENT_FINGERPRINT_VERSION = "manifest-v2"
_FINGERPRINT_COMPATIBILITY_KEY = "_fingerprint_compatibility"


def load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return _validate_manifest(data)


def _validate_manifest(data: dict[str, Any]) -> dict[str, Any]:
    required = {"fingerprint", "snapshot_date", "snapshot_payload", "baseline_activity"}
    missing = sorted(required.difference(data))
    if missing:
        raise ValueError(f"manifest missing required keys: {', '.join(missing)}")
    version_value = data.get("fingerprint_version")
    implicit_legacy = version_value is None
    if implicit_legacy:
        version = LEGACY_FINGERPRINT_VERSION
    elif not isinstance(version_value, str):
        raise ValueError("unsupported fingerprint_version: expected a string")
    else:
        version = version_value

    if version == LEGACY_FINGERPRINT_VERSION:
        expected_fingerprint = _fingerprint_snapshot(data["snapshot_payload"])
        compatibility_state = (
            "legacy_implicit_v1" if implicit_legacy else "legacy_explicit_v1"
        )
        covered_fields = ["snapshot_payload"]
    elif version == CURRENT_FINGERPRINT_VERSION:
        expected_fingerprint = fingerprint_manifest_v2(data)
        compatibility_state = "current_v2"
        covered_fields = [
            "fingerprint_version",
            "snapshot_date",
            "snapshot_payload",
            "baseline_activity",
        ]
    else:
        raise ValueError(f"unsupported fingerprint_version: {version!r}")

    if data["fingerprint"] != expected_fingerprint:
        raise ValueError(
            f"manifest fingerprint does not match {version} signed content"
        )

    validated = dict(data)
    validated[_FINGERPRINT_COMPATIBILITY_KEY] = {
        "version": version,
        "state": compatibility_state,
        "covered_fields": covered_fields,
        "upgrade_required": version != CURRENT_FINGERPRINT_VERSION,
    }
    return validated


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _fingerprint_snapshot(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def fingerprint_manifest_v2(manifest: dict[str, Any]) -> str:
    """Return the v2 digest over every operator-reviewed seed field."""
    signed_content = {
        "fingerprint_version": CURRENT_FINGERPRINT_VERSION,
        "snapshot_date": manifest["snapshot_date"],
        "snapshot_payload": manifest["snapshot_payload"],
        "baseline_activity": manifest["baseline_activity"],
    }
    return hashlib.sha256(_stable_json(signed_content).encode("utf-8")).hexdigest()


async def _latest_codex_snapshot_payload(db: Any) -> dict[str, Any] | None:
    cursor = await db.execute(
        """
        SELECT data FROM system_snapshots
        WHERE system = 'codex'
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return json.loads(row["data"])


async def _baseline_activity_state(db: Any, entry: dict[str, Any]) -> str:
    cursor = await db.execute(
        """
        SELECT summary, branch, tags, source_trust
        FROM activity_log
        WHERE source = ?
          AND timestamp = ?
          AND project_name = ?
        """,
        (
            entry["caller"],
            entry["timestamp"],
            entry["project_name"],
        ),
    )
    rows = list(await cursor.fetchall())
    if not rows:
        return "missing"
    if len(rows) != 1:
        return "conflict"
    row = rows[0]
    try:
        stored_tags = json.loads(row["tags"])
    except (json.JSONDecodeError, TypeError):
        return "conflict"
    expected_tags = entry.get("tags", [])
    identical = (
        row["summary"] == entry["summary"]
        and row["branch"] == entry.get("branch")
        and stored_tags == expected_tags
        and row["source_trust"] == "agent"
    )
    return "identical" if identical else "conflict"


async def apply_manifest(manifest: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    manifest = _validate_manifest(manifest)
    fingerprint_compatibility = manifest[_FINGERPRINT_COMPATIBILITY_KEY]
    db = await open_db(config.DB_PATH)
    try:
        snapshot_payload = manifest["snapshot_payload"]
        baseline_activity = manifest["baseline_activity"]
        snapshot_write = "skipped_identical"
        activity_write = "skipped_identical"

        activity_state = await _baseline_activity_state(db, baseline_activity)
        if activity_state == "conflict":
            await db.rollback()
            return {
                "ok": False,
                "dry_run": dry_run,
                "snapshot_write": "blocked_conflict",
                "activity_write": "conflict",
                "fingerprint_compatibility": fingerprint_compatibility,
                "bridge_file": str(config.BRIDGE_FILE_PATH),
            }

        current_snapshot = await _latest_codex_snapshot_payload(db)
        if current_snapshot is None or _fingerprint_snapshot(
            current_snapshot
        ) != _fingerprint_snapshot(snapshot_payload):
            snapshot_write = "would_insert" if dry_run else "inserted"
            if not dry_run:
                snapshot_json = json.dumps(snapshot_payload)
                cursor = await db.execute(
                    """
                    INSERT INTO system_snapshots (system, snapshot_date, data)
                    VALUES (?, ?, ?)
                    """,
                    ("codex", manifest["snapshot_date"], snapshot_json),
                )
                snapshot_id = cursor.lastrowid
                if snapshot_id is not None:
                    await upsert_fts_entry(
                        db, "snapshot", str(snapshot_id), fts_text_for_snapshot(snapshot_json)
                    )
                await db.execute(
                    """
                    DELETE FROM system_snapshots
                    WHERE system = ? AND id NOT IN (
                        SELECT id FROM system_snapshots WHERE system = ?
                        ORDER BY created_at DESC, id DESC LIMIT ?
                    )
                    """,
                    ("codex", "codex", config.SNAPSHOT_RETENTION_PER_SYSTEM),
                )
                await gc_fts_orphans(db, "snapshot")

        if activity_state == "missing":
            activity_write = "would_insert" if dry_run else "inserted"
            if not dry_run:
                cursor = await db.execute(
                    """
                    INSERT INTO activity_log (source, timestamp, project_name, summary, branch, tags)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        baseline_activity["caller"],
                        baseline_activity["timestamp"],
                        baseline_activity["project_name"],
                        baseline_activity["summary"],
                        baseline_activity.get("branch"),
                        json.dumps(baseline_activity.get("tags", [])),
                    ),
                )
                activity_id = cursor.lastrowid
                if activity_id is not None:
                    await upsert_fts_entry(
                        db,
                        "activity",
                        str(activity_id),
                        fts_text_for_activity(
                            baseline_activity["project_name"],
                            baseline_activity["summary"],
                            baseline_activity.get("branch"),
                            baseline_activity.get("tags", []),
                        ),
                    )

        if not dry_run and (snapshot_write == "inserted" or activity_write == "inserted"):
            await db.commit()
            context_snapshot: list[ContextExportSnapshot] = []
            content = await build_markdown(db, context_snapshot=context_snapshot)
            await export_bridge_file(
                db,
                content,
                context_snapshot,
                principal="codex_seed",
                trigger="codex_seed",
            )
            await db.commit()
        elif not dry_run:
            await db.rollback()

        return {
            "ok": True,
            "dry_run": dry_run,
            "snapshot_write": snapshot_write,
            "activity_write": activity_write,
            "fingerprint_compatibility": fingerprint_compatibility,
            "bridge_file": str(config.BRIDGE_FILE_PATH),
        }
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bridge_db.codex_seed")
    parser.add_argument(
        "--manifest", required=True, help="Path to the Codex baseline seed manifest JSON."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate and report without mutating the DB."
    )
    parser.add_argument("--apply", action="store_true", help="Apply the baseline seed manifest.")
    args = parser.parse_args()

    if args.dry_run == args.apply:
        raise SystemExit("Choose exactly one of --dry-run or --apply.")

    manifest_path = Path(args.manifest)
    manifest = load_manifest(manifest_path)
    result = asyncio.run(apply_manifest(manifest, dry_run=args.dry_run))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
