"""Entry point: python -m bridge_db [--doctor|--status|--dogfood]"""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys
import tempfile
from datetime import UTC, datetime, timedelta
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

    # 5. Recovery readiness and historical provenance are distinct. A verified
    # current anchor can establish present readiness without blessing legacy
    # backups that lack creation-time metadata.
    try:
        from bridge_db.evidence import migration_backup_inventory
        from bridge_db.recovery import recovery_anchor_inventory

        legacy = migration_backup_inventory(config.DB_PATH)
        current = recovery_anchor_inventory(
            config.DB_PATH,
            expected_schema_version=SCHEMA_VERSION,
        )
        legacy_ok = legacy["count"] == legacy["verified_count"]
        recovery_ok = current["ready"] or (current["state"] == "missing" and legacy_ok)
        checks.append(
            (
                "Recovery integrity",
                recovery_ok,
                f"current={current['state']}, "
                f"legacy_provenance={legacy['provenance_state']}",
            )
        )
    except Exception as exc:
        checks.append(("Recovery integrity", False, str(exc)))

    # 6. Audit log writable
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
    print(f"  Projection health: {summary['projection_health']}")
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
        f" open_write_conflicts={summary['signals']['open_write_conflicts']},"
        f" audit_degraded={summary['signals']['audit_degraded']},"
        " evidence_disposition_degraded="
        f"{summary['signals']['evidence_disposition_degraded']},"
        " migration_backup_integrity_ok="
        f"{summary['signals']['migration_backup_integrity_ok']},"
        " current_recovery_anchor_ready="
        f"{summary['signals']['current_recovery_anchor_ready']},"
        " legacy_backup_provenance="
        f"{summary['signals']['legacy_backup_provenance_state']}"
    )
    print(
        "  Recovery:"
        f" current_anchor={summary['signals']['current_recovery_anchor_state']},"
        f" legacy_backups={summary['evidence_lifecycle']['migration_backups']['count']},"
        " legacy_provenance="
        f"{summary['signals']['legacy_backup_provenance_state']}"
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
    if signals["audit_degraded"]:
        notes.append("audit_degraded=true")
    if signals["evidence_disposition_degraded"]:
        notes.append("evidence_disposition_degraded=true")
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
    print(f"  Projection health: {summary['projection_health']}")
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
    evidence = health["evidence_lifecycle"]
    print(
        "  Evidence lifecycle:"
        f" audit_bytes={evidence['audit']['total_bytes']},"
        f" audit_segments={evidence['audit']['segment_count']},"
        f" recall_bytes={evidence['recall']['total_bytes']},"
        f" recall_segments={evidence['recall']['segment_count']},"
        f" audit_degraded={evidence['audit_degraded']},"
        f" disposition_degraded={evidence['disposition_degraded']},"
        f" current_recovery_anchor={evidence['current_recovery_anchor']['state']},"
        f" migration_backups={evidence['migration_backups']['count']},"
        f" verified_backups={evidence['migration_backups']['verified_count']},"
        " legacy_provenance="
        f"{evidence['migration_backups']['provenance_state']}"
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
        and not health["evidence_lifecycle"]["audit_degraded"]
        and not health["evidence_lifecycle"]["disposition_degraded"]
        and health["evidence_lifecycle"]["recovery_integrity_ok"]
    )


def run_create_recovery_anchor() -> bool:
    """Create exactly one current anchor, or verify the one already present."""
    from bridge_db import config
    from bridge_db.db import SCHEMA_VERSION
    from bridge_db.recovery import (
        create_recovery_anchor,
        recovery_anchor_inventory,
    )

    try:
        result = create_recovery_anchor(
            config.DB_PATH,
            expected_schema_version=SCHEMA_VERSION,
        )
        disposition = "created"
    except FileExistsError:
        result = recovery_anchor_inventory(
            config.DB_PATH,
            expected_schema_version=SCHEMA_VERSION,
        )
        disposition = "preserved_existing"
    except (OSError, RuntimeError) as exc:
        print(f"recovery anchor creation refused: {exc}")
        return False

    print("bridge-db RecoveryAnchorV1")
    print(f"  Result: {disposition}")
    print(f"  State: {result['state']}")
    print(f"  Path: {result['path']}")
    print(f"  Schema: v{result.get('schema_version')}")
    print(f"  Bytes: {result.get('backup_bytes')}")
    print(f"  Digest verified: {result.get('digest_ok')}")
    print(f"  SQLite integrity: {result.get('integrity_ok')}")
    print(f"  Semantic readback: {result.get('semantic_readback_ok')}")
    if result["errors"]:
        print(f"  Errors: {','.join(result['errors'])}")
    return bool(result["ready"])


def run_verify_recovery_anchor() -> bool:
    """Read-verify the current recovery anchor without changing it."""
    from bridge_db import config
    from bridge_db.db import SCHEMA_VERSION
    from bridge_db.recovery import recovery_anchor_inventory

    result = recovery_anchor_inventory(
        config.DB_PATH,
        expected_schema_version=SCHEMA_VERSION,
    )
    print("bridge-db RecoveryAnchorV1 verification")
    print(f"  State: {result['state']}")
    print(f"  Path: {result['path']}")
    print(f"  Schema: v{result.get('schema_version')}")
    print(f"  Digest verified: {result.get('digest_ok')}")
    print(f"  SQLite integrity: {result.get('integrity_ok')}")
    print(f"  Semantic readback: {result.get('semantic_readback_ok')}")
    if result["errors"]:
        print(f"  Errors: {','.join(result['errors'])}")
    return bool(result["ready"])


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
    from bridge_db.auth import GRANT_TTL_DAYS, hash_token, scopes_for_caller
    from bridge_db.models import CALLER_IDS

    if caller not in CALLER_IDS:
        print(f"refused: unknown caller '{caller}'. Known: {', '.join(CALLER_IDS)}")
        return False
    if not _require_tty("enroll"):
        return False

    token = secrets.token_urlsafe(32)
    data = _read_principals_file()
    if data.get("version") != 2 and data["principals"]:
        print("refused: upgrade the existing registry with --upgrade-principals-v2")
        return False
    rotated = caller in data["principals"]
    previous = data["principals"].get(caller, {})
    previous_generation = previous.get("generation", 0)
    generation = previous_generation + 1 if isinstance(previous_generation, int) else 1
    issued_at = clock.now().astimezone(UTC)
    expires_at = issued_at + timedelta(days=GRANT_TTL_DAYS)
    data["version"] = 2
    data["principals"][caller] = {
        "token_sha256": hash_token(token),
        "issued_at": issued_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generation": generation,
        "scopes": scopes_for_caller(caller),
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


def run_upgrade_principals_v2() -> bool:
    """Preserve v1 token hashes while adding expiring, scoped v2 grants."""
    from bridge_db import config
    from bridge_db.audit import log_audit
    from bridge_db.auth import GRANT_TTL_DAYS, scopes_for_caller
    from bridge_db.models import CALLER_IDS

    if not _require_tty("upgrade-principals-v2"):
        return False
    data = _read_principals_file()
    if data.get("version") == 2:
        print("principals registry is already version 2")
        return True
    if data.get("version") != 1:
        print(f"refused: unsupported principals registry version {data.get('version')!r}")
        return False

    principals = cast(dict[str, Any], data["principals"])
    for caller, entry in principals.items():
        entry_dict = cast(dict[str, Any], entry) if isinstance(entry, dict) else {}
        token_hash = entry_dict.get("token_sha256")
        if (
            caller not in CALLER_IDS
            or not isinstance(token_hash, str)
            or len(token_hash) != 64
            or any(char not in "0123456789abcdef" for char in token_hash)
        ):
            print(f"refused: malformed v1 principal entry for {caller!r}")
            return False

    print(f"upgrade principals registry v1 -> v2 ({len(principals)} grants)")
    try:
        confirmed = input("Type 'upgrade' to preserve hashes and add 90-day grants: ")
    except EOFError:
        confirmed = ""
    if confirmed.strip() != "upgrade":
        print("upgrade cancelled")
        return False

    backup = config.PRINCIPALS_PATH.with_name(
        f"{config.PRINCIPALS_PATH.name}.pre-v2.bak"
    )
    if not backup.exists() and config.PRINCIPALS_PATH.exists():
        raw = config.PRINCIPALS_PATH.read_text(encoding="utf-8")
        fd, tmp = tempfile.mkstemp(dir=backup.parent)
        try:
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(raw)
            os.replace(tmp, backup)
        except Exception:
            os.unlink(tmp)
            raise

    issued_at = clock.now().astimezone(UTC)
    expires_at = issued_at + timedelta(days=GRANT_TTL_DAYS)
    upgraded: dict[str, Any] = {"version": 2, "principals": {}}
    for caller, entry in principals.items():
        upgraded["principals"][caller] = {
            "token_sha256": entry["token_sha256"],
            "issued_at": issued_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generation": 1,
            "scopes": scopes_for_caller(caller),
        }
    _write_principals_file(upgraded)
    log_audit(
        "auth.registry_upgrade",
        "operator-cli",
        None,
        ok=True,
        detail=f"version=1->2 grants={len(principals)} ttl_days={GRANT_TTL_DAYS}",
    )
    print(
        f"upgraded {len(principals)} grants to v2; existing token hashes preserved "
        f"through {expires_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
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
            f"  {caller}: issued_at={entry.get('issued_at', entry.get('enrolled_at', '?'))}, "
            f"expires_at={entry.get('expires_at', '?')}, "
            f"generation={entry.get('generation', '?')}, "
            f"scopes={','.join(entry.get('scopes', [])) or '?'}, "
            f"hash={str(entry.get('token_sha256', ''))[:8]}…"
        )
    return True


async def run_promote_section(section_name: str) -> bool:
    """Atomically promote one exact reviewed context version (TTY-gated)."""
    from bridge_db import config
    from bridge_db.audit import log_audit
    from bridge_db.db import content_sha256, open_db
    from bridge_db.models import SECTION_OWNERS

    if section_name not in SECTION_OWNERS:
        print(
            f"refused: unknown section '{section_name}'. Known: {sorted(SECTION_OWNERS)}"
        )
        return False
    if not _require_tty("promote-section"):
        return False

    db = await open_db(config.DB_PATH)
    promoted_from: str | None = None
    reviewed_hash: str | None = None
    reviewed_version: int | None = None
    try:
        cursor = await db.execute(
            "SELECT content, source_trust, updated_at, version "
            "FROM context_sections WHERE section_name = ?",
            (section_name,),
        )
        row = await cursor.fetchone()
        if row is None:
            print(f"no stored section '{section_name}'")
            return False
        reviewed_hash = content_sha256(cast(str, row["content"]))
        reviewed_version = cast(int, row["version"])
        if row["source_trust"] == "operator":
            print(
                f"section '{section_name}' is already operator-trusted "
                f"(version={reviewed_version} sha256={reviewed_hash})"
            )
            return True

        print(
            f"review section={section_name} version={reviewed_version} "
            f"source_trust={row['source_trust']} updated_at={row['updated_at']} "
            f"sha256={reviewed_hash}"
        )
        print("--- reviewed content ---")
        print(cast(str, row["content"]))
        print("--- end reviewed content ---")
        try:
            confirmed = input("Promote this exact section version to operator trust? [y/N] ")
        except EOFError:
            confirmed = ""
        if confirmed.strip().lower() not in {"y", "yes"}:
            print("promotion cancelled")
            return False

        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT content, source_trust, updated_at, version "
            "FROM context_sections WHERE section_name = ?",
            (section_name,),
        )
        current = await cursor.fetchone()
        if (
            current is None
            or current["version"] != reviewed_version
            or current["source_trust"] != row["source_trust"]
            or content_sha256(cast(str, current["content"])) != reviewed_hash
        ):
            await db.rollback()
            print("refused: section changed after review; inspect it again")
            return False

        promoted_from = cast(str, current["source_trust"])
        cursor = await db.execute(
            "UPDATE context_sections SET source_trust = 'operator' "
            "WHERE section_name = ? AND version = ? AND source_trust = ?",
            (section_name, reviewed_version, promoted_from),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            print("refused: section changed before promotion")
            return False
        await db.commit()
    finally:
        await db.close()

    log_audit(
        "auth.promote_section",
        "operator-cli",
        None,
        ok=True,
        detail=(
            f"section={section_name} version={reviewed_version} "
            f"{promoted_from}->operator sha256={reviewed_hash}"
        ),
    )
    print(
        f"promoted '{section_name}': {promoted_from} -> operator "
        f"(version={reviewed_version} sha256={reviewed_hash})"
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


def _handoff_row_payload(row: Any) -> dict[str, Any]:
    return {field: row[field] for field in _HANDOFF_PROMOTION_FIELDS}


def _handoff_row_json(row: Any) -> str:
    return json.dumps(_handoff_row_payload(row), sort_keys=True, separators=(",", ":"))


def _handoff_row_digest(row: Any) -> str:
    return hashlib.sha256(_handoff_row_json(row).encode("utf-8")).hexdigest()


async def _select_handoff(db: Any, handoff_id: int) -> Any:
    cursor = await db.execute(
        "SELECT id, project_name, project_path, roadmap_file, phase, "
        "dispatched_from, dispatched_at, picked_up_at, cleared_at, "
        "canonical_key, source_trust, status, claimed_by "
        "FROM pending_handoffs WHERE id = ?",
        (handoff_id,),
    )
    return await cursor.fetchone()


async def run_cancel_handoff(handoff_id: int, reason: str) -> bool:
    """Cancel one unclaimed handoff through an exact-row operator ceremony."""
    from bridge_db import config
    from bridge_db.audit import log_audit
    from bridge_db.db import open_db

    clean_reason = reason.strip()
    if handoff_id <= 0 or not clean_reason:
        print("refused: cancellation requires a positive handoff id and a reason")
        return False
    if not _require_tty("cancel-handoff"):
        return False

    db = await open_db(config.DB_PATH)
    reviewed_digest: str | None = None
    project_name: str | None = None
    previous_status: str | None = None
    try:
        row = await _select_handoff(db, handoff_id)
        if row is None:
            print(f"no stored handoff {handoff_id}")
            return False
        if row["status"] == "active" and row["claimed_by"] is not None:
            print(
                f"refused: handoff {handoff_id} is claimed by {row['claimed_by']}; "
                "the claimant owns completion"
            )
            return False
        if row["status"] not in ("pending", "active"):
            print(f"refused: handoff {handoff_id} is already {row['status']}")
            return False
        reviewed_digest = _handoff_row_digest(row)
        project_name = cast(str, row["project_name"])
        previous_status = cast(str, row["status"])
        print(
            f"cancel handoff id={handoff_id} project={json.dumps(project_name)} "
            f"status={previous_status} claimant={row['claimed_by']} "
            f"sha256={reviewed_digest} reason={json.dumps(clean_reason)}"
        )
        try:
            confirmed = input("Type 'cancel' to clear this exact unclaimed handoff: ")
        except EOFError:
            confirmed = ""
        if confirmed.strip() != "cancel":
            print("cancellation cancelled")
            return False

        await db.execute("BEGIN IMMEDIATE")
        current = await _select_handoff(db, handoff_id)
        if current is None or _handoff_row_digest(current) != reviewed_digest:
            await db.rollback()
            print("refused: handoff changed after review; inspect it again")
            return False
        cursor = await db.execute(
            "UPDATE pending_handoffs "
            "SET status = 'cleared', cleared_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE id = ? AND status = ? AND claimed_by IS NULL",
            (handoff_id, previous_status),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            print("refused: handoff changed before cancellation")
            return False
        await db.execute(
            """
            INSERT INTO handoff_cancellation_receipts (
                handoff_id, reason, previous_status, previous_claimant,
                reviewed_row_sha256, cancelled_by
            ) VALUES (?, ?, ?, NULL, ?, 'operator-cli')
            """,
            (handoff_id, clean_reason, previous_status, reviewed_digest),
        )
        await db.commit()
    finally:
        await db.close()

    log_audit(
        "handoff.operator_cancel",
        "operator-cli",
        project_name,
        ok=True,
        detail=(
            f"handoff_id={handoff_id} previous_status={previous_status} "
            f"sha256={reviewed_digest}"
        ),
    )
    print(f"cancelled handoff {handoff_id}; durable receipt recorded")
    return True


async def run_quarantine_cleared_operator_handoffs() -> bool:
    """Relabel legacy cleared operator rows while preserving exact recovery images."""
    from bridge_db import config
    from bridge_db.audit import log_audit
    from bridge_db.db import open_db

    if not _require_tty("quarantine-cleared-operator-handoffs"):
        return False
    db = await open_db(config.DB_PATH)
    reviewed: dict[int, tuple[str, str]] = {}
    try:
        cursor = await db.execute(
            "SELECT id, project_name, project_path, roadmap_file, phase, "
            "dispatched_from, dispatched_at, picked_up_at, cleared_at, "
            "canonical_key, source_trust, status, claimed_by "
            "FROM pending_handoffs "
            "WHERE status = 'cleared' AND source_trust = 'operator' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM handoff_trust_quarantine AS q "
            "WHERE q.handoff_id = pending_handoffs.id"
            ") "
            "ORDER BY id"
        )
        rows = list(await cursor.fetchall())
        if not rows:
            print("no cleared operator-trust handoffs require quarantine")
            return True
        for row in rows:
            row_json = _handoff_row_json(row)
            digest = hashlib.sha256(row_json.encode("utf-8")).hexdigest()
            reviewed[int(row["id"])] = (row_json, digest)
            print(
                f"quarantine handoff id={row['id']} "
                f"project={json.dumps(row['project_name'])} sha256={digest}"
            )
        try:
            confirmed = input(
                f"Type 'quarantine' to preserve and relabel {len(rows)} exact rows: "
            )
        except EOFError:
            confirmed = ""
        if confirmed.strip() != "quarantine":
            print("quarantine cancelled")
            return False

        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT id, project_name, project_path, roadmap_file, phase, "
            "dispatched_from, dispatched_at, picked_up_at, cleared_at, "
            "canonical_key, source_trust, status, claimed_by "
            "FROM pending_handoffs "
            "WHERE status = 'cleared' AND source_trust = 'operator' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM handoff_trust_quarantine AS q "
            "WHERE q.handoff_id = pending_handoffs.id"
            ") "
            "ORDER BY id"
        )
        current_rows = list(await cursor.fetchall())
        current = {
            int(row["id"]): (
                _handoff_row_json(row),
                _handoff_row_digest(row),
            )
            for row in current_rows
        }
        if current != reviewed:
            await db.rollback()
            print("refused: quarantine set changed after review; inspect it again")
            return False
        for row in current_rows:
            handoff_id = int(row["id"])
            row_json, digest = reviewed[handoff_id]
            await db.execute(
                """
                INSERT INTO handoff_trust_quarantine (
                    handoff_id, row_json, row_sha256, previous_source_trust,
                    quarantined_by
                ) VALUES (?, ?, ?, 'operator', 'operator-cli')
                """,
                (handoff_id, row_json, digest),
            )
            updated = await db.execute(
                "UPDATE pending_handoffs SET source_trust = 'ingested' "
                "WHERE id = ? AND status = 'cleared' AND source_trust = 'operator'",
                (handoff_id,),
            )
            if updated.rowcount != 1:
                await db.rollback()
                print(f"refused: handoff {handoff_id} changed before quarantine")
                return False
        await db.commit()
    finally:
        await db.close()

    log_audit(
        "handoff.trust_quarantine",
        "operator-cli",
        None,
        ok=True,
        detail=f"rows={len(reviewed)} ids={sorted(reviewed)}",
    )
    print(f"quarantined {len(reviewed)} cleared operator-trust handoffs")
    return True


async def run_restore_handoff_trust(handoff_id: int) -> bool:
    """Restore one quarantined row only when its complete state still matches."""
    from bridge_db import config
    from bridge_db.audit import log_audit
    from bridge_db.db import open_db

    if handoff_id <= 0:
        print("refused: handoff id must be a positive integer")
        return False
    if not _require_tty("restore-handoff-trust"):
        return False
    db = await open_db(config.DB_PATH)
    stored_digest: str | None = None
    project_name: str | None = None
    try:
        receipt = await (
            await db.execute(
                "SELECT row_json, row_sha256, restored_at "
                "FROM handoff_trust_quarantine WHERE handoff_id = ?",
                (handoff_id,),
            )
        ).fetchone()
        if receipt is None or receipt["restored_at"] is not None:
            print(f"no active quarantine recovery image for handoff {handoff_id}")
            return False
        row = await _select_handoff(db, handoff_id)
        if row is None or row["source_trust"] != "ingested":
            print("refused: current handoff is missing or no longer quarantined")
            return False
        stored = cast(dict[str, Any], json.loads(cast(str, receipt["row_json"])))
        stored_row_json = cast(str, receipt["row_json"])
        stored_digest = cast(str, receipt["row_sha256"])
        if hashlib.sha256(stored_row_json.encode("utf-8")).hexdigest() != stored_digest:
            print("refused: quarantine recovery image failed digest verification")
            return False
        current = _handoff_row_payload(row)
        current["source_trust"] = "operator"
        if current != stored:
            print("refused: current handoff differs from the recovery image")
            return False
        project_name = cast(str, row["project_name"])
        try:
            confirmed = input(
                f"Type 'restore' to restore operator trust for handoff {handoff_id}: "
            )
        except EOFError:
            confirmed = ""
        if confirmed.strip() != "restore":
            print("restore cancelled")
            return False

        await db.execute("BEGIN IMMEDIATE")
        locked_receipt = await (
            await db.execute(
                "SELECT row_json, row_sha256, restored_at "
                "FROM handoff_trust_quarantine WHERE handoff_id = ?",
                (handoff_id,),
            )
        ).fetchone()
        if (
            locked_receipt is None
            or locked_receipt["restored_at"] is not None
            or locked_receipt["row_json"] != stored_row_json
            or locked_receipt["row_sha256"] != stored_digest
        ):
            await db.rollback()
            print("refused: quarantine recovery image changed before restore")
            return False
        current_row = await _select_handoff(db, handoff_id)
        if current_row is None:
            await db.rollback()
            print("refused: handoff disappeared before restore")
            return False
        current_payload = _handoff_row_payload(current_row)
        current_payload["source_trust"] = "operator"
        if current_payload != stored:
            await db.rollback()
            print("refused: handoff changed before restore")
            return False
        cursor = await db.execute(
            "UPDATE pending_handoffs SET source_trust = 'operator' "
            "WHERE id = ? AND source_trust = 'ingested'",
            (handoff_id,),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            print("refused: handoff trust changed before restore")
            return False
        await db.execute(
            "UPDATE handoff_trust_quarantine "
            "SET restored_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE handoff_id = ? AND restored_at IS NULL",
            (handoff_id,),
        )
        await db.commit()
    finally:
        await db.close()

    log_audit(
        "handoff.trust_restore",
        "operator-cli",
        project_name,
        ok=True,
        detail=f"handoff_id={handoff_id} sha256={stored_digest}",
    )
    print(f"restored operator trust for handoff {handoff_id}")
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
        "--create-recovery-anchor",
        action="store_true",
        help="Create one atomic RecoveryAnchorV1 bundle, without overwriting evidence",
    )
    parser.add_argument(
        "--verify-recovery-anchor",
        action="store_true",
        help="Verify the current RecoveryAnchorV1 bundle using a disposable copy",
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
        "--upgrade-principals-v2",
        action="store_true",
        help="Preserve v1 token hashes and add scoped, expiring v2 grants (operator TTY only)",
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
    parser.add_argument(
        "--cancel-handoff",
        metavar="ID",
        type=int,
        help="Cancel one exact unclaimed handoff (operator TTY only)",
    )
    parser.add_argument(
        "--cancel-reason",
        help="Required durable reason for --cancel-handoff",
    )
    parser.add_argument(
        "--quarantine-cleared-operator-handoffs",
        action="store_true",
        help="Preserve and relabel cleared legacy operator handoffs (operator TTY only)",
    )
    parser.add_argument(
        "--restore-handoff-trust",
        metavar="ID",
        type=int,
        help="Restore one exact quarantined handoff recovery image (operator TTY only)",
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
    if args.create_recovery_anchor:
        sys.exit(0 if run_create_recovery_anchor() else 1)
    if args.verify_recovery_anchor:
        sys.exit(0 if run_verify_recovery_anchor() else 1)
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
    if args.upgrade_principals_v2:
        sys.exit(0 if run_upgrade_principals_v2() else 1)
    if args.promote_section:
        sys.exit(0 if asyncio.run(run_promote_section(args.promote_section)) else 1)
    if args.promote_handoff is not None:
        sys.exit(0 if asyncio.run(run_promote_handoff(args.promote_handoff)) else 1)
    if args.cancel_handoff is not None:
        if not args.cancel_reason:
            parser.error("--cancel-handoff requires --cancel-reason")
        sys.exit(
            0
            if asyncio.run(run_cancel_handoff(args.cancel_handoff, args.cancel_reason))
            else 1
        )
    if args.quarantine_cleared_operator_handoffs:
        sys.exit(
            0 if asyncio.run(run_quarantine_cleared_operator_handoffs()) else 1
        )
    if args.restore_handoff_trust is not None:
        sys.exit(
            0 if asyncio.run(run_restore_handoff_trust(args.restore_handoff_trust)) else 1
        )

    from bridge_db.server import mcp

    mcp.run()


if __name__ == "__main__":
    main()
