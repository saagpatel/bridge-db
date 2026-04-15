"""Entry point: python -m bridge_db [--doctor]"""

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


def main() -> None:
    parser = argparse.ArgumentParser(prog="bridge-db")
    parser.add_argument("--doctor", action="store_true", help="Run diagnostics and exit")
    args, _ = parser.parse_known_args()

    if args.doctor:
        ok = asyncio.run(_run_doctor())
        sys.exit(0 if ok else 1)

    from bridge_db.server import mcp

    mcp.run()


if __name__ == "__main__":
    main()
