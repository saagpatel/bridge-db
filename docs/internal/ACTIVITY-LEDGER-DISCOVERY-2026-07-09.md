# Activity-Logging + Retention Subsystem — Discovery Audit (2026-07-09)

**Status: discovery only. No design, no code.** This document is the complete map of
bridge-db's activity-logging + retention subsystem, produced ahead of the planned
retention-change + durable-ledger design. Every claim was verified against live code
(`~/Projects/bridge-db`, HEAD `d145eca`) and the live DB
(`~/.local/share/bridge-db/bridge.db`, schema v12, WAL) on 2026-07-09/10.
Six parallel read-only audit agents traced: write paths, consumers, the SHIPPED→Notion
pipeline, the FTS5 mirror, the historical rationale, and schema/migrations/INV-13.

---

## 0. Executive summary — the corrected mental model

The operator's mental model ("logged BridgeDB" = durable ledger the next agent catches
up from) diverges from reality on **four** axes, not just the known 50-cap:

1. **Rolling buffer, and documented as such.** `activity_log` keeps the newest 50 rows
   per source; the docs explicitly call this "a convenience retention policy for recent
   context, **not a proof ledger**" (`docs/internal/OPERATOR-CHECKLIST.md:199-211`).
   The buffer-vs-ledger tension was noticed at design time and deliberately resolved
   toward *buffer*, with durability delegated to `shipped_sync_receipts` + Notion +
   memory files.
2. **The designated durable ledger is itself not durable.** `shipped_sync_receipts`
   and `shipped_event_dispositions` carry `FOREIGN KEY ... REFERENCES activity_log(id)
   ON DELETE CASCADE`, and `PRAGMA foreign_keys=ON` is applied on every connection
   (`db.py:442`, test-asserted). When a receipted SHIPPED row rotates out of the
   50-row window, **its receipt cascade-deletes in the same silent prune**. The only
   truly durable copy of a shipped event is the Notion Build Log page. Local "proof"
   is time-bombed by design contradiction: docs say the receipts table is "never
   auto-pruned"; the schema wires it to die with the buffer.
3. **Prune deletions are invisible.** No audit entry, no receipt, no count, no
   tombstone. The row's FTS mirror is GC'd in the same transaction, so it also vanishes
   from `recall`. AUTOINCREMENT id gaps are the only fingerprint, and they can't
   distinguish a lost SHIPPED row from routine churn. Zero historical loss is
   **unprovable**.
4. **Catch-up surfacing is 5 entries deep.** The two wired cold-start paths (SessionStart
   warmup hook; `/start` skill) each surface only 5 entries, and the hook is scoped to
   `project_name = basename(cwd)`. Even within the 50-row window, most of the "ledger"
   never reaches the next agent unless it asks explicitly.

Aggravating factor: **cc's 50 slots are shared with session-boundary telemetry.** Live,
35 of cc's 54 rows are `session-boundary` lifecycle rows; only ~19 are substantive.
The effective substantive cc history is ~19 entries, and every SessionEnd erodes it
further until the next MCP write re-prunes.

Live counts (2026-07-10): total 181 — cc 54, codex 50, personal_ops 50, claude_ai 14,
notion_os 13. codex and personal_ops are pinned at cap (their windows are live and
tight); cc exceeds 50 via the non-pruning SessionEnd path (§2).

---

## 1. Schema reality

### activity_log (live DDL == code DDL, no structural drift)

```sql
CREATE TABLE activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL CHECK(source IN ('cc','codex','claude_ai','notion_os','personal_ops')),
    timestamp TEXT NOT NULL,          -- caller-supplied LOGICAL event date (e.g. '2026-07-09')
    project_name TEXT NOT NULL,
    summary TEXT NOT NULL,            -- what recall full-text searches
    branch TEXT,
    tags TEXT NOT NULL DEFAULT '[]',  -- JSON array serialized to TEXT; 'SHIPPED' is the only tag with structured consumers
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),  -- server insert time, UTC; PRUNE ORDERS BY THIS
    canonical_key TEXT,               -- GHRA repo_full_name, resolved on write (v5)
    source_trust TEXT NOT NULL DEFAULT 'agent' CHECK(source_trust IN ('operator','agent','ingested'))  -- v7
);
CREATE INDEX idx_activity_source ON activity_log(source);
CREATE INDEX idx_activity_timestamp ON activity_log(timestamp DESC);
```

Key semantics: `timestamp` (logical event date) ≠ `created_at` (insert time); the prune
orders by `created_at DESC, id DESC`. `source` is CHECK-constrained at the SQL layer to
exactly the 5 `CallerID` values (`models.py:6`), so the per-source cap partitions into
exactly 5 independent 50-row buckets.

### Full table inventory (live, with retention class)

| Table | Rows | Retention |
|---|---|---|
| activity_log | 181 | **PRUNED** — 50/source on every MCP `log_activity` |
| system_snapshots | 39 | **PRUNED** — 10/system-family on every `save_snapshot` |
| content_index (FTS5) | 246 | Derived — GC'd with its source rows |
| pending_handoffs | 22 | Durable — status-flip (`pending→active→cleared`), never DELETEd |
| context_sections | 4 | Durable — CAS-versioned, mutable |
| context_section_export_state | 4 | Durable |
| cost_records | 6 | Durable — UNIQUE(system,month) upsert |
| session_costs / session_classification | 145 / 145 | Durable |
| shipped_sync_receipts | 14 | "Durable" **but cascade-dies with its activity row** (§4) |
| shipped_event_dispositions | 1 | Same cascade exposure |
| write_conflicts | 0 | Durable receipt ledger |

