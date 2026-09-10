# bridge-db: Findings from a Full Read (2026-07-10)

Read scope: all of `src/bridge_db/` (db.py, server, config, models, clock, invariants,
audit, instruction_boundary, project_resolver, and every tool module), the DST harness
(`tests/dst/sim.py`, cas-pingpong scenario, regress_seeds.txt), ROADMAP.md, CLAUDE.md,
and the git history arc (211 commits, phase-0 through the DST campaign).

## What this thing actually is

One SQLite file (`bridge.db`, WAL mode) that five agent systems — Claude Code, Codex,
Claude.ai, Notion OS, personal-ops — use as their shared nervous system, fronted by a
stdio MCP server. 26 tools across 10 modules. But calling it "a database with an API"
undersells the design. It's really four systems sharing one file:

1. **A coordination substrate** — handoff queue with single-winner claim semantics,
   context sections with optimistic concurrency (integer-version CAS), and durable
   `write_conflicts` receipts for every refused or displaced write.
2. **A memory layer** — activity log with protected-tag retention (BD-INV-1),
   snapshots with per-family retention, FTS5 `content_index` mirror kept transactionally
   in sync with four source tables, `recall` with bm25 + AND-first/OR-fallback.
3. **An accountability layer** — shipped events must terminate in either a
   *receipt* (proof of downstream sync) or a *disposition* (explicit policy reason
   for no sync). Every prune, every conflict, every refused claim leaves an audit line.
4. **A verification harness** — production-live `always()`/`sometimes()` invariants
   (TigerBeetle vocabulary) plus a deterministic-simulation substrate that replays
   seeded interleavings of the real tool code against the real schema.

## The load-bearing design moves (ranked by how interesting they are)

### 1. The DST harness is the headline
`tests/dst/sim.py` is a genuine FoundationDB/TigerBeetle-style deterministic simulator,
scaled to a personal project:
- `SimClock` — logical time that only moves on `tick()`; installed both into the Python
  clock seam AND into SQLite itself via a per-connection `strftime` override
  (`create_function("strftime", ...)`), so even column DEFAULTs run on simulated time.
