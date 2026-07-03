"""Reconcile stored canonical keys to GithubRepoAuditor's registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiosqlite

from bridge_db.audit import log_audit
from bridge_db.project_resolver import resolve as resolve_project


@dataclass(frozen=True)
class CanonicalKeyReconcileResult:
    """Summary of one canonical-key reconciliation pass."""

    registry_present: bool
    rows_checked: int
    rows_updated: int
    disagreements_resolved: int
    unresolvable_rows: int
    unresolvable_nulled: int
    table_counts: dict[str, dict[str, int]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "registry_present": self.registry_present,
            "rows_checked": self.rows_checked,
            "rows_updated": self.rows_updated,
            "disagreements_resolved": self.disagreements_resolved,
            "unresolvable_rows": self.unresolvable_rows,
            "unresolvable_nulled": self.unresolvable_nulled,
            "table_counts": self.table_counts,
        }


async def reconcile_canonical_keys(
    db: aiosqlite.Connection,
    *,
    audit: bool = True,
    caller: str = "codex",
    project: str = "bridge-db",
) -> CanonicalKeyReconcileResult:
    """Rewrite stored canonical_key values to GHRA repo_full_name values.

    GithubRepoAuditor remains the source of truth. This pass consumes its
    registry through project_resolver, fixes disagreeing bridge rows, and sets
    unresolvable rows to NULL instead of preserving drifted slugs.
    """

    probe = resolve_project("__bridge_db_registry_probe__")
    if not probe.registry_present:
        result = CanonicalKeyReconcileResult(
            registry_present=False,
            rows_checked=0,
            rows_updated=0,
            disagreements_resolved=0,
            unresolvable_rows=0,
            unresolvable_nulled=0,
            table_counts={},
        )
        if audit:
            log_audit(
                "reconcile_canonical_keys",
                caller,
                project,
                ok=False,
                detail="registry_present=false updated=0",
            )
        return result

    totals = {
        "rows_checked": 0,
        "rows_updated": 0,
        "disagreements_resolved": 0,
        "unresolvable_rows": 0,
        "unresolvable_nulled": 0,
    }
    table_counts: dict[str, dict[str, int]] = {}

    for table in ("activity_log", "pending_handoffs"):
        pk = "id"
        cursor = await db.execute(
            f"SELECT {pk}, project_name, canonical_key FROM {table}"  # noqa: S608
        )
        rows = await cursor.fetchall()
        counts = {
            "rows_checked": 0,
            "rows_updated": 0,
            "disagreements_resolved": 0,
            "unresolvable_rows": 0,
            "unresolvable_nulled": 0,
        }
        for row in rows:
            counts["rows_checked"] += 1
            totals["rows_checked"] += 1
            resolution = resolve_project(row["project_name"])
            desired = resolution.canonical_key if resolution.matched else None
            existing = row["canonical_key"]
            if desired is None:
                counts["unresolvable_rows"] += 1
                totals["unresolvable_rows"] += 1
            if existing == desired:
                continue
            await db.execute(
                f"UPDATE {table} SET canonical_key = ? WHERE {pk} = ?",  # noqa: S608
                (desired, row[pk]),
            )
            counts["rows_updated"] += 1
            totals["rows_updated"] += 1
            if desired is None:
                counts["unresolvable_nulled"] += 1
                totals["unresolvable_nulled"] += 1
            else:
                counts["disagreements_resolved"] += 1
                totals["disagreements_resolved"] += 1
        table_counts[table] = counts

    await db.commit()
    result = CanonicalKeyReconcileResult(
        registry_present=True,
        rows_checked=totals["rows_checked"],
        rows_updated=totals["rows_updated"],
        disagreements_resolved=totals["disagreements_resolved"],
        unresolvable_rows=totals["unresolvable_rows"],
        unresolvable_nulled=totals["unresolvable_nulled"],
        table_counts=table_counts,
    )
    if audit:
        log_audit(
            "reconcile_canonical_keys",
            caller,
            project,
            ok=True,
            detail=(
                f"checked={result.rows_checked} updated={result.rows_updated} "
                f"disagreements_resolved={result.disagreements_resolved} "
                f"unresolvable_rows={result.unresolvable_rows} "
                f"unresolvable_nulled={result.unresolvable_nulled}"
            ),
        )
    return result
