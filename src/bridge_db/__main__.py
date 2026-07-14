"""Entry point: python -m bridge_db [--doctor|--status|--dogfood]"""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys
import tempfile
from datetime import datetime
from typing import Any, cast

from bridge_db import clock


async def _run_doctor() -> bool:
    """Run diagnostics and print pass/fail for each check. Returns True if all pass."""

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
        age_h = (clock.now().timestamp() - mtime) / 3600
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


async def run_status(*, now: datetime | None = None) -> bool:
    """Print a compact operator-facing bridge status summary."""
    from bridge_db import config
    from bridge_db.db import open_db
    from bridge_db.tools.health import collect_status_summary

    db = await open_db(config.DB_PATH)
    try:
        summary = await collect_status_summary(db, now=now)
    finally:
        await db.close()

    print("bridge-db status")
    print(f"  Storage health: {summary['storage_health']}")
    print(f"  Operating state: {summary['operating_state']}")
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
        f" synced_shipped={summary['signals']['synced_shipped']}"
    )
    print(
        "  Signals:"
        f" pending_handoffs={summary['signals']['pending_handoffs']},"
        f" unprocessed_shipped={summary['signals']['unprocessed_shipped']},"
        f" actionable_unprocessed_shipped={summary['signals']['actionable_unprocessed_shipped']},"
        " dispositioned_unprocessed_shipped="
        f"{summary['signals']['dispositioned_unprocessed_shipped']},"
        " processed_shipped_without_receipt="
        f"{summary['signals']['processed_shipped_without_receipt']},"
        f" fts_missing={summary['signals']['fts_missing']},"
        f" fts_orphaned={summary['signals']['fts_orphaned']},"
        f" open_write_conflicts={summary['signals']['open_write_conflicts']}"
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
    freshness_lines = _status_freshness_lines(summary)
    for line in freshness_lines:
        print(f"  {line}")
    print(
        "  Latest snapshots:"
        f" cc={summary['latest_snapshots']['cc']}, codex={summary['latest_snapshots']['codex']}"
    )
    print(f"  Latest activity: {summary['latest_activity_json']}")

    return bool(summary["ok"])


def _status_freshness_lines(summary: dict[str, Any]) -> list[str]:
    """Return compact CLI freshness hints without dumping raw status JSON."""
    freshness = summary.get("freshness")
    if not isinstance(freshness, dict):
        return []
    freshness_block = cast(dict[str, Any], freshness)

    overall = str(freshness_block.get("overall", "unknown"))
    lines = [f"Freshness: {overall}"]
    actions_raw = freshness_block.get("next_actions")
    if not isinstance(actions_raw, list):
        return lines
    actions = cast(list[object], actions_raw)

    action_labels: list[str] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_block = cast(dict[str, Any], action)
        action_name = action_block.get("action")
        if not isinstance(action_name, str):
            continue
        owner = action_block.get("owner")
        if isinstance(owner, str) and owner:
            action_labels.append(f"{action_name} ({owner})")
        else:
            action_labels.append(action_name)

    if action_labels:
        lines.append(f"Next actions: {', '.join(action_labels)}")
    return lines


def _status_attention(summary: dict[str, Any]) -> str | None:
    """Return a short operator hint for status signals that need follow-up."""
    notes: list[str] = []
    if not summary["ok"]:
        notes.append("bridge health is degraded")
    signals = summary["signals"]
    if signals["pending_handoffs"]:
        notes.append(f"pending_handoffs={signals['pending_handoffs']}")
    if signals["actionable_unprocessed_shipped"]:
        notes.append(
            f"actionable_unprocessed_shipped={signals['actionable_unprocessed_shipped']}"
        )
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
    recent_disposition = collect_audit_tail(tool="record_disposition", limit=10)
    recall = collect_recall_stats(days=7)

    print("bridge-db dogfood")
    print(f"  Storage health: {summary['storage_health']}")
    print(f"  Operating state: {summary['operating_state']}")
    print(
        "  Signals:"
        f" pending_handoffs={summary['signals']['pending_handoffs']},"
        f" unprocessed_shipped={summary['signals']['unprocessed_shipped']},"
        f" actionable_unprocessed_shipped={summary['signals']['actionable_unprocessed_shipped']},"
        " dispositioned_unprocessed_shipped="
        f"{summary['signals']['dispositioned_unprocessed_shipped']},"
        " processed_shipped_without_receipt="
        f"{summary['signals']['processed_shipped_without_receipt']}"
    )
    print(
        f"  WAL: size_bytes={health['wal_size_bytes']}, warning={health['wal_warning']}"
    )
    print(
        f"  Ledger: protected={health['ledger_protected_count']} "
        f"receipt_orphans={health['receipt_orphan_count']} "
        f"disposition_orphans={health['disposition_orphan_count']}"
    )
    # Informational, never a dogfood gate: open receipts are the conflict
    # machinery doing its job; the operator action is to read them, not to
    # treat the bridge as failing.
    if health["open_write_conflicts"]:
        print(
            f"  Write conflicts: open={health['open_write_conflicts']},"
            f" oldest_age_hours={health['oldest_open_conflict_age_hours']}"
            ' (inspect via get_write_conflicts(status="open"))'
        )
    else:
        print("  Write conflicts: open=0")
    print(f"  FTS: {_fts_detail(health['fts_index'])}")
    print(
        "  Recall:"
        f" queries_7d={recall['total_queries']},"
        f" miss_rate={recall['miss_rate']},"
        f" scopes={recall['scope_breakdown']}"
    )
    print(f"  Recent audit rows checked: {len(recent_audit)}")
    print(f"  Latest record_disposition: {_latest_detail(recent_disposition)}")

    return bool(
        summary["ok"]
        and summary["signals"]["pending_handoffs"] == 0
        and summary["signals"]["actionable_unprocessed_shipped"] == 0
        and summary["signals"]["processed_shipped_without_receipt"] == 0
        and health["fts_index"]["ok"]
        and not health["wal_warning"]
        and health["receipt_orphan_count"] == 0
        and health["disposition_orphan_count"] == 0
    )


async def run_rebuild_content_index() -> bool:
    """Rebuild FTS content_index and verify it matches source tables."""
    from bridge_db import config
    from bridge_db.db import (
        collect_fts_index_metrics,
        open_db,
        repopulate_content_index,
    )

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


async def run_reconcile_canonical_keys() -> bool:
    """Reconcile stored canonical_key values to GHRA repo_full_name values."""
    from bridge_db import config
    from bridge_db.canonical_reconcile import reconcile_canonical_keys
    from bridge_db.db import open_db

    db = await open_db(config.DB_PATH)
    try:
        result = await reconcile_canonical_keys(db)
    finally:
        await db.close()

    print("bridge-db canonical_key reconcile")
    print(f"  Registry: {'present' if result.registry_present else 'missing'}")
    print(
        "  Rows:"
        f" checked={result.rows_checked},"
        f" updated={result.rows_updated},"
        f" disagreements_resolved={result.disagreements_resolved},"
        f" unresolvable_rows={result.unresolvable_rows},"
        f" unresolvable_nulled={result.unresolvable_nulled}"
    )
    for table, counts in result.table_counts.items():
        print(
            f"  {table}:"
            f" checked={counts['rows_checked']},"
            f" updated={counts['rows_updated']},"
            f" disagreements_resolved={counts['disagreements_resolved']},"
            f" unresolvable_nulled={counts['unresolvable_nulled']}"
        )
    print(f"  Overall: {'reconciled' if result.registry_present else 'blocked'}")
    return result.registry_present


async def run_checkpoint() -> bool:
    """Force a WAL checkpoint to bound -wal growth (register #3 / FMEA 1.3)."""
    from bridge_db import config
    from bridge_db.db import checkpoint_wal, open_db

    db = await open_db(config.DB_PATH)
    try:
        result = await checkpoint_wal(db)
    finally:
        await db.close()

    print("bridge-db WAL checkpoint (TRUNCATE)")
    print(
        f"  busy={result['busy']} log_frames={result['log_frames']} "
        f"checkpointed={result['checkpointed']}"
    )
    ok = result["busy"] == 0
    print(
        f"  Overall: {'truncated' if ok else 'partial (readers active — retry when idle)'}"
    )
    return ok


async def run_log_session_boundary(
    project_name: str, duration_minutes: str | None = None, timestamp: str | None = None
) -> bool:
    """Log a Claude Code session boundary through the normal activity + FTS path."""
    from bridge_db import config
    from bridge_db.audit import log_audit
    from bridge_db.db import collect_fts_index_metrics, insert_activity_row, open_db

    ts = timestamp or clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = "CC session ended"
    if duration_minutes:
        summary = f"CC session ended ({duration_minutes}min)"

    db = await open_db(config.DB_PATH)
    try:
        insert_result = await insert_activity_row(
            db,
            source="cc",
            timestamp=ts,
            project_name=project_name,
            summary=summary,
            tags=["session-boundary"],
        )
        activity_id = insert_result.activity_id
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


def _require_tty(action: str) -> bool:
    """Operator ceremonies require an interactive terminal. Agents run non-TTY."""
    if sys.stdin.isatty():
        return True
    print(
        f"refused: --{action} is an operator ceremony and requires an interactive TTY"
    )
    return False


def _read_principals_file() -> dict[str, Any]:
    from bridge_db import config

    if config.PRINCIPALS_PATH.exists():
        try:
            data = json.loads(config.PRINCIPALS_PATH.read_text(encoding="utf-8"))
            if isinstance(data.get("principals"), dict):
                return data
        except json.JSONDecodeError:
            print(
                f"warning: malformed principals file at {config.PRINCIPALS_PATH}, rewriting"
            )
    return {"version": 1, "principals": {}}


def _write_principals_file(data: dict[str, Any]) -> None:
    from bridge_db import config

    config.PRINCIPALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 0600 from creation + atomic replace: no window where the file is
    # world-readable or partially written.
    fd, tmp = tempfile.mkstemp(dir=config.PRINCIPALS_PATH.parent)
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, config.PRINCIPALS_PATH)
    except Exception:
        os.unlink(tmp)
        raise