Durable-precedent patterns already in this DB: append-only receipt tables
(write_conflicts, session_costs), upsert ledgers (cost_records), and **status-flip
without deletion** (pending_handoffs). The audit JSONL (`audit.jsonl`) is append-only
by design with no rotation (`config.py:46`).

### Migration framework

`PRAGMA user_version` (no migrations table); `SCHEMA_VERSION = 12` (`db.py:15`);
engine `ensure_schema()` at `db.py:464-521`. Fresh DBs get the consolidated schema
block directly; existing DBs walk a step ladder, each step = DDL string + optional
post-hook, committed independently. Ladder v1→v12 one-liners:

- v1→v2 rename+recreate activity_log/cost_records to widen CHECKs (SQLite can't ALTER a CHECK)
- v2→v3 add `content_index` FTS5 + `repopulate_content_index` post-hook
- v3→v4 add `shipped_sync_receipts`
- v4→v5 / v5→v6 add `canonical_key` to activity_log / pending_handoffs
- v6→v7 add `source_trust` (4 tables) + operator backfill
- v7→v8 add `shipped_event_dispositions`
- v8→v9 add `session_costs`
- v9→v10 add `context_section_export_state` + `write_conflicts` (CAS + conflict receipts)
- v10→v11 data-only: reindex activity tags into FTS
- v11→v12 add `session_classification`

House pattern for a change: new `_MIGRATION` string + ladder tuple + bump
`SCHEMA_VERSION` + mirror DDL into the fresh-schema block; additive `ALTER TABLE ADD
COLUMN` for new columns; the v1→v2 rename-recreate dance only if a CHECK must change;
FTS-relevant data changes carry a post-hook. `tests/test_migration.py` (16) +
`tests/test_schema_convergence_concurrency.py` (2) gate ladder/fresh convergence.

---

## 2. Write paths (4 total — only one prunes)

| # | Path | Trigger | Prunes? |
|---|---|---|---|
| A | MCP `log_activity` → `insert_activity_row` (`tools/activity.py:224`, passes `retention_limit=50` at `:232`) | Any MCP caller | **YES** |
| B | CLI `--log-session-boundary` (`__main__.py:383-419`) | **Claude Code SessionEnd hook** — live, recurring | NO (intentional, documented in CLAUDE.md) |
| C | codex seed raw INSERT (`codex_seed.py:120-133`) | One-shot seed (guarded) | NO |
| D | markdown migration `_insert_activity` (`migration.py:249-262`) | One-time import | NO |

Prune mechanics (`db.py:722-777`): INSERT → `upsert_fts_entry` → (if `retention_limit
is not None`) the DELETE → `gc_fts_orphans(db,"activity")` — all in **one transaction**,
committed once by the tool (`activity.py:236`). The DELETE, verbatim:

```sql
DELETE FROM activity_log
WHERE source = ? AND id NOT IN (
    SELECT id FROM activity_log WHERE source = ?
    ORDER BY created_at DESC, id DESC LIMIT ?
)
```

Recency only. **No exemption for tags (SHIPPED/PROCESSED), source_trust, receipts, or
anything else.** The low-level default is `retention_limit=None` (no prune); only path A
passes the cap. `ACTIVITY_RETENTION_PER_SOURCE: int = 50` is a bare literal at
`config.py:36` — **no env override**; changing it requires a source edit.

**The cc=54 "anomaly," fully explained:** it's a sawtooth, not a stable state. Last
pruning cc write = nightly digest 2026-07-09 (id 5576) → clipped cc to exactly 50.
Four SessionEnd boundary rows (ids 5578-5581, path B) landed since → 54. The next MCP
cc write inserts row 55 and re-prunes to 50, deleting the 5 oldest cc rows in the same
transaction. codex/personal_ops have no non-pruning path, so they pin at exactly 50.

The path-B carve-out's design story: boundary rows are compressed at *read* time by
`get_activity_signal` (lifecycle aggregation, `activity.py:329-438`) instead of deleted
at write time — but compression is display-only; **boundary rows still occupy cap
slots** and evict substantive rows on the next prune.

Retention precedent elsewhere: `system_snapshots` uses the identical
DELETE-NOT-IN-newest-N idiom at 10/family (`snapshots.py:40-73`, `config.py:37`), also
paired with FTS GC. Nothing else in the DB is pruned.

---

## 3. Read paths and consumers

### Internal (bridge-db)

| Tool | Behavior | Depth exposure |
|---|---|---|
| `get_recent_activity` (`activity.py:264-310`) | Raw feed, newest first, limit 1-200 default 20, source/since filters | Window-capped |
| `get_activity_signal` (`activity.py:313-438`) | Operator feed, default 20; compresses cc lifecycle rows into aggregates (display-only) | Window-capped |
| `get_shipped_events` (`activity.py:441-586`) | All SHIPPED rows via `json_each(tags)`, **no limit**, LEFT JOINs receipts + dispositions | **Sharp** — reads live activity_log only; a pruned SHIPPED row is invisible to sync |
| `recall` (`recall.py:190-270`) | FTS5 over `content_index` (indexes project_name+summary+branch+tags) | Pruned rows GC'd from index in-transaction — gone from recall |
| `health`/`status` (`health.py:254-278`) | Newest row per source (freshness) + FTS drift metrics + un-receipted SHIPPED guard (`health.py:335,383`) | Guard only sees rows **still in the table** — a pruned SHIPPED row silently drops off the guard too |
| `export_bridge_markdown` (`export.py:122-290`) | ≤20/source into `~/.claude/projects/.../memory/claude_ai_context.md`, fully overwritten each export; auto-fires after confirm/mark (`activity.py:152-167`) | Strictly smaller/staler mirror — preserves nothing the 50-cap dropped |
| `audit_tail` (`audit.py:44`) | JSONL audit + recall query logs — not activity_log | N/A |

### External (this machine)

| Consumer | Reads | Notes |
|---|---|---|
| SessionStart warmup hook (`~/.claude/hooks/bridge-db-recall-warmup.sh:29-79`) | Direct `sqlite3 -readonly`, `project_name = basename(cwd)`, lifecycle-compressed, **LIMIT 5** | One of two wired catch-up paths |
| `/start` skill (`~/.claude/skills/start/SKILL.md:44`) | `get_recent_activity(source="cc", limit=5)` | The other wired catch-up path |
| ch10 cost-outcome logger (`~/.claude/hooks/ch10-cost-outcome-logger.py:120-158`) | Session-scoped rows by canonical_key/project; checks SHIPPED+receipt to grade session | Fail-safe on miss |
| Notion OS bridge-sync (`~/Projects/Notion/src/notion/bridge-db-mcp-client.ts:179,260`) | `get_shipped_events(unprocessed_only)` + `get_recent_activity`; writes back confirm/mark | **The loss-window victim** (§4) |
| GHRA registry + seam linter (`project_registry.py:276`, `operator_os_seam_linter.py:545`) | `SELECT DISTINCT project_name[, canonical_key]` | An idle project silently drops out of the DISTINCT set as its rows age out |
| Codex (`~/.codex/config.toml*`) | MCP `get_recent_activity` enabled | Per its limit |
| weekly-review skill (`SKILL.md:36,47`) | The exported markdown (≤20/source) | Via file, not DB |

**Verified NON-consumers:** cost-tracker (reads session_costs/session_classification
only), personal-ops (writer only), the 30-min `bridge-db-checkpoint` launchd job (WAL
checkpoint only — does not sync, does not prune).

### The catch-up answer

When the operator says "logged BridgeDB," the next agent actually sees: **5
lifecycle-compressed project-scoped entries** (warmup hook, only if `project_name`
matches the new session's cwd basename) and/or **5 newest cc rows** (`/start`). Deeper
reads require an explicit tool call, capped by the 50/source window. Nothing wired is
durable beyond that window.

---

## 4. SHIPPED → Notion Build Log pipeline + the loss window

**There is no shipped_events table.** A SHIPPED event is the string `'SHIPPED'` inside
`activity_log.tags`. The pipeline:

1. `log_activity(tags=["SHIPPED"])` — row inserted into the prunable buffer. Nothing
   durable is written at log time.
2. **Manual** `notion bridge-db sync --live` (`Notion/src/notion/bridge-db-sync.ts`;
   `npm run bridge-db:sync`): spawns bridge-db as a subprocess, calls
   `get_shipped_events(unprocessed_only=true)`, writes a Notion build_log page per row,
   then `confirm_shipped_sync` per row (Notion write before confirm → at-least-once:
   crash after write = duplicate, not loss). Dry-run is the default; schema-version
   preflight fails loud below v4.
3. `confirm_shipped_sync` (`activity.py:787-857`) appends `PROCESSED` to tags + INSERTs
   a `shipped_sync_receipts` row (activity_id UNIQUE, downstream_ref = Notion page id).
4. Alternative terminal state: `record_shipped_event_disposition` (`activity.py:692-782`),
   4 disposition types, refuses if a receipt exists. **Does not add PROCESSED** — a
   dispositioned row re-matches `unprocessed_only=true` forever; the consumer must skip
   it via `policy_disposition` (confirmed live: 1 disposition, 14 PROCESSED of 15 SHIPPED).
5. `mark_shipped_processed` is for non-SHIPPED operational rows only; guard F7
   (`activity.py:603-652`) refuses any batch containing a SHIPPED id.

**The loss window, pinned:**
- **Opens** at `log_activity` time — the SHIPPED row exists only in the prunable buffer.
- **Closes** only when the manual sync writes the Notion page. (`confirm_shipped_sync`
  does NOT protect the row; the receipt itself cascade-dies if the row is later pruned.)
- **Loss condition:** 50 newer rows *from the same source* land before sync runs. The
  row, its tag, and the health guard's visibility of it are deleted silently; the
  event never reaches Notion and leaves no trace.
- **Real-world width: unbounded.** No launchd job, hook, or orchestrator invokes the
  sync — it is entirely manual/on-demand. The prune clock advances automatically on
  every MCP write; the sync clock advances only when the operator runs it. codex and
  personal_ops sit AT the cap right now, so their windows are live and tight.
- Live state is clean (15 SHIPPED: 14 receipted + 1 dispositioned, 0 unsynced), and
  orphan receipts are structurally impossible (cascade) — which also means **absence of
  orphans proves nothing about past loss**.

**Receipt durability contradiction (new finding, verified inline):**
`shipped_sync_receipts` and `shipped_event_dispositions` both declare
`ON DELETE CASCADE` against `activity_log(id)`, and `PRAGMA foreign_keys=ON` runs on
every connection (`db.py:442`; asserted by `tests/test_db.py:63`). So when a receipted
SHIPPED row eventually rotates out of the window, its receipt is deleted too — directly
contradicting `OPERATOR-CHECKLIST.md`'s "shipped_sync_receipts is the downstream proof
ledger" framing and the docs' claim that receipts are never auto-pruned. It hasn't
bitten yet only because SHIPPED volume is low relative to the cap. The ch10
cost-outcome logger's SHIPPED+receipt session grading also depends on receipts being
present.

---

## 5. FTS5 mirror invariant

- One standalone FTS5 table `content_index (source_type UNINDEXED, source_id UNINDEXED,
  text)`, porter/unicode61 tokenizer, self-contained (not external-content).
- Mirrors 4 tables via `_FTS_SOURCE_TABLES` (`db.py:780`): context_sections('section'),
  activity_log('activity'), system_snapshots('snapshot'), pending_handoffs('handoff').
  Cost/shipped tables are not mirrored.
- **Zero SQL triggers.** The invariant is maintained entirely in application Python:
  every write path pairs base write + `upsert_fts_entry` (and every prune pairs base
  DELETE + `gc_fts_orphans`) inside the caller's single transaction. There is no window
  where a committed prune leaves stale FTS rows — but nothing *structural* enforces the
  pairing; omitting the GC call on a new delete path is how drift would start.
- Invariant currently holds perfectly: 246 FTS rows == 246 base rows, 0 orphans, all 4
  types. `collect_fts_index_metrics` (`db.py:788`) feeds health/status and treats any
  drift as a hard failure; `--rebuild-content-index` (`repopulate_content_index`,
  `db.py:882`) is the repair path.
- Ghost-result behavior: recall queries `content_index` directly, then per-hit fetches
  preview/trust from the base row (`_preview_and_trust`, `recall.py:144`). A missing
  base row yields a ghost result (real snippet, empty preview, null trust) rather than
  an error — defensive-only today.
- **Duplication hazard for any change:** the source_type→(table,pk) map exists in three
  + implicit places with no single source of truth. Adding a new mirrored table touches
  **six** hard-coded sites: `_FTS_SOURCE_TABLES` (`db.py:780`), the duplicate map in
  `gc_fts_orphans` (`db.py:702`), an `fts_text_for_<x>` builder + in-transaction upsert
  call, `repopulate_content_index` (`db.py:882`), `_preview_and_trust` (`recall.py:144`),
  and recall's scope Literal/`_VALID_SCOPES` (`recall.py:192`). Miss one and the new
  type is invisible to rebuild and to drift detection.
- Merely *exempting* rows from the activity prune is FTS-cheap: the GC is a NOT-IN
  sweep keyed to whatever survives, and health's `expected` is a plain COUNT(*), so
  the invariant math re-balances automatically. Only the DELETE predicate changes.

---

## 6. Why the 50-cap and the scope closure exist (the record we owe an answer to)

**Timeline:** `ACTIVITY_RETENTION_PER_SOURCE = 50` shipped in the repo's **first
commit** (`d0d279c`, 2026-04-14, Phase 0) alongside the 10/family snapshot cap, and has
never been edited. It was a design default, not a response to any incident.

**Scope closure:** dated decision, 2026-04-17
(`docs/internal/bridge-db-semantic-memory-IMPLEMENTATION-PLAN-v2.1.md:5-19`): "⛔ CLOSED
— Path B chosen." Empirical trigger: a 20-query eval showed **12 of 20 misses were
content-not-in-bridge.db at all** (it lived in memory files, plan docs, Notion) — a
content-scope problem no retrieval layer could fix. Decision: bridge-db stays
cross-system *state* coordination; unified recall across memory/plans/Notion is "a
separate project, not an extension of bridge-db." Echoed in CLAUDE.md:67, AGENTS.md:24,
README.md:24, OPERATOR-CHECKLIST.md:195, ROADMAP.md:156-188 ("reopen the roadmap only
if a concrete new cross-system coordination need surfaces — not to expand scope into
knowledge search").

**Rationale verdicts:**

| Candidate rationale | Verdict |
|---|---|
| (a) Keep bridge.db small | Confirmed as designed-in effect (plan v2.1 quantifies "~280 rows long-run ceiling"); refuted as trigger |
| (b) Bound the markdown export | Inferred only — no direct evidence; export is independently capped at 20/source anyway |
| (c) Prevent get_recent_activity firehose | Concern is real and named, but the remedy built for it is `get_activity_signal` compression + the no-prune SessionEnd path — **not** the 50-cap |
| (d) Separation of concerns: bridge = ephemeral state; durability lives in memory/Notion/receipts | **Strongly confirmed — the stated rationale**, repeated across six files |
| (e) FTS size/perf | Refuted — plan v2.1 argues perf is a non-issue at this scale |

**The tension was pre-resolved:** `OPERATOR-CHECKLIST.md:199-211` explicitly splits
"activity_log = recent context, not a proof ledger" from "shipped_sync_receipts = the
downstream proof ledger." The design's answer to durability already exists on paper.
**But** §4 shows that answer is structurally broken (receipts cascade-die), and it only
ever covered SHIPPED events — the operator's broader "what happened / what it does /
what it points to" record has no durable surface anywhere in the system.

**Honest framing for the design:** the scope closure forbids *semantic knowledge
search*, with evidence. A durable activity/decision ledger is arguably cross-system
*state* coordination — the exact category ROADMAP says may reopen the roadmap. The
design doesn't have to fight the closure; it has to (1) answer rationale (d) — why the
existing durability surfaces (Notion, memory files, receipts) are insufficient for the
catch-up use case, and (2) either fix or explicitly supersede the broken receipt
ledger.

**INV registry:** there is **no** numbered INV-* registry in bridge-db (repo-wide grep
empty). "INV-13" is Fable Rigor Campaign numbering (external catalog). The only named
invariant in-repo is the unnumbered FTS5 write-path invariant (CLAUDE.md:33). If the
design wants an enforceable retention invariant, it will be authoring the first.

---

## 7. INV-13 — clear_handoff ownership bug (PARKED — documented, not fixed)

Confirmed real, two halves (`src/bridge_db/tools/handoffs.py` — note: `tools/` package,
not the previously-cited top-level path):

- **(a) Role gate, not claimant gate** (`handoffs.py:295-356`): the entire clear
  authorization is `caller in ("cc","codex")`; it then UPDATEs **every** non-cleared
  row matching project_name (or shared canonical_key) to `cleared`. No check of who
  picked the handoff up. Under auth modes `off`/`warn`, the caller string is
  self-asserted besides.
- **(b) The claimant identity doesn't exist in the schema**: `pending_handoffs` has no
  `claimed_by`-style column; `pick_up_handoff` records only `picked_up_at` + status
  (`handoffs.py:231-234`). The claimant's identity goes to the audit JSONL only.
  Even a willing fix has nothing to gate on without a schema change (v13 migration).
- **Misfire shape:** any cc/codex session can clear another agent's active claim
  (cross-directional), and a clear also wipes still-`pending` rows nobody claimed.
  Contrast: the pending→active transition is heavily guarded (source_trust gate,
  principal-bound identity, TOCTOU CAS + write_conflicts receipt, `handoffs.py:189-270`).
  Hardening stopped one transition short.
- **Test landscape warning:** `tests/test_handoffs.py::test_handoff_lifecycle_across_pending_pickup_and_clear`
  (L438) *actively locks in* non-claimant clearing (picks up as one caller, clears as
  `codex` at L472). An ownership fix breaks that test by design and requires the new
  claimant column.

---

## 8. Change-surface inventory (what a retention/ledger change will touch)

Facts only — mechanism is for the design session.

- **The prune predicate:** one DELETE at `db.py:765-774`; exemptions = edit that SQL.
  FTS GC + health metrics rebalance automatically (§5). The cap constant
  (`config.py:36`) has no env override.
- **A new durable table:** house migration pattern (§1) + the six FTS sites (§5) if it
  should be recall-searchable + `export_bridge_markdown` if it should appear in the
  markdown + surfacing changes (warmup hook / `/start` / `get_activity_signal`) if the
  next agent should actually see it.
- **Receipt cascade:** `ON DELETE CASCADE` on shipped_sync_receipts +
  shipped_event_dispositions is a schema-level decision to revisit (SQLite FK changes
  require the rename-recreate dance, precedent v1→v2).
- **Boundary-row pollution:** cc telemetry and substantive rows share one cap; any
  "important rows survive" rule must decide whether boundary rows count against it.
- **Tests:** primary break/extend sites `tests/test_activity.py` (41 tests, prune
  behavior), `test_db.py` (27, insert+GC), `test_recall.py` (18), `test_migration.py`
  (16) + convergence (2), `test_handoffs.py` (31, INV-13 lands here). 334 tests total;
  gates `uv run pytest` / `uv run pyright` (strict) / `uv run ruff check` (local +
  Codex verify contract; the GitHub workflow does not run them).
- **Backfill reality:** none possible. Pruned history is gone (id range 1414-5581 with
  181 survivors ≈ ~4,200 rows deleted over the DB's life, tags included). The ledger
  starts from adoption day.
- **External blast radius:** Notion sync preflights `MIN_BRIDGE_DB_SCHEMA_VERSION=4`
  and spawns its own bridge-db subprocess — schema bumps are tolerated; behavior
  changes to `get_shipped_events` semantics are the sensitive seam. GHRA reads DISTINCT
  columns only. cost-tracker/personal-ops untouched.

---

## 9. Open questions the design must answer

1. **Buffer vs ledger, on the record.** The system's documented answer to durability is
   "receipts + Notion + memory files." Why is that insufficient for the operator's
   catch-up use case? (It is — receipts cover only SHIPPED, cascade-die, and nothing
   wired surfaces history — but the design doc owes the explicit argument against
   rationale (d).)
	What is your rec to this question?
	**Rec (CC):** Keep activity_log the buffer and don't relitigate rationale (d)
	wholesale. The on-record argument is narrow: the catch-up record is cross-system
	*coordination state* — bridge-db's own mission — and none of the three designated
	durability surfaces serve it (Notion covers SHIPPED only, manual, off-machine;
	memory files are CC-private, invisible to codex/claude_ai; receipts cover SHIPPED
	only and cascade-die). ROADMAP's own reopening clause ("concrete new cross-system
	coordination need") covers this exactly. Extend retention inside the existing
	state-coordination scope; no knowledge-store expansion.
2. **Fix or supersede the receipt ledger?** The CASCADE makes today's "proof ledger"
   non-durable. Does the design repair receipts (drop cascade / snapshot content), fold
   them into the new durable surface, or both?
	What is your rec to this question?
	**Rec (CC):** Fix by protecting the parent, not rewiring the child. Once SHIPPED
	rows are prune-exempt (Q3/Q4), the cascade can never fire in practice, and it
	remains a *correct* integrity rule (a receipt shouldn't outlive its row). No FK
	surgery, no rename-recreate dance, no parallel receipt store. Add the Q10 health
	check so receipts reconcile against receipted rows forever.
3. **What is "important"?** Tag-based (SHIPPED+)? `source_trust='operator'`? An explicit
   ledger flag at log time? Everything-non-boundary? The answer defines the prune
   exemption predicate and/or the new table's write trigger.
	That's exactly what I'm trying to figure out. What is important? Obviously, shipped is important, but there's a lot of things that can be important.
	The answer to this is this exercise we're doing. I feel like we're starting to lean into a far larger rewrite or redo of this project and this repo itself than what we're looking at. This could definitely expand. 
	**Rec (CC):** Put the importance decision at write time, not in a classifier:
	protected = any row whose tags include `SHIPPED` or a new ledger tag (name TBD)
	that agents attach when the operator says "log this to BridgeDB." Optionally also
	`source_trust='operator'`. Everything else (boundaries, sweeps, telemetry) stays
	in the rolling window. On the scope worry: agreed, and the audit itself is the
	counter-evidence — the minimal coherent footprint is one prune-predicate edit +
	one tag convention + one prune audit line + a surfacing tweak + doc updates + the
	INV-13 column, all inside existing architecture. No new table, no rewrite, no
	knowledge-store creep. If the design grows past that list, that's the tripwire to
	stop and re-scope.
4. **Where does the durable ledger live?** Exempt-in-place (edit one DELETE; rows stay
   recall-searchable for free; table grows) vs. new table (clean separation; six FTS
   sites + migration + new surfacing) vs. status-flip pattern à la pending_handoffs.
   Each has a different answer to the scope-closure reconciliation (§6).
	What is your rec to this question?
	**Rec (CC):** Exempt-in-place. One DELETE-predicate edit; protected rows stay
	recall-searchable for free; FTS GC and health metrics rebalance automatically
	(§5); avoids the six-site new-table tax; and the scope-closure story stays clean
	("same table, same mission, smarter retention"). With Q8 answered "the more the
	merrier," size math makes in-place growth a non-issue.
5. **How does it reach the next agent?** Catch-up today is 5-deep and cwd-keyed. A
   ledger nobody surfaces is a write-only archive. Which of warmup hook / `/start` /
   `get_activity_signal` / a new tool carries it, and at what depth?
	What is your rec to this question?
	**Rec (CC):** No new tool. Extend `get_activity_signal` with a pinned "ledger"
	section (protected rows, newest N), and bump the two wired surfacers: `/start`
	additionally pulls protected rows (~10), and the warmup hook includes the
	project's protected rows regardless of recency. Keep wired depth small (5-10);
	recall covers the long tail since protected rows never leave FTS.
6. **Close the SHIPPED pre-sync window by which clock?** Exempt unsynced SHIPPED from
   prune (retention-side), write the durable record at log time (write-side), and/or
   schedule the Notion sync (sync-side). Note the sync is manual today and dry-run by
   default.
	What is your rec to this question?
	**Rec (CC):** Retention-side is the structural fix — SHIPPED prune-exemption
	closes the window permanently (synced or not) with zero new infra. Sync-side then
	becomes hygiene rather than survival: surface "unsynced SHIPPED count" in
	health/worklist, optionally a scheduled reminder. Skip write-side entirely — it's
	redundant once retention-side lands.
7. **Boundary telemetry vs substantive rows:** separate caps? Exclude boundaries from
   the ledger? cc's effective substantive history is ~19 rows today.
	What is your rec to this question?
	**Rec (CC):** Phase-split. Phase 1 (this design): boundaries are simply never
	protected — once important rows stop competing with telemetry for slots, the
	eviction problem this audit exists for is solved. The residual crowding of the
	*unprotected* window (~19 substantive cc rows) is real but transient-by-design;
	if it still hurts after phase 1, phase 2 is a two-bucket cap (boundaries capped
	separately/tighter) at the same code site, no schema. Don't block the train on it.
8. **Retention for the durable surface itself:** truly unbounded (audit-JSONL
   precedent) or a much larger cap? Size math is trivial either way (~280-row current
   ceiling; even 10k prose rows ≈ a few MB) — but the answer should be explicit.
	The more the merrier
9. **Observability of deletion:** should any future prune emit a receipt/audit line
   (count + ids + tags), so silent loss can't recur unobserved?
	Yes
10. **An enforceable invariant:** does the design author bridge-db's first numbered
    invariant ("no protected row is ever deleted by retention") plus a health check,
    so the guarantee outlives the implementation?
	What is your rec to this question?
	**Rec (CC):** Yes — author it as the repo's first numbered invariant (suggest
	BD-INV-1: "retention never deletes a protected row, its receipt, or its
	disposition"), enforced three ways: the prune predicate itself, a health check
	(protected rows older than the newest-50 exist; receipts reconcile), and the Q9
	prune audit line asserting zero protected ids in every deleted set. That's what
	makes the guarantee outlive the implementation, and it anchors the Q12 doc
	updates.
11. **INV-13 sequencing:** the handoff fix needs a v13 migration (claimant column) and
    breaks a green test by design. Same migration train as the ledger change, or
    separate? (Parked, but the design should state the order.)
	Same train, 
12. **Doc reconciliation:** CLAUDE.md, README, AGENTS.md, ROADMAP, OPERATOR-CHECKLIST
    all state the buffer/ledger split and scope closure. Whatever ships must update
    those declarations in the same change — the audit found them internally consistent
    and load-bearing; leaving them stale would recreate exactly the mental-model gap
    that caused this audit.
	Agreed

---

## 10. Corrections to the prior session's findings

- "Auto-prunes on every write" → only the MCP tool prunes; 3 of 4 write paths don't.
  cc=53 → now 54, and it's a sawtooth (SessionEnd inserts without pruning; next MCP cc
  write re-clips to 50).
- "SHIPPED's only durable copy is downstream in Notion via receipts" → sharper:
  receipts themselves cascade-delete with the pruned row (verified: FK + `PRAGMA
  foreign_keys=ON` at `db.py:442`). Notion pages are the *only* durable copy.
- Loss window "narrow but real" → mechanism as described, but **unbounded in time**:
  the Notion sync has no scheduler; it is manual-only. Two sources sit at cap now.
- INV-13 file location: `src/bridge_db/tools/handoffs.py` (tools/ package), not
  `src/bridge_db/handoffs.py`. Bug confirmed as described, plus: a green test actively
  locks in the buggy behavior, and no INV-* registry exists in-repo.
- Live totals: 181 rows (not 180); claude_ai=14, notion_os=13 (not at cap).

---

## 11. Impact audit (second wave, 2026-07-10) — plan revisions

Operator approved the plan direction; a second five-agent wave stress-tested each
component against the whole estate (~/Projects/Notion, ~/Projects/personal-ops,
~/Projects/cost-tracker, ~/Projects/GithubRepoAuditor, ~/.claude, ~/.codex,
~/Projects/operator-os-docs). Baseline verifiers on main: **345 pytest passed (2.6s),
ruff clean, pyright 0 errors.**

**Verdict: nothing in the estate hard-BREAKS under the plan. The revisions below are
required to ship it without rot.**

### R1 — Prune predicate (db.py:764-774)
- Keep-set: **Interpretation A** — `newest-50 ∪ protected` via one added conjunct
  (`AND NOT EXISTS (SELECT 1 FROM json_each(tags) WHERE upper(value) IN (...))`).
  A vs B ("50 unprotected ∪ protected") is provably invisible to every live consumer
  (all readers take newest-N ≤ 50); they differ only in deep unprotected history. A is
  the minimal reviewable diff. Revisit trigger: protected volume within a source's
  newest-50 becomes large (today ~13 SHIPPED/month estate-wide).
- **Case-insensitivity is mandatory** — every existing matcher is exact-case; a
  lowercase `'ledger'` from any writer would be silently pruned. Match with
  `upper(value)` (or normalize at write time; nothing normalizes today).
- **Protected-vocabulary decision (OPERATOR INPUT REQUIRED):** SHIPPED-only protection
  is a complete no-op for personal_ops and notion_os (zero SHIPPED rows, structurally
  always — they emit REVIEW_CLOSED/APPROVAL_SENT/TASK_DONE/PLANNING_APPLIED/bridge-sync).
  Rec: protect `SHIPPED` + the new ledger tag ONLY; make the ledger tag the universal
  opt-in and update writer conventions — do not hardwire other systems' tag names into
  the predicate. Swings protection from ~15 rows to ~50+ if widened.
- No index needed (json_each per ~50 rows/source is trivial; tags has no index).
- Tag-validation reality: there is NONE (no allowlist, no CHECK, no normalizer, no
  hook). Live vocabulary already drifted — 30 distinct tag values; "retired" tags
  dominate (HYGIENE 44, REVIEW_CLOSED 34, MAINTENANCE 23). Nothing chokes on unknown
  tags; adding a ledger tag costs zero at the write path.

### R2 — get_shipped_events hardening (ship WITH the exemption or the sync rots)
- **Dispositions must leave the feed:** `record_shipped_event_disposition` doesn't add
  PROCESSED, so dispositioned rows re-match `unprocessed_only=true` forever (eternal
  under the plan). Fix server-side: `unprocessed_only` also excludes rows with a
  disposition (mirror health's `actionable` query, health.py:150-158). Notion skips
  them cleanly today (bridge-db-sync.ts:203-216) — noise, not corruption — but the
  skip-set grows unboundedly per run.
- **Honor the limit the client already sends:** the tool has NO limit param; the Notion
  client passes `limit` (default 50) which FastMCP **silently drops** (no
  extra='forbid'). Add a real `limit` (+ document `since`). Today masked by prune;
  eternal rows unmask it.
- **Memoize `_load_meta_shipped_event_policy`** (activity.py:123-150): currently a full
  file read + JSON parse **per returned row** (mtime-cache it like project_resolver).
- Notion tests are fully mocked (no break), but assert limit-forwarding values — update
  alongside (mcp-client.test.ts:111,120,161).

### R3 — Prune audit line mechanics
House convention is audit-AFTER-commit (activity.py:236→238 et al.), and
insert_activity_row doesn't commit. Plumb deleted `(count, ids, tags)` out of
insert_activity_row; `log_activity` emits `log_audit("log_activity.prune", ...)`
post-commit. Capture via `DELETE ... RETURNING` (verify bundled SQLite ≥ 3.35) or a
pre-DELETE SELECT on the same predicate.

### R4 — Health/dogfood behavior change (accept + document)
Retention currently gives health amnesia: an ignored unsynced SHIPPED row prunes away
and its nag vanishes. Under the plan `actionable_unprocessed_shipped` persists until a
receipt or disposition exists, and `--dogfood` (\_\_main\_\_.py:295-300) stays red until
then. This is BD-INV-1 working as intended — every SHIPPED write now carries a terminal
obligation (confirm or disposition) — but it is a real workflow change; document it.
BD-INV-1 home: there is no check registry — add its metric inline in
`collect_health_metrics` beside the shipped counts and gate it in `run_dogfood`.

### R5 — get_activity_signal shape constraint
Returns a FLAT `list[dict]`; the test suite indexes `entry["kind"]`; no other
programmatic consumer exists anywhere (Codex hasn't even wired the tool). Pinned ledger
rows = same flat list, new `kind: "ledger"`, prepended. Do NOT wrap in a dict.

### R6 — Surfacing implementation constraints
- Warmup hook (~/.claude/hooks/bridge-db-recall-warmup.sh) queries sqlite directly (not
  MCP): the ledger pull is a **capped** SQL UNION branch (newest 5-10 protected for the
  project) — uncapped "regardless of recency" balloons cold-start injection on
  SHIPPED-heavy projects. Edit-tool only (shell writes to ~/.claude/hooks are
  guard-blocked); hook is ADVISORY in the integrity manifest — operator re-runs
  `hook-integrity-regen.sh` after the edit or every session start nags.
- /start skill: protected pull slots after the existing get_recent_activity call
  (SKILL.md:44); share the same small N; PROVENANCE.json must exist (pre-existing skill,
  should be fine).
- export_bridge_markdown has no ledger concept: mirror a "## Pinned Ledger" section or
  explicitly declare the ledger live-read-only (low severity, consistency only).

### R7 — INV-13 revisions (the biggest deltas from the original sketch)
- **Claim site:** write `claimed_by` in `pick_up_handoff`'s CAS UPDATE
  (handoffs.py:229-236), value = `get_principal(ctx) or caller` (the existing
  gate_identity).
- **Clear semantics (load-bearing):** strict `claimed_by == caller` BREAKS the live
  skill flow — `/finish` and `/bank` clear opportunistically by project_name via
  bridge-log-protocol.md, usually from sessions that never claimed (and the docstring
  documents "opportunistic" clears of rows that may not exist). Rec: **gate `active`
  rows on claimant; still allow clearing unclaimed `pending` rows.** Fixes the real
  theft case without breaking /finish, and avoids stranding.
- **Granularity honesty:** all cc windows share ONE token → principal "cc"; there is no
  session identity anywhere in the auth layer (CallerID is a closed 5-value literal).
  Claimant-gating meaningfully protects **cross-role only (cc↔codex)**; cc↔cc remains
  role-gated until a session-identity primitive exists — out of scope, say so.
- **Auth posture ceiling:** live mode is `warn` (CC via ~/.claude.json; default in
  config.py:84 is `off`), so the caller string is self-asserted — the fix buys
  **accident-safety**, not adversarial protection (that rides the enforce flip, still
  HOLD per readiness). Worth doing; don't oversell.
- **No doctrine escape hatch needed:** peer-agent takeover is branch-as-lease +
  log_activity receipts by design (doctrine :41-47 explicitly disavows the gated
  handoff tools); clear_handoff isn't in that flow.
- **Stale reality:** no expiry/reap exists (health staleness is advisory-only). A
  claimant gate with no reap can strand orphaned `active` rows (22 live pending rows
  include old ones). Pair with a reap or explicitly defer it.
- **Test blast (corrected):** 3-4 clear_handoff tests break (not 1):
  `test_clear_handoff_by_project_name`, `test_clear_handoff_clears_all_matching_rows`
  (cleared_count 2→1), likely both canonical-alias clear tests; the lifecycle test
  previously flagged actually SURVIVES a role-valued gate (same-role clear). Plus new
  tests: cross-role refusal, unclaimed-pending clears, stale-strand behavior.
- v13 migration: three lockstep edits — SCHEMA_VERSION 12→13 (db.py:15), ladder tuple +
  `_MIGRATION_V12_TO_V13` ALTER, AND mirror the column in the fresh-schema
  pending_handoffs block (db.py:93-105) — or `test_fresh_vs_migrated_schema_convergence`
  fails. No external consumer pins a max schema version; Notion's MIN=4 gate tolerates
  v13. The retention change itself is pure code, no DDL.

### R8 — Doc/convention updates (expanded list)
In-repo: `log_activity` Field desc + docstring (activity.py:186-217 — still advertises
the five retired tags; highest priority, it's the live API contract), CLAUDE.md,
README.md, AGENTS.md, ROADMAP.md, docs/internal/OPERATOR-CHECKLIST.md,
integration-spec.md. Out-of-repo: ~/.claude/rules/agent-division.md (tag vocabulary —
already fiction vs live data), warmup hook + /start skill (R6), optional
~/.codex/config.toml if Codex should see get_activity_signal.

### Incidental findings (not this train, log separately)
- `vibe-code-handoff/SKILL.md:225-228` calls create_handoff with caller "cc" — a
  pre-existing dead path (code rejects non-claude_ai creators); falls back to a tag.
- Tag vocabulary drift: "retired" tags dominate live rows; nothing enforces the
  documented vocabulary anywhere.
- `source_trust_clamped` leaked into one live row's tags (response field written as a
  tag by some writer).
