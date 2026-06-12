# bridge-db `source_trust` Governance Control — Implementation Plan

Feature addition to the existing `bridge-db` repo (`/Users/d/Projects/bridge-db`). **Not
greenfield** — it adds a provenance/integrity dimension to the cross-provider handoff store. It is
the first concrete instance of the output-side taint model (#2) and it closes the CRITICAL path
(A1) from the cross-provider governance red-team (#4): an untrusted-origin handoff being picked up
and executed by Codex with `danger-full-access`.

Grounded in the real code: `db.py` (`SCHEMA_VERSION = 6`, `_SCHEMA_DDL`, the `_MIGRATION_Vx_TO_Vy`
ladder driven by PRAGMA `user_version`), `tools/handoffs.py` (`create_handoff` claude_ai-only;
`pick_up_handoff` cc/codex-only — the gate point), `models.py` (`CallerID` = a `Literal` type).

---

## Section 1: EXEC SUMMARY

### 1a. What we're building
A `source_trust` provenance label on every instruction-bearing row in bridge-db
(`pending_handoffs`, `activity_log`, `context_sections`, `system_snapshots`), with values
`operator | agent | ingested`. Writers set it (conservative default `agent`); `pick_up_handoff`
**gates** on it — a non-`operator` handoff cannot transition to `active` without an explicit
operator confirmation, with Codex held to the strict path. `get_pending_handoffs`, `recall`, and
`status` surface the label so `/start` and the operator approval view show provenance. This makes
the peer-trust blind spot visible and breaks the laundering path where untrusted content becomes
trusted operator intent for the next provider.

### 1b. Riskiest parts and de-risking strategy
- **R1 — schema migration on the live canonical DB (HIGH).** Why: bridge-db is the source of truth; a bad migration corrupts cross-provider state. Mitigation: follow the existing versioned ladder exactly — bump `SCHEMA_VERSION` to 7, add `_MIGRATION_V6_TO_V7` as additive `ALTER TABLE ... ADD COLUMN` (no rename/recreate needed since we add, not alter), keep it idempotent, and add a `test_migration` case that runs v6→v7 on a populated fixture DB. Fallback: ADD COLUMN with a plain default and enforce the enum at the application layer if the column-level CHECK proves awkward on ADD COLUMN.
- **R2 — backfill mislabels history (MEDIUM).** Why: existing rows have no provenance; a blind default could mark genuinely-untrusted history as safe. Mitigation: conservative backfill — `context_sections` (owner-authored) → `operator`; existing `pending_handoffs` → `operator` (operator-dispatched historically); `activity_log` / `system_snapshots` → `agent`. Document the assumption. Fallback: backfill everything to `agent` (most conservative) and let the operator promote.
- **R3 — the human gate has no true out-of-band channel in an MCP tool (MEDIUM).** Why: `pick_up_handoff` runs inside the agent; there is no separate human prompt. Mitigation: model the gate as a two-call protocol — a non-`operator` pickup returns `requires_confirmation` without transitioning state; the operator re-invokes with `confirm=True`. The agent must surface the block. Fallback: for `codex` callers, refuse non-`operator` pickup entirely until promoted, rather than offering inline confirm.

### 1c. Shortest path to value
Ship Phase 0 + Phase 2 first (schema + the pickup gate) — that alone closes A1. Phases 1 and 3
(writer ergonomics + surfacing) complete the provenance story but the gate is the security win.

---

## Section 2: REVIEW GATE (SPEC LOCK)

### 2a. Goal
No non-`operator` handoff reaches `active` without explicit operator confirmation, and provenance
is visible everywhere a provider reads the store.

### 2b. Success metrics
- A handoff with `source_trust='agent'` → `pick_up_handoff` returns `requires_confirmation` and the row stays `pending`; same call with `confirm=True` transitions to `active`.
- A handoff with `source_trust='operator'` picks up in one call (today's behavior preserved).
- `caller='codex'` on a non-`operator` handoff takes the strict path (refuse-until-promoted).
- `get_pending_handoffs`, `recall`, and `status` each include `source_trust`.
- v6→v7 migration runs idempotently on a populated fixture DB; fresh-create DB has the column; full `uv run pytest` green.

### 2c. Hard constraints
- Additive, backward-compatible: existing tool signatures keep working (new params default; existing callers unaffected except the new gate).
- Follow the existing schema-version ladder; no destructive table rewrites.
- Single-writer discipline unchanged; no new dependencies.
- The label lives in the DB row only — never in the markdown export (which launders it).

### 2d. Locked decisions
- **Label values:** `operator | agent | ingested` as a `Literal` type `SourceTrust` in `models.py` (mirrors `CallerID`). Rationale: matches the codebase's typing pattern; three levels map to the red-team's origin classes.
- **Default on write:** `agent`. Rationale: a Claude-dispatched handoff is agent-authored unless the operator asserts otherwise — conservative by default.
- **Gate semantics:** two-call confirm for `cc`; refuse-until-promoted for `codex`. Rationale: Codex is the highest-severity sink (A1), so it gets the stricter rule.
- **Enforcement column on:** `pending_handoffs` is the gated table; the other three carry the label for surfacing/recall only (no gate). Rationale: pickup is the dangerous transition.
- **Export boundary:** `source_trust` is never serialized into the markdown bridge export, only read from the DB row. Rationale: the markdown is a regenerated projection; writing the label there would let a provider read trust state from a source that flattens provenance (the laundering vector). The DB is the only authority for trust, consistent with the canonical-store discipline.
- **Who sets `ingested`:** a writer marks `ingested` when the content derives from untrusted external input (a fetched page, a scanned repo file, an upstream tool output). It is the strongest signal and always takes the strict path at pickup. Rationale: makes the highest-risk provenance explicit rather than collapsing it into `agent`.

---

## Section 3: ARCHITECTURE

### 3a. System diagram
```
writer tools (create_handoff / log_activity / update_section / save_snapshot)
        │ set source_trust (default 'agent')
        ▼
DB rows (pending_handoffs | activity_log | context_sections | system_snapshots)
        │
        ├─▶ get_pending_handoffs / recall / status  → surface source_trust
        └─▶ pick_up_handoff (cc|codex)  → GATE: non-operator ⇒ requires_confirmation / refuse(codex)
```

### 3b. Tech stack
- Python 3.12+ (repo `.python-version`), `aiosqlite`, FastMCP — all existing.
- `pytest` via `uv run pytest` — existing runner.
- No new dependencies.

### 3c. File structure
```
src/bridge_db/db.py              # EDIT — SCHEMA_VERSION 6→7; _SCHEMA_DDL adds source_trust; _MIGRATION_V6_TO_V7
src/bridge_db/models.py          # EDIT — SourceTrust Literal type
src/bridge_db/tools/handoffs.py  # EDIT — create_handoff param; pick_up_handoff GATE; get_pending_handoffs surface
src/bridge_db/tools/activity.py  # EDIT — log_activity source_trust param
src/bridge_db/tools/context.py   # EDIT — update_section source_trust param
src/bridge_db/tools/snapshots.py # EDIT — save_snapshot source_trust param
src/bridge_db/tools/recall.py    # EDIT — include source_trust in hits
src/bridge_db/tools/health.py    # EDIT — status counts by source_trust
src/bridge_db/audit.py           # EDIT — record gate decisions (consumed, not redefined)
tests/test_migration.py          # EDIT — v6→v7 case
tests/test_db.py                 # EDIT — fresh-create has column
tests/test_handoffs.py           # EDIT — the gate (the crux)
tests/test_recall.py             # EDIT — surfacing
README.md / docs                 # EDIT — provenance model + CHANGELOG
```

### 3d. Data model
The change to persistence: add `source_trust TEXT NOT NULL DEFAULT 'agent'` (with a column CHECK
`source_trust IN ('operator','agent','ingested')` where ADD COLUMN permits it; else app-layer
enum) to the four instruction-bearing tables. `_SCHEMA_DDL` gains the column for fresh DBs;
`_MIGRATION_V6_TO_V7` adds it + backfills existing rows per the R2 strategy. `SCHEMA_VERSION → 7`.
No table rewrites — additive ALTER only.

Concrete migration sketch (`_MIGRATION_V6_TO_V7`):
```sql
-- additive column on each instruction-bearing table
ALTER TABLE pending_handoffs  ADD COLUMN source_trust TEXT NOT NULL DEFAULT 'agent'
    CHECK(source_trust IN ('operator','agent','ingested'));
ALTER TABLE activity_log      ADD COLUMN source_trust TEXT NOT NULL DEFAULT 'agent'
    CHECK(source_trust IN ('operator','agent','ingested'));
ALTER TABLE context_sections  ADD COLUMN source_trust TEXT NOT NULL DEFAULT 'agent'
    CHECK(source_trust IN ('operator','agent','ingested'));
ALTER TABLE system_snapshots  ADD COLUMN source_trust TEXT NOT NULL DEFAULT 'agent'
    CHECK(source_trust IN ('operator','agent','ingested'));

-- conservative backfill of pre-existing rows (R2)
UPDATE context_sections  SET source_trust = 'operator';   -- owner-authored context
UPDATE pending_handoffs  SET source_trust = 'operator';   -- historically operator-dispatched
-- activity_log / system_snapshots keep the 'agent' default (agent-recorded)
```
Notes for the implementer: SQLite `ADD COLUMN` permits a column-level `CHECK` as long as the
constant `DEFAULT` satisfies it (it does), so no rename/recreate is required — unlike the v1→v2
migration which rewrote tables to *change* existing CHECKs. The migration must run inside the same
versioned runner that applies `_MIGRATION_V2_TO_V3` etc., guarded on `PRAGMA user_version < 7`, and
must be idempotent (the runner already gates each step on the version, so a re-run is a no-op).
After the ALTERs, bump `user_version` to 7. The FTS `content_index` is unaffected (the label is
`UNINDEXED` metadata, not searchable text), so no `repopulate_content_index` is needed.

### 3e. Type definitions
```python
# models.py
SourceTrust = Literal["operator", "agent", "ingested"]
```
```python
# tools/handoffs.py — pick_up_handoff gains:
confirm: Annotated[bool, Field(description="Operator confirmation to pick up a non-operator-trust handoff")] = False
# returns, when gated:
{"ok": False, "requires_confirmation": True, "handoff_id": id,
 "source_trust": "agent", "reason": "non-operator-trust handoff; re-invoke with confirm=True"}
```

### 3f. API contracts
Not applicable for external APIs — bridge-db is a local MCP server. The internal tool contract
changes are additive: new optional params with defaults; one new gated return shape on
`pick_up_handoff` for the confirmation path.

### 3g. Dependencies
No new dependencies:
```bash
uv sync
uv run pytest -q
```

---

## Section 4: PHASED IMPLEMENTATION

### Phase 0: Schema + type foundation (Week 1, ~3h)
**Objective:** the `source_trust` column on all four tables + the `SourceTrust` type, via the
versioned migration ladder. No tool-behavior change.
**Tasks:**
1. Add `SourceTrust = Literal["operator","agent","ingested"]` to `models.py`. — Acceptance: `from bridge_db.models import SourceTrust` imports; mypy/type check passes.
2. Bump `SCHEMA_VERSION` 6→7; add the `source_trust` column to all four tables in `_SCHEMA_DDL` (fresh-create path). — Acceptance: a fresh DB created from `_SCHEMA_DDL` has `source_trust` on `pending_handoffs`, `activity_log`, `context_sections`, `system_snapshots` with default `agent`.
3. Add `_MIGRATION_V6_TO_V7`: `ALTER TABLE ... ADD COLUMN source_trust ...` for each table, then backfill (`context_sections`→`operator`; existing `pending_handoffs`→`operator`; `activity_log`/`system_snapshots`→`agent`); wire it into the migration runner. — Acceptance: `test_migration.py` runs v6→v7 on a populated v6 fixture DB idempotently; re-run is a no-op; backfill values match the strategy.
**Verification checklist:**
- [ ] `uv run pytest tests/test_db.py tests/test_migration.py -q` → green
- [ ] Fresh DB `PRAGMA user_version` == 7; populated v6 DB upgrades to 7 without data loss
- [ ] Re-running the migration is idempotent (no duplicate column / no error)
**Parallel Dispatch Proposal (≥3 disjoint tasks):**
- Dispatchable in parallel: Task 1 (type), and the `_SCHEMA_DDL` edits (Task 2) vs the migration fixture authoring (part of Task 3) once the column spec is fixed.
- Subagent type: coder
- Rationale: the type and the fresh-create DDL are independent; the migration consumes the agreed column spec.
**Risks:**
- ADD COLUMN with CHECK rejected: Mitigation → plain default + app-layer enum validation → Fallback: rename+recreate per the existing v1→v2 pattern.
- Backfill mislabels: Mitigation → conservative per-table rule + documented assumption → Fallback: all `agent`.
**Phase-end review:** Run `/ultrareview`. Address all findings before marking the phase complete.

### Phase 1: Writer defaults (Week 1, ~3h)
**Objective:** writers can set provenance; conservative defaults persist it.
**Tasks:**
1. Add `source_trust: SourceTrust = "agent"` to `create_handoff` and persist it in the INSERT. — Acceptance: `create_handoff(..., source_trust="operator")` stores `operator`; omitting it stores `agent`; `test_handoffs.py` asserts both.
2. Add the same param + persistence to `log_activity`, `update_section`, `save_snapshot`. — Acceptance: each writer stores the provided/default value; existing tests still pass (params are optional).
3. Record the chosen `source_trust` in the audit log on `create_handoff`. — Acceptance: `log_audit` entry carries the trust level (asserted in `test_audit`/`test_handoffs`).
**Verification checklist:**
- [ ] `uv run pytest tests/test_handoffs.py tests/test_activity.py tests/test_context.py tests/test_snapshots.py -q` → green
- [ ] Omitting `source_trust` on every writer yields `agent` (backward compatible)
**Parallel Dispatch Proposal (≥3 disjoint tasks):**
- Dispatchable in parallel: the four writer edits (Tasks 1–2 across `handoffs.py`, `activity.py`, `context.py`, `snapshots.py`) are independent files.
- Subagent type: coder
- Rationale: separate tool modules, no shared state beyond the column added in Phase 0.
**Risks:**
- Param ordering breaks callers: Mitigation → append as keyword-only with default → Fallback: keep positional signature stable.
**Phase-end review:** Run `/ultrareview`. Address all findings before marking the phase complete.

### Phase 2: The pickup gate (Week 1–2, ~4h) — the security win
**Objective:** `pick_up_handoff` enforces provenance; non-`operator` requires confirmation; Codex
takes the strict path.
**Tasks:**
1. Read `source_trust` in `pick_up_handoff`; add a `confirm: bool = False` param. If trust != `operator` and not `confirm`: return `{ok: False, requires_confirmation: True, source_trust, reason}` and do NOT transition state. — Acceptance: `test_handoffs.py` — agent-trust pickup without confirm stays `pending` and returns `requires_confirmation`; with `confirm=True` → `active`.
2. Strict path for `caller='codex'`: a non-`operator` handoff is refused (not confirmable inline) with guidance to promote the handoff to `operator` first. — Acceptance: `codex` + agent-trust → `ToolError`/refusal, row stays `pending`; `codex` + operator-trust → `active` in one call.
3. Record the gate decision (allowed / confirmation-required / refused) in the audit log. — Acceptance: each path logs a distinct audit event (asserted).
**Verification checklist:**
- [ ] `uv run pytest tests/test_handoffs.py tests/test_audit.py -q` → green
- [ ] `operator`-trust handoff: pickup unchanged from today (one call) for both `cc` and `codex`
- [ ] No state transition on any gated/refused path
**Parallel Dispatch Proposal (≥3 disjoint tasks):**
- Not parallelizable beyond test authoring vs gate logic: the gate is one function. Dispatch as a single focused task with its tests; (a) gate logic, (b) cc-confirm tests, (c) codex-strict tests can be authored concurrently.
- Subagent type: coder
- Rationale: one code site, but three independent test scenarios.
**Risks:**
- Confirm protocol confuses callers: Mitigation → explicit `reason` + `requires_confirmation` flag in the return → Fallback: document the two-call protocol in the tool docstring.
- Operator-trust handoffs accidentally gated: Mitigation → assert the operator-trust fast path in tests → Fallback: feature-flag the gate.
**Phase-end review:** Run `/ultrareview`. Address all findings before marking the phase complete.

### Phase 3: Surfacing + docs (Week 2, ~3h)
**Objective:** provenance is visible wherever a provider reads the store.
**Tasks:**
1. Add `source_trust` to `get_pending_handoffs` output and to `recall` hit records. — Acceptance: both include the field; `test_recall.py` asserts a recalled handoff/activity carries its trust.
2. Add a `source_trust` breakdown (counts by level) to `status`/`health`. — Acceptance: `status` shows e.g. `{operator: n, agent: m, ingested: k}`; `test_health.py` extended.
3. Document the provenance model in `README.md` (+ the gate protocol) and a `CHANGELOG` entry; note the vibe-code-handoff skill should pass `source_trust` on `create_handoff` going forward. — Acceptance: doc section exists; CHANGELOG dated entry present.
**Verification checklist:**
- [ ] `uv run pytest -q` → entire suite green
- [ ] `recall` and `get_pending_handoffs` both surface `source_trust`
**Parallel Dispatch Proposal (≥3 disjoint tasks):**
- Dispatchable in parallel: `recall.py` surfacing, `health.py` counts, and docs are independent.
- Subagent type: coder
- Rationale: separate files, consume the Phase-0 column only.
**Risks:**
- recall result-shape change breaks consumers: Mitigation → additive field, never remove existing keys → Fallback: gate behind a verbose flag.
**Phase-end review:** Run `/ultrareview`. Address all findings before marking the phase complete.

---

## Section 5: SECURITY & CREDENTIALS
- **Credential storage:** none — `source_trust` is a provenance label, not a secret.
- **Data boundaries:** the label lives in the DB only; it is never written to the markdown export (which launders provenance), preventing a provider from reading trust state from a flattened source.
- **Threat closed:** the CRITICAL A1 path — a non-`operator` handoff can no longer transition to `active` on Codex without operator promotion, breaking untrusted→trusted laundering at the pickup boundary.
- **Encryption at rest / token rotation:** not applicable — no secrets or tokens introduced.

---

## Section 6: TESTING STRATEGY

**Phase 0** — Manual: create a fresh DB, inspect `PRAGMA user_version` and table_info. Automate: `test_db` (fresh-create has column), `test_migration` (v6→v7 idempotent on a populated fixture, backfill values). Verify: re-run is a no-op.

**Phase 1** — Manual: call each writer with and without `source_trust`. Automate: per-writer store/default assertions; audit entry carries trust. Verify: omission yields `agent` everywhere.

**Phase 2** — Manual: pick up an agent-trust handoff as `cc` (expect confirmation) then with confirm; as `codex` (expect refusal). Automate: the full gate matrix (operator/agent × cc/codex × confirm/no-confirm) + no-transition assertions + audit events. Verify: operator-trust fast path unchanged.

**Phase 3** — Manual: `recall` and `status` show provenance. Automate: surfacing assertions; full `uv run pytest -q` green. Verify: additive fields only, no removed keys.