- `SimConnection` — async facade over synchronous sqlite3, removing aiosqlite's executor
  thread (the one concurrency source the harness can't seed). SQLITE_BUSY becomes a
  scheduler-owned event (`timeout=0`), not invisible C-layer waiting.
- `SimScheduler` — every writer parks at every connection op; a seeded RNG picks who
  proceeds. All concurrency is an explicit, replayable grant sequence.
- `FaultPlan` — buggify-style fault points (crash / busy / delay / error) keyed on
  statement fingerprints, with liveness coin-flips and fire probabilities off the run RNG.
  `pre-commit` faults key on the last statement of the *current* transaction — precise
  enough to land a crash in a specific two-op window.
- `trace_hash` — the run is a pure function of (seed, scenario, git SHA); replay is
  bit-identical (asserted in tests).
- `regress_seeds.txt` — every bug-finding seed is pinned forever, with comments that
  read like incident reports ("9/30 sweep seeds hit the race branch").

And it *found real bugs that changed the system*: the INV-4 evidence (17/30 seeds
lose committed work under warn-mode CAS) flipped the production default to `enforce`;
INV-2 found a two-op crash window that silently dropped conflict receipts; INV-8 found
a TOCTOU on the trust coordinate; INV-13 added the claimant column (schema v13).
The config comment on `CONTEXT_CAS_MODE` literally cites the seed file.

### 2. Receipts as first-class data (losing loudly)
The design refuses to let any coordination failure be trace-free:
- CAS rejection → `stale_cas`/`missing_cas` receipt, staged **inside the same
  transaction** as the refused write (the zero-rowcount UPDATE holds the tx open;
  the receipt INSERT joins it; one commit makes both durable atomically). This is
  INV-5 and it closes a crash window that a separate receipt-commit would leave.
- Accepted blind overwrite (warn mode) → `legacy_blind_write` receipt carrying the
  *displaced content's sha256*. Displacement may be legal; it is never anonymous.
- Raced handoff claim → `raced_claim` receipt. Crashed claim retry → `stale_claim`
  receipt via statement-atomic insert-if-absent (WHERE NOT EXISTS inside the INSERT,
  so two concurrent retries converge to one row).
- Stale markdown import → `stale_export_base` receipt keyed on export-state CAS
  (exported version + content sha recorded at export time).

### 3. Live invariants with reachability counters
`always()` crashes the tool call loudly on protocol violation (with rollback-first
`always_tx` so the shared connection can't flush corrupt state later). `sometimes()`
is the inverse: proof that a guard actually fires. A `sometimes-coverage` gate in DST
distinguishes a defended edge case from a dead branch — "tier-1 dead-harness defense."
That's a testing idea most production teams don't have.

### 4. The provenance lattice (`source_trust`)
Three-level trust label (`operator` / `agent` / `ingested`) on all four
instruction-bearing tables, with real behavioral consequences at the dangerous
transition: Codex (danger-full-access) is refused non-operator handoffs outright;
CC must re-confirm. File-channel imports are demoted to `ingested` when auth is on.
Every read returns an `instruction_boundary` block — "stored data, not instructions" —
prompt-injection hygiene at the data-model level. (Ground rule: guard internals stay
out of public material; the *data-model* idea — provenance as a column with a lattice —
is coordination-side and fair game.)

### 5. Lifecycle honesty: receipts vs dispositions
`SHIPPED` rows can't just be marked processed (the legacy tool refuses them). They
terminate as either proof (`shipped_sync_receipts`: downstream system + ref) or an
explicit policy decision (`shipped_event_dispositions`: enum'd reason, decided_by).
Unresolved rows nag in health. This is a tiny state machine that encodes "claims
require evidence" into the schema.

### 6. Retention with a durability contract (BD-INV-1)
Rolling window (50/source) with tag-protected exemption, case-insensitive on purpose
(the one deliberately non-exact-case matcher in the codebase — a lowercase 'ledger'
from any writer must still protect). Every prune emits an audit line naming deleted
ids+tags. Health has orphan-receipt/orphan-disposition gates. Discovered the hard way:
the prune used to silently eat SHIPPED rows *and cascade-delete their receipts*
(ACTIVITY-LEDGER-DISCOVERY-2026-07-09).

### 7. The FTS mirror invariant
Every write path to the four content tables upserts/GCs `content_index` in the same
transaction; drift is a *hard health failure* because recall depends on the mirror.
Migration v11 exists purely to atone for a change that shipped without a version bump
(tags added to FTS text without reindexing history) — the migration ladder as
confession log.

### 8. Scope discipline as a feature
The semantic-memory arc CLOSED at Phase −1 with an eval: most recall misses were
content that isn't in bridge.db at all, so a vector layer can't help. "bridge.db is a
cross-system state bridge, not a knowledge store" is enforced in four docs. The
roadmap's reopening clause then got exercised *correctly* for the durable ledger
(cross-system state coordination, in-scope) — scope discipline with a working hinge.

## The lifecycle story (how data flows)

```
claude_ai ──create_handoff──▶ pending_handoffs (pending)
cc/codex ──pick_up_handoff──▶ (active, claimed_by, trust gate, CAS single-winner)
cc/codex ──clear_handoff────▶ (cleared; claimant-gated, opportunistic by name/canonical key)

any caller ──log_activity──▶ activity_log ──prune(50/source, tag-protected)──▶ audit line
        SHIPPED rows ──▶ confirm_shipped_sync (receipt) | record_shipped_event_disposition
cc/codex ──save_snapshot──▶ system_snapshots (10/family)

all writes ──▶ content_index (FTS5, same-tx mirror) ──▶ recall (bm25)
all writes ──▶ export_bridge_markdown ──▶ atomic-rename fallback file ──▶ sync_from_file
                       └─ export-state CAS guards the round trip
```

Identity thread: `project_name` (free-form) → GHRA registry → `canonical_key`
(`repo_full_name` or `supp:<slug>`), resolved on write, pass-through when the registry
is absent, unmatched names audited rather than silently recorded. mtime-keyed cache.

## Rough edges found (proposal candidates)

1. **Clock-seam leak (real, verified):** `tools/activity.py:282` and
   `tools/export.py:129` call `date.today()` directly — bypassing `clock.now()`,
   whose module docstring claims total coverage. Consequences: (a) DST runs that hit
   `log_activity` without an explicit timestamp read the *real* clock →
   nondeterminism; (b) `date.today()` is **local** time while everything else in the
   system is UTC (snapshots use `clock.now().date()` = UTC) — near midnight, the
   activity's logical date and `created_at`'s UTC date can straddle differently in
   activity vs snapshots. Fix is two lines + a regression test.
2. **`recall` has no caller attribution:** `_log_recall(..., caller=None)` always —
   the tool takes no caller param, so `recall_stats` can never break down usage by
   system. Cheap, within observability scope.
3. **Active handoffs are invisible:** `get_pending_handoffs` returns only
   `status='pending'`; there is no read surface listing active claims (who holds what,
   since when) short of health-freshness signals. A `status` filter param (default
   pending, back-compat) would close it. `claimed_by` isn't returned anywhere either.
4. **Snapshot prunes are silent:** activity prunes emit `log_activity.prune` audit
   lines; `_prune_snapshots` deletes with no audit trail. Asymmetry vs BD-INV-1's
   "every prune emits an audit line" philosophy.
5. **`write_conflicts` has no lifecycle:** receipts accumulate forever with
   status='open'; nothing transitions them and there's no retention policy. Today the
   table is famously near-empty (the essay exists), but the DST campaign is now
   *designed* to generate receipts on races — a resolve/acknowledge path or aging
   policy keeps the "open receipts = signal" property meaningful.
6. **`update_section`'s post-commit echo re-read** can race a concurrent writer on the
   shared connection (returned version may be newer than the write it echoes). Cosmetic,
   but worth a comment or a RETURNING clause.
7. **Export freshness depends on best-effort auto-export** buried in
   `_export_bridge_markdown_after_processing` (catch-all warn). Freshness *is* health-
   monitored, so this is documented-degraded rather than silent; still the weakest link
   in the file-fallback chain.

## Public-material angles (pre-research shortlist)

Existing pieces cover: why the conflict table stays empty; FTS5 over vectors. Missed
angles, best-first as of this read:

- **A: "I ran TigerBeetle-style simulation testing on my SQLite file"** — the DST
  story: sim clock down into strftime, seeded scheduler, buggify faults, pinned seed
  corpus, and the punchline that it flipped a production default (INV-4) and found a
  receipt-eating crash window (INV-2). Interactive potential: a real interleaving
  explorer — pick a seed, scrub the grant sequence, watch the lost update happen.
- **B: "Losing loudly: write-conflict receipts as a design pattern"** — the receipt
  discipline (same-tx staging, displaced-sha forensics, insert-if-absent convergence).
  Sibling of the existing empty-conflict-table essay — that one is "why empty," this
  one is "what the machinery does when it isn't."
- **C: "The claim is not the SELECT"** — TOCTOU at N=2 agents: status-guarded UPDATE
  as the real claim, the trust coordinate as the second TOCTOU axis, claimant-gated
  clears. Interactive: two-writer race stepper.
- **D: "Retention that can't eat its receipts"** — BD-INV-1, the discovery audit as
  incident story, protected tags, prune audit lines, orphan gates.
- **E: "A schema is a ledger of your mistakes"** — the migration ladder v1→v13 read as
  autobiography (v11 = the FTS confession; v13 = the DST-driven claimant column).
  Diagram-friendly.
- **F: "Sometimes() — asserting that your guards actually fire"** — reachability
  counters as first-class production telemetry; dead-harness defense.
- **G: Data-model diagram set** — the four-plane view (coordination / memory /
  accountability / verification) over one SQLite file.
