"""Entry point: python -m bridge_db [--doctor|--status|--dogfood]"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from typing import Any


async def _run_doctor() -> bool:
    """Run diagnostics and print pass/fail for each check. Returns True if all pass."""
    from datetime import datetime

    from bridge_db import config
    from bridge_db.db import SCHEMA_VERSION, open_db

    checks: list[tuple[str, bool, str]] = []  # (label, passed, detail)

    # 1. DB path exists
    db_exists = config.DB_PATH.exists()
    checks.append(("DB file exists", db_exists, str(config.DB_PATH)))

    # 2. DB opens cleanly
    db = None
    try:
        db = await open_db(config.DB_PATH)
        checks.append(("DB opens (WAL + schema)", True, "ok"))
    except Exception as exc:
        checks.append(("DB opens (WAL + schema)", False, str(exc)))

    # 3. Schema version
    if db is not None:
        try:
            cursor = await db.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            version: int = row[0] if row else 0
            version_ok = version == SCHEMA_VERSION
            checks.append(
                (
                    f"Schema version == {SCHEMA_VERSION}",
                    version_ok,
                    f"found v{version}",
                )
            )
        except Exception as exc:
            checks.append((f"Schema version == {SCHEMA_VERSION}", False, str(exc)))
        finally:
            await db.close()

    # 4. Bridge file
    bridge_exists = config.BRIDGE_FILE_PATH.exists()
    bridge_detail = str(config.BRIDGE_FILE_PATH)
    if bridge_exists:
        mtime = config.BRIDGE_FILE_PATH.stat().st_mtime
        age_h = (datetime.now(UTC).timestamp() - mtime) / 3600
        bridge_detail += f" ({age_h:.1f}h old)"
    checks.append(("Bridge file exists", bridge_exists, bridge_detail))

    # 5. Audit log writable
    try:
        config.AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(config.AUDIT_LOG_PATH, "a", encoding="utf-8"):
            pass
        checks.append(("Audit log writable", True, str(config.AUDIT_LOG_PATH)))
    except Exception as exc:
        checks.append(("Audit log writable", False, str(exc)))

    # Print results
    all_ok = True
    for label, passed, detail in checks:
        icon = "✓" if passed else "✗"
        print(f"  {icon}  {label}: {detail}")
        if not passed:
            all_ok = False

    return all_ok


def _fts_detail(fts_index: dict[str, Any]) -> str:
    return (
        f"expected={fts_index['expected']},"
        f" indexed={fts_index['indexed']},"
        f" missing={fts_index['missing']},"
        f" orphaned={fts_index['orphaned']}"
    )


async def run_status() -> bool:
    """Print a compact operator-facing bridge status summary."""
    from bridge_db import config
    from bridge_db.db import open_db
    from bridge_db.tools.health import collect_status_summary

    db = await open_db(config.DB_PATH)
    try:
        summary = await collect_status_summary(db)
    finally:
        await db.close()

    print("bridge-db status")
    print(f"  Overall: {summary['overall']}")
    print(
        "  DB:"
        f" exists={summary['db']['exists']},"
        f" schema=v{summary['db']['schema_version']}"
        f" (expected v{summary['db']['expected_schema_version']})"
    )
    print(
        "  Bridge file:"
        f" exists={summary['bridge_file']['exists']}, age={summary['bridge_file']['age_human']}"
    )
    print(
        "  Rows:"
        f" contexts={summary['row_counts']['context_sections']},"
        f" activity={summary['row_counts']['activity_log']},"
        f" handoffs={summary['row_counts']['pending_handoffs']},"
        f" snapshots={summary['row_counts']['system_snapshots']},"
        f" costs={summary['row_counts']['cost_records']},"
        f" shipped_receipts={summary['row_counts']['shipped_sync_receipts']}"
    )
    print(
        "  Signals:"
        f" pending_handoffs={summary['signals']['pending_handoffs']},"
        f" unprocessed_shipped={summary['signals']['unprocessed_shipped']},"
        " processed_shipped_without_receipt="
        f"{summary['signals']['processed_shipped_without_receipt']},"
        f" fts_missing={summary['signals']['fts_missing']},"
        f" fts_orphaned={summary['signals']['fts_orphaned']}"
    )
    print(f"  FTS: {_fts_detail(summary['fts_index'])}")
    trust = summary["pending_handoffs_by_trust"]
    print(
        "  Pending handoff trust:"
        f" operator={trust['operator']}, agent={trust['agent']}, ingested={trust['ingested']}"
    )
    attention = _status_attention(summary)
    if attention:
        print(f"  Attention: {attention}")
    print(
        "  Latest snapshots:"
        f" cc={summary['latest_snapshots']['cc']}, codex={summary['latest_snapshots']['codex']}"
    )
    print(f"  Latest activity: {summary['latest_activity_json']}")

    return bool(summary["ok"])


def _status_attention(summary: dict[str, Any]) -> str | None:
    """Return a short operator hint for status signals that need follow-up."""
    notes: list[str] = []
    if not summary["ok"]:
        notes.append("bridge health is degraded")
    signals = summary["signals"]
    if signals["pending_handoffs"]:
        notes.append(f"pending_handoffs={signals['pending_handoffs']}")
    if signals["unprocessed_shipped"]:
        notes.append(f"unprocessed_shipped={signals['unprocessed_shipped']}")
    if signals["processed_shipped_without_receipt"]:
        notes.append(
            f"processed_shipped_without_receipt={signals['processed_shipped_without_receipt']}"
        )
    if signals["fts_missing"]:
        notes.append(f"fts_missing={signals['fts_missing']}")
    if signals["fts_orphaned"]:
        notes.append(f"fts_orphaned={signals['fts_orphaned']}")
    if not notes:
        return None
    return "; ".join(notes) + " — dogfood will fail until cleared"


def _latest_detail(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "none"
    detail = rows[0].get("detail")
    return str(detail) if detail is not None else "none"


def _has_detailed_mark_audit(rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return True
    detail = str(rows[0].get("detail") or "")
    return all(token in detail for token in ("activity_ids=", "updated_ids=", "missing_ids="))


async def run_dogfood() -> bool:
    """Run the read-only bridge observability dogfood checklist."""
    from bridge_db import config
    from bridge_db.db import open_db
    from bridge_db.tools.audit import collect_audit_tail
    from bridge_db.tools.health import collect_health_metrics, collect_status_summary
    from bridge_db.tools.recall import collect_recall_stats

    db = await open_db(config.DB_PATH)
    try:
        summary = await collect_status_summary(db)
        health = await collect_health_metrics(db)
    finally:
        await db.close()

    recent_audit = collect_audit_tail(limit=10)
    recent_confirm = collect_audit_tail(tool="confirm_shipped_sync", limit=10)
    recent_mark = collect_audit_tail(tool="mark_shipped_processed", limit=10)
    recall = collect_recall_stats(days=7)
    mark_detail_is_current = _has_detailed_mark_audit(recent_mark)

    print("bridge-db dogfood")
    print(f"  Overall: {summary['overall']}")
    print(
        "  Signals:"
        f" pending_handoffs={summary['signals']['pending_handoffs']},"
        f" unprocessed_shipped={summary['signals']['unprocessed_shipped']},"
        " processed_shipped_without_receipt="
        f"{summary['signals']['processed_shipped_without_receipt']}"
    )
    print(f"  WAL: size_bytes={health['wal_size_bytes']}, warning={health['wal_warning']}")
    print(f"  FTS: {_fts_detail(health['fts_index'])}")
    print(
        "  Recall:"
        f" queries_7d={recall['total_queries']},"
        f" miss_rate={recall['miss_rate']},"
        f" scopes={recall['scope_breakdown']}"
    )
    print(f"  Recent audit rows checked: {len(recent_audit)}")
    print(f"  Latest confirm_shipped_sync: {_latest_detail(recent_confirm)}")
    print(f"  Latest mark_shipped_processed: {_latest_detail(recent_mark)}")
    print(
        "  Compatibility audit detail:"
        f" {'current' if mark_detail_is_current else 'legacy terse detail observed'}"
    )

    return bool(
        summary["ok"]
        and summary["signals"]["pending_handoffs"] == 0
        and summary["signals"]["unprocessed_shipped"] == 0
        and summary["signals"]["processed_shipped_without_receipt"] == 0
        and health["fts_index"]["ok"]
        and not health["wal_warning"]
    )


async def run_rebuild_content_index() -> bool:
    """Rebuild FTS content_index and verify it matches source tables."""
    from bridge_db import config
    from bridge_db.db import collect_fts_index_metrics, open_db, repopulate_content_index

    db = await open_db(config.DB_PATH)
    try:
        counts = await repopulate_content_index(db)
        metrics = await collect_fts_index_metrics(db)
    finally:
        await db.close()

    print("bridge-db content_index rebuild")
    print(
        "  Rebuilt:"
        f" sections={counts['section']},"
        f" activity={counts['activity']},"
        f" snapshots={counts['snapshot']},"
        f" handoffs={counts['handoff']}"
    )
    print(f"  FTS: {_fts_detail(metrics)}")
    print(f"  Overall: {'healthy' if metrics['ok'] else 'degraded'}")
    return bool(metrics["ok"])


async def run_log_session_boundary(
    project_name: str, duration_minutes: str | None = None, timestamp: str | None = None
) -> bool:
    """Log a Claude Code session boundary through the normal activity + FTS path."""
    from bridge_db import config
    from bridge_db.audit import log_audit
    from bridge_db.db import collect_fts_index_metrics, insert_activity_row, open_db

    ts = timestamp or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = "CC session ended"
    if duration_minutes:
        summary = f"CC session ended ({duration_minutes}min)"

    db = await open_db(config.DB_PATH)
    try:
        activity_id = await insert_activity_row(
            db,
            source="cc",
            timestamp=ts,
            project_name=project_name,
            summary=summary,
            tags=["session-boundary"],
        )
        await db.commit()
        metrics = await collect_fts_index_metrics(db)
    finally:
        await db.close()

    log_audit(
        "log_session_boundary",
        "cc",
        project_name,
        ok=bool(metrics["ok"]),
        detail=f"activity_id={activity_id} fts_missing={metrics['missing']}",
    )
    print("bridge-db session boundary")
    print(f"  Logged: activity_id={activity_id}, project={project_name}")
    print(f"  FTS: {_fts_detail(metrics)}")
    print(f"  Overall: {'healthy' if metrics['ok'] else 'degraded'}")
    return bool(metrics["ok"])


def main() -> None:
    parser = argparse.ArgumentParser(prog="bridge-db")
    parser.add_argument("--doctor", action="store_true", help="Run diagnostics and exit")
    parser.add_argument("--status", action="store_true", help="Print a compact bridge summary")
    parser.add_argument(
        "--dogfood",
        action="store_true",
        help="Run the read-only bridge observability dogfood checklist",
    )
    parser.add_argument(
        "--rebuild-content-index",
        action="store_true",
        help="Rebuild the FTS content_index from source tables and verify it",
    )
    parser.add_argument(
        "--log-session-boundary",
        metavar="PROJECT_NAME",
        help="Log a Claude Code session-boundary activity entry through the FTS-safe path",
    )
    parser.add_argument(
        "--duration-minutes",
        help="Optional duration value for --log-session-boundary",
    )
    args, _ = parser.parse_known_args()

    if args.doctor:
        ok = asyncio.run(_run_doctor())
        sys.exit(0 if ok else 1)
    if args.status:
        ok = asyncio.run(run_status())
        sys.exit(0 if ok else 1)
    if args.dogfood:
        ok = asyncio.run(run_dogfood())
        sys.exit(0 if ok else 1)
    if args.rebuild_content_index:
        ok = asyncio.run(run_rebuild_content_index())
        sys.exit(0 if ok else 1)
    if args.log_session_boundary:
        ok = asyncio.run(run_log_session_boundary(args.log_session_boundary, args.duration_minutes))
        sys.exit(0 if ok else 1)

    from bridge_db.server import mcp

    mcp.run()


if __name__ == "__main__":
    main()
