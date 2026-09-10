# bridge-db improvement proposals (drafts for review)

> **Outcome (2026-07-11): all six shipped.** Implemented on four branches during the
> same session, operator-approved, and merged to main: P1 via `fix/clock-seam-leak`,
> P2+P3 via `feat/coordination-visibility` (review-hardened by an 8-angle code review),
> P4+P5 via `feat/observability-parity`, P6 via `fix/update-section-echo-note`.
> Post-merge verifier: 428 tests passed, pyright 0 errors (strict), ruff clean.
> The text below is preserved as written pre-implementation.

Session: 2026-07-10 Fable deep-dive. All proposals were drafted as reviewable diffs —
no src/ edits on this branch, no live DB touched. Ranked by (value ÷ risk). Each
carries a concrete diff sketch and a test plan. Scope check: all six are
coordination/memory/data-model side; none expand bridge-db toward a knowledge store;
none change the MCP surface incompatibly.

---

## P1 — Close the clock-seam leak (`date.today()` × 2)  [correctness, small]

**Problem (verified).** `clock.py`'s contract: "Every Python-side wall-clock read in
bridge_db goes through `now()`." Two violations exist:

- `tools/activity.py:282` — `ts = timestamp or str(date.today())`
- `tools/export.py:129` — `today = str(date.today())`

Two consequences:
1. **DST determinism hole.** Any sim scenario that calls `log_activity` without an
   explicit `timestamp` (or renders the export) reads the host's real clock. The DB
   file stops being a pure function of (seed, scenario, SHA). No current scenario
   trips it — that's luck, not protection; the next activity-path scenario inherits a
   flaky substrate.
2. **Local-vs-UTC inconsistency.** `date.today()` is host-local; every other
   timestamp (SQLite `strftime('now')`, `clock.now()`, snapshots'
   `_utc_snapshot_date()`) is UTC. A 5pm PT `log_activity` and a 5pm PT
   `save_snapshot` currently disagree about what day it is after 4/5pm PT.

**Change.**
```python
# tools/activity.py
from bridge_db import clock  # (config import already present)
ts = timestamp or clock.now().date().isoformat()

# tools/export.py
from bridge_db import clock
today = clock.now().date().isoformat()
```
Remove the now-unused `from datetime import date` imports.

**Behavioral note (the one decision you need to make).** The default logical date
becomes the UTC date, matching snapshots. For a late-evening PT session the default
date shifts to "tomorrow" — but the existing since-filter matches either
`timestamp` or `created_at` (the CLAUDE.md "activity date semantics" gotcha exists
precisely because this straddle already happens), so discoverability is unchanged.
The alternative — preserving local dates via `clock.now().astimezone().date()` — keeps
today's default but makes the sim DB depend on host TZ, which is exactly the
nondeterminism DST exists to kill. Recommend UTC.

**Tests.** (a) Unit: install a fixed provider, call `log_activity` with no timestamp,
assert the row's `timestamp` equals the provider's UTC date. (b) DST: extend the
determinism test to route one `log_activity` through the sim and assert `trace_hash`
+ DB bytes stable across replays. (c) Grep-guard test: assert no `date.today()` /
`datetime.now(` outside `clock.py` (cheap way to keep the seam sealed — mirrors the
FTS-invariant style of enforcement).

**Docs.** CLAUDE.md gotcha "Activity date semantics" gains one sentence: default
logical date is UTC as of vNext.

---

## P2 — Surface `write_conflicts` in health/status  [observability, small]

**Problem (verified).** Neither `tools/health.py` nor `__main__.py` mentions
`write_conflicts` at all. The receipt ledger is the system's forensic backbone — and
its only consumer is a tool nobody is nagged to call. With CAS now in enforce mode and
the DST campaign deliberately generating raced/stale receipts, "open receipts exist"
is precisely the operator-attention signal health exists to carry. Related asymmetry:
receipts also have no lifecycle pressure — `status` transitions
(acknowledged/resolved/ignored) exist in the schema but nothing reads or nags on them.

**Change (read-only, no new mutating surface).**
- `collect_health_metrics`: add `open_write_conflicts` (count of `status='open'`) and
  `oldest_open_conflict_age_hours`. Soft signal like `wal_warning` — does NOT fold
  into `ok` (a conflict receipt is evidence of the system working, not of it being
  broken; folding it into `ok` would teach operators to ignore red).
- `collect_status_summary` signals block: add `open_write_conflicts`, with a
  `next_command` hint pointing at `get_write_conflicts(status="open")`.
- `--dogfood`: print the count; do not gate on it.