def run_enroll(caller: str) -> bool:
    """Generate a token for one caller, store its hash, print the token once."""
    from bridge_db.audit import log_audit
    from bridge_db.auth import hash_token
    from bridge_db.models import CALLER_IDS

    if caller not in CALLER_IDS:
        print(f"refused: unknown caller '{caller}'. Known: {', '.join(CALLER_IDS)}")
        return False
    if not _require_tty("enroll"):
        return False

    token = secrets.token_urlsafe(32)
    data = _read_principals_file()
    rotated = caller in data["principals"]
    data["principals"][caller] = {
        "token_sha256": hash_token(token),
        "enrolled_at": clock.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_principals_file(data)
    log_audit("auth.enroll", caller, None, ok=True, detail=f"rotated={rotated}")

    print(
        f"bridge-db enrollment — principal '{caller}' {'rotated' if rotated else 'enrolled'}"
    )
    print("  Set this token in the client's MCP spawn env (shown once, not stored):")
    print(f"  token: {token}")
    print("  env:   BRIDGE_DB_PRINCIPAL_TOKEN")
    return True


def run_revoke_principal(caller: str) -> bool:
    from bridge_db.audit import log_audit

    if not _require_tty("revoke-principal"):
        return False
    data = _read_principals_file()
    if caller not in data["principals"]:
        print(f"no enrollment found for '{caller}'")
        return False
    del data["principals"][caller]
    _write_principals_file(data)
    log_audit("auth.revoke", caller, None, ok=True, detail=None)
    print(f"revoked '{caller}' — its connections bind as unbound on next spawn")
    return True


def run_list_principals() -> bool:
    data = _read_principals_file()
    if not data["principals"]:
        print("no principals enrolled")
        return True
    print("enrolled principals")
    for caller, entry in sorted(data["principals"].items()):
        print(
            f"  {caller}: enrolled_at={entry.get('enrolled_at', '?')}, "
            f"hash={str(entry.get('token_sha256', ''))[:8]}…"
        )
    return True


async def run_promote_section(section_name: str) -> bool:
    """Operator-only label promotion for a context section (TTY-gated)."""
    from bridge_db import config
    from bridge_db.audit import log_audit
    from bridge_db.db import open_db
    from bridge_db.models import SECTION_OWNERS

    if section_name not in SECTION_OWNERS:
        print(
            f"refused: unknown section '{section_name}'. Known: {sorted(SECTION_OWNERS)}"
        )
        return False
    if not _require_tty("promote-section"):
        return False

    db = await open_db(config.DB_PATH)
    try:
        cursor = await db.execute(
            "SELECT source_trust, updated_at FROM context_sections WHERE section_name = ?",
            (section_name,),
        )
        row = await cursor.fetchone()
        if row is None:
            print(f"no stored section '{section_name}'")
            return False
        await db.execute(
            "UPDATE context_sections SET source_trust = 'operator' WHERE section_name = ?",
            (section_name,),
        )
        await db.commit()
    finally:
        await db.close()

    log_audit(
        "auth.promote_section",
        "operator-cli",
        None,
        ok=True,
        detail=f"section={section_name} {row['source_trust']}->operator",
    )
    print(
        f"promoted '{section_name}': {row['source_trust']} -> operator "
        f"(content as of {row['updated_at']})"
    )
    return True


_HANDOFF_PROMOTION_FIELDS = (
    "id",
    "project_name",
    "project_path",
    "roadmap_file",
    "phase",
    "dispatched_from",
    "dispatched_at",
    "picked_up_at",
    "cleared_at",
    "canonical_key",
    "source_trust",
    "status",
    "claimed_by",
)


def _handoff_promotion_digest(row: Any) -> str:
    """Bind an operator review to the exact security-relevant handoff state."""
    payload = {field: row[field] for field in _HANDOFF_PROMOTION_FIELDS}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


async def run_promote_handoff(handoff_id: int) -> bool:
    """Atomically promote one reviewed pending handoff (operator TTY only)."""
    from bridge_db import config
    from bridge_db.audit import log_audit
    from bridge_db.db import open_db

    if handoff_id <= 0:
        print("refused: handoff id must be a positive integer")
        return False
    if not _require_tty("promote-handoff"):
        return False

    db = await open_db(config.DB_PATH)
    promoted_from: str | None = None
    reviewed_digest: str | None = None
    project_name: str | None = None
    try:
        cursor = await db.execute(
            "SELECT id, project_name, project_path, roadmap_file, phase, "
            "dispatched_from, dispatched_at, picked_up_at, cleared_at, "
            "canonical_key, source_trust, status, claimed_by "
            "FROM pending_handoffs WHERE id = ?",
            (handoff_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            print(f"no stored handoff {handoff_id}")
            return False
        if row["status"] != "pending":
            print(
                f"refused: handoff {handoff_id} is {row['status']}, not pending"
            )
            return False
        if row["source_trust"] == "operator":
            print(f"handoff {handoff_id} is already operator-trusted")
            return True

        reviewed_digest = _handoff_promotion_digest(row)
        project_name = cast(str, row["project_name"])
        print(
            "review handoff "
            f"id={handoff_id} project={json.dumps(project_name)} "
            f"phase={json.dumps(row['phase'])} source_trust={row['source_trust']} "
            f"sha256={reviewed_digest}"
        )
        try:
            confirmed = input("Promote this exact pending handoff to operator trust? [y/N] ")
        except EOFError:
            confirmed = ""
        if confirmed.strip().lower() not in {"y", "yes"}:
            print("promotion cancelled")
            return False

        # Re-read under a write lock and compare the complete reviewed state so
        # the operator cannot approve one handoff image and promote another.
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT id, project_name, project_path, roadmap_file, phase, "
            "dispatched_from, dispatched_at, picked_up_at, cleared_at, "
            "canonical_key, source_trust, status, claimed_by "
            "FROM pending_handoffs WHERE id = ?",
            (handoff_id,),
        )
        current = await cursor.fetchone()
        if (
            current is None
            or current["status"] != "pending"
            or _handoff_promotion_digest(current) != reviewed_digest
        ):
            await db.rollback()
            print("refused: handoff changed after review; inspect it again")
            return False

        promoted_from = cast(str, current["source_trust"])
        cursor = await db.execute(
            "UPDATE pending_handoffs SET source_trust = 'operator' "
            "WHERE id = ? AND status = 'pending' AND source_trust = ?",
            (handoff_id, promoted_from),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            print("refused: handoff changed before promotion")
            return False
        await db.commit()
    finally:
        await db.close()

    log_audit(
        "auth.promote_handoff",
        "operator-cli",
        project_name,
        ok=True,
        detail=(
            f"handoff_id={handoff_id} {promoted_from}->operator "
            f"sha256={reviewed_digest}"
        ),
    )
    print(
        f"promoted handoff {handoff_id}: {promoted_from} -> operator "
        f"(sha256={reviewed_digest})"
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(prog="bridge-db")
    parser.add_argument(
        "--doctor", action="store_true", help="Run diagnostics and exit"
    )
    parser.add_argument(
        "--status", action="store_true", help="Print a compact bridge summary"
    )
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
        "--reconcile-canonical-keys",
        action="store_true",
        help="Backfill activity/handoff canonical_key values from the GHRA registry",
    )
    parser.add_argument(
        "--checkpoint",
        action="store_true",
        help="Force a WAL checkpoint (TRUNCATE) to bound -wal growth",
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
    parser.add_argument(
        "--enroll",
        metavar="CALLER",
        help="Enroll a principal: generate a token, store its hash (operator TTY only)",
    )
    parser.add_argument(
        "--revoke-principal",
        metavar="CALLER",
        help="Remove a principal's enrollment (operator TTY only)",
    )
    parser.add_argument(
        "--list-principals", action="store_true", help="List enrolled principals"
    )
    parser.add_argument(
        "--promote-section",
        metavar="SECTION",
        help="Set a context section's source_trust to 'operator' (operator TTY only)",
    )
    parser.add_argument(
        "--promote-handoff",
        metavar="ID",
        type=int,
        help="Review and promote a pending handoff to operator trust (operator TTY only)",
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
    if args.reconcile_canonical_keys:
        ok = asyncio.run(run_reconcile_canonical_keys())
        sys.exit(0 if ok else 1)
    if args.checkpoint:
        ok = asyncio.run(run_checkpoint())
        sys.exit(0 if ok else 1)
    if args.log_session_boundary:
        ok = asyncio.run(
            run_log_session_boundary(args.log_session_boundary, args.duration_minutes)
        )
        sys.exit(0 if ok else 1)
    if args.enroll:
        sys.exit(0 if run_enroll(args.enroll) else 1)
    if args.revoke_principal:
        sys.exit(0 if run_revoke_principal(args.revoke_principal) else 1)
    if args.list_principals:
        sys.exit(0 if run_list_principals() else 1)
    if args.promote_section:
        sys.exit(0 if asyncio.run(run_promote_section(args.promote_section)) else 1)
    if args.promote_handoff is not None:
        sys.exit(0 if asyncio.run(run_promote_handoff(args.promote_handoff)) else 1)

    from bridge_db.server import mcp

    mcp.run()


if __name__ == "__main__":
    main()
