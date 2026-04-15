"""Private/manual Codex baseline seed entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from bridge_db import config
from bridge_db.db import open_db
from bridge_db.tools.export import build_markdown


def load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    required = {"fingerprint", "snapshot_date", "snapshot_payload", "baseline_activity"}
    missing = sorted(required.difference(data))
    if missing:
        raise ValueError(f"manifest missing required keys: {', '.join(missing)}")
    return data


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _fingerprint_snapshot(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


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


async def _baseline_activity_exists(db: Any, entry: dict[str, Any]) -> bool:
    cursor = await db.execute(
        """
        SELECT 1
        FROM activity_log
        WHERE source = ?
          AND timestamp = ?
          AND project_name = ?
        LIMIT 1
        """,
        (
          entry["caller"],
          entry["timestamp"],
          entry["project_name"],
        ),
    )
    return await cursor.fetchone() is not None


async def apply_manifest(manifest: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    db = await open_db(config.DB_PATH)
    try:
        snapshot_payload = manifest["snapshot_payload"]
        baseline_activity = manifest["baseline_activity"]
        snapshot_write = "skipped_identical"
        activity_write = "skipped_duplicate"

        current_snapshot = await _latest_codex_snapshot_payload(db)
        if current_snapshot is None or _fingerprint_snapshot(current_snapshot) != _fingerprint_snapshot(
            snapshot_payload
        ):
            snapshot_write = "would_insert" if dry_run else "inserted"
            if not dry_run:
                await db.execute(
                    """
                    INSERT INTO system_snapshots (system, snapshot_date, data)
                    VALUES (?, ?, ?)
                    """,
                    ("codex", manifest["snapshot_date"], json.dumps(snapshot_payload)),
                )
                await db.execute(
                    """
                    DELETE FROM system_snapshots
                    WHERE system = ? AND id NOT IN (
                        SELECT id FROM system_snapshots WHERE system = ?
                        ORDER BY created_at DESC LIMIT ?
                    )
                    """,
                    ("codex", "codex", config.SNAPSHOT_RETENTION_PER_SYSTEM),
                )

        activity_exists = await _baseline_activity_exists(db, baseline_activity)
        if not activity_exists:
            activity_write = "would_insert" if dry_run else "inserted"
            if not dry_run:
                await db.execute(
                    """
                    INSERT INTO activity_log (source, timestamp, project_name, summary, branch, tags)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        baseline_activity["caller"],
                        baseline_activity["timestamp"],
                        baseline_activity["project_name"],
                        baseline_activity["summary"],
                        None,
                        json.dumps(baseline_activity.get("tags", [])),
                    ),
                )

        if not dry_run and (snapshot_write == "inserted" or activity_write == "inserted"):
            await db.commit()
            content = await build_markdown(db)
            config.BRIDGE_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            config.BRIDGE_FILE_PATH.write_text(content, encoding="utf-8")
        elif not dry_run:
            await db.rollback()

        return {
            "ok": True,
            "dry_run": dry_run,
            "snapshot_write": snapshot_write,
            "activity_write": activity_write,
            "bridge_file": str(config.BRIDGE_FILE_PATH),
        }
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bridge_db.codex_seed")
    parser.add_argument("--manifest", required=True, help="Path to the Codex baseline seed manifest JSON.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and report without mutating the DB.")
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
