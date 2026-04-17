"""Entry point: python -m bridge_db [--doctor|--status]"""

import argparse
import asyncio
import sys
from datetime import UTC


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
        f" costs={summary['row_counts']['cost_records']}"
    )
    print(
        "  Signals:"
        f" pending_handoffs={summary['signals']['pending_handoffs']},"
        f" unprocessed_shipped={summary['signals']['unprocessed_shipped']}"
    )
    print(
        "  Latest snapshots:"
        f" cc={summary['latest_snapshots']['cc']}, codex={summary['latest_snapshots']['codex']}"
    )
    print(f"  Latest activity: {summary['latest_activity_json']}")

    return bool(summary["ok"])


def main() -> None:
    parser = argparse.ArgumentParser(prog="bridge-db")
    parser.add_argument("--doctor", action="store_true", help="Run diagnostics and exit")
    parser.add_argument("--status", action="store_true", help="Print a compact bridge summary")
    args, _ = parser.parse_known_args()

    if args.doctor:
        ok = asyncio.run(_run_doctor())
        sys.exit(0 if ok else 1)
    if args.status:
        ok = asyncio.run(run_status())
        sys.exit(0 if ok else 1)

    from bridge_db.server import mcp

    mcp.run()


if __name__ == "__main__":
    main()