**Deliberately NOT proposed:** a receipt-retention/auto-close policy. Receipts are
cheap rows; silence about them was the problem, not their persistence. Revisit only if
the table actually grows past nuisance size — and then age out `resolved`/`ignored`
rows only, never `open`.

**Tests.** Health unit test seeds one open + one resolved receipt, asserts count=1 and
age computed from the open one; status test asserts the signal appears only when >0.

---

## P3 — Make active handoff claims readable  [API gap, small]

**Problem.** `pick_up_handoff` records `claimed_by` (schema v13) and `picked_up_at`,
but no read surface returns them: `get_pending_handoffs` filters `status='pending'`
hard-coded and omits both columns. An operator (or a `/start` hook) cannot answer
"who holds what right now?" without raw SQL. The INV-13 claimant gate ships blind.

**Change (back-compatible).**
```python
async def get_pending_handoffs(
    status: Literal["pending", "active", "all"] = "pending",  # new, default preserves behavior
    ctx: Context = None,
) -> list[dict[str, Any]]:
```
- WHERE clause keyed on the param (`all` = pending+active; `cleared` stays excluded —
  it's history, `recall` covers it).
- Add `picked_up_at` and `claimed_by` to the SELECT and payload for all rows (NULL on
  pending rows; harmless additive fields for existing consumers).

**Tests.** Create → default call returns it; pick up → default call no longer returns
it, `status="active"` returns it with claimant; `all` returns both orderings stable.

**Docs.** Server instructions string + CLAUDE.md tool notes mention the filter.

---

## P4 — Audit parity for snapshot prunes  [consistency, tiny]

**Problem.** BD-INV-1's philosophy is "every prune emits an audit line," enforced for
activity (`log_activity.prune` names ids + tags). `_prune_snapshots` deletes rows in
silence — no audit event, no count returned to the caller. Snapshots are
instruction-bearing rows (they carry `source_trust`); their deletion should be as
loud as activity's.

**Change.** `_prune_snapshots` returns the deleted ids (+ families); `save_snapshot`
emits `log_audit("save_snapshot.prune", caller, None, ok=True,
detail=f"pruned={n} ids={ids} families={fams}")` when non-empty, and includes
`pruned_count` in its return payload.

**Tests.** Save 11 same-family snapshots, assert one prune audit line with the evicted
id, assert cross-family retention untouched (existing family tests already cover the
partition; this adds the audit assertion).

---

## P5 — Caller attribution for `recall`  [observability, tiny]

**Problem.** `_log_recall(..., caller=None)` — always None; the tool takes no caller
param. `recall_stats` can answer "is recall earning its keep?" but never "for whom?"
Per-system miss rates are the difference between "recall is weak" and "Codex phrases
queries badly" — an actual routing decision.

**Change.** Optional `caller: CallerID | None = None` param on `recall` (optional →
every existing consumer keeps working; read-only tool so no `require_caller`
enforcement — the value is telemetry, not authz; principal, when bound, is recorded
alongside rather than instead, mirroring the `attempted_by`/`principal` receipt
split). Thread through to `_log_recall`; `collect_recall_stats` adds a
`caller_breakdown` map (count + miss_rate per caller, `unattributed` bucket).

**Tests.** Query with and without caller; stats roll-up shows both buckets with
correct miss rates.

---

## P6 — Comment-grade fix: `update_section` post-commit echo  [cosmetic]

**Problem.** After a successful CAS write commits, the tool re-reads the row to echo
`version`/`content_sha256`. On the shared connection another writer can land between
commit and re-read, so the echo can describe a *newer* row than the write it reports.
Harmless today (callers treat the response as "current state"), but it's the kind of
almost-lie DST scenarios eventually trip over.

**Change (pick one):**
- (a) Two-line comment stating the echo is "current state at response time, not
  necessarily this write's image" — zero risk; or
- (b) Compute the echo from the UPDATE itself via `RETURNING version, updated_at,
  source_trust` (SQLite ≥3.35, comfortably below the 3.50.4 already probed) — the
  echo becomes exactly this write's image.

Recommend (b) with (a)'s honesty note in the docstring either way.

**Tests (b).** Existing update_section tests keep passing; add one asserting the
returned version is exactly `if_match_version + 1` even when a concurrent writer
lands after commit (DST two-writer scenario covers the interleaving).

---

## Sequencing

P1 first (correctness, and it hardens the DST substrate everything else gets verified
on). P2+P3 together as one "coordination visibility" PR. P4+P5 together as one
"observability parity" PR. P6 rides along with whichever touches context.py next.
Each PR: `uv run pytest && uv run pyright && uv run ruff check` green before review.
