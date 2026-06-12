# bridge-db `source_trust` Governance Control — Implementation Roadmap

Feature addition to the existing `bridge-db` repo at `/Users/d/Projects/bridge-db`. **Not
greenfield** — read the existing code first and follow its patterns: the versioned schema ladder
in `db.py` (`SCHEMA_VERSION = 6`, `_SCHEMA_DDL`, `_MIGRATION_Vx_TO_Vy` guarded on PRAGMA
`user_version`), the `Literal` typing in `models.py` (`CallerID`), and the FastMCP tool registration
in `tools/handoffs.py`. This adds a provenance label and gates `pick_up_handoff` on it — closing
the CRITICAL cross-provider laundering path (an untrusted-origin handoff executed by Codex with
`danger-full-access`).

---

## Architecture

### System Overview
```
writer tools (create_handoff / log_activity / update_section / save_snapshot)
        │ set source_trust (default 'agent')
        ▼
DB rows (pending_handoffs | activity_log | context_sections | system_snapshots)
        │
        ├─▶ get_pending_handoffs / recall / status  → surface source_trust
        └─▶ pick_up_handoff (cc|codex)  → GATE: non-operator ⇒ requires_confirmation; codex ⇒ refuse-until-promoted
```
The label is provenance metadata, not searchable text — the FTS `content_index` (UNINDEXED
columns) is untouched, so no `repopulate_content_index` is needed. The gate lives at the one
dangerous transition: a handoff going `pending → active`.

### File Structure (real paths)
```
src/bridge_db/db.py              # EDIT — SCHEMA_VERSION 6→7; _SCHEMA_DDL column; _MIGRATION_V6_TO_V7 + backfill
src/bridge_db/models.py          # EDIT — SourceTrust Literal type
src/bridge_db/tools/handoffs.py  # EDIT — create_handoff param; pick_up_handoff GATE; get_pending_handoffs surface
src/bridge_db/tools/activity.py  # EDIT — log_activity source_trust param
src/bridge_db/tools/context.py   # EDIT — update_section source_trust param
src/bridge_db/tools/snapshots.py # EDIT — save_snapshot source_trust param
src/bridge_db/tools/recall.py    # EDIT — include source_trust in hits
src/bridge_db/tools/health.py    # EDIT — status counts by source_trust
src/bridge_db/audit.py           # EDIT — record gate decisions (consume log_audit)
tests/test_migration.py          # EDIT — v6→v7 idempotent on populated fixture
tests/test_db.py                 # EDIT — fresh-create has column
tests/test_handoffs.py           # EDIT — the gate matrix (the crux)
tests/test_recall.py             # EDIT — surfacing
README.md / CHANGELOG            # EDIT — provenance model + gate protocol
```

### Data Model
Additive only. `source_trust TEXT NOT NULL DEFAULT 'agent'` on the four instruction-bearing
tables, with a column CHECK `IN ('operator','agent','ingested')`. SQLite `ADD COLUMN` permits a
column CHECK when the constant default satisfies it (it does), so no rename/recreate — unlike the
v1→v2 migration that rewrote tables to change existing CHECKs.

`_MIGRATION_V6_TO_V7` sketch:
```sql
ALTER TABLE pending_handoffs ADD COLUMN source_trust TEXT NOT NULL DEFAULT 'agent'
    CHECK(source_trust IN ('operator','agent','ingested'));
-- (same for activity_log, context_sections, system_snapshots)
UPDATE context_sections SET source_trust = 'operator';   -- owner-authored
UPDATE pending_handoffs SET source_trust = 'operator';   -- historically operator-dispatched
-- activity_log / system_snapshots keep the 'agent' default
```
Guard on `PRAGMA user_version < 7`; bump to 7 after. Idempotent via the existing version gate.

### Type Definitions
```python
# models.py
SourceTrust = Literal["operator", "agent", "ingested"]
```
```python
# tools/handoffs.py — pick_up_handoff gains:
confirm: Annotated[bool, Field(description="Operator confirmation for a non-operator-trust handoff")] = False
# gated return (no state transition):
{"ok": False, "requires_confirmation": True, "handoff_id": id,
 "source_trust": "agent", "reason": "non-operator-trust handoff; re-invoke with confirm=True"}
```

### API Contracts
Not applicable for external APIs — bridge-db is a local MCP server. Internal tool-contract changes
are additive: new optional params with defaults, plus one new gated return shape on
`pick_up_handoff`. Existing callers that omit the new params keep today's behavior, except that a
non-`operator` handoff now requires confirmation at pickup.

### Dependencies
No new dependencies:
```bash
uv sync
uv run pytest -q
```

## Scope Boundaries
**In scope:** the `source_trust` column on the four instruction-bearing tables, the v6→v7
migration + backfill, the `SourceTrust` type, writer params + defaults, the `pick_up_handoff` gate
(cc-confirm / codex-strict), surfacing in `get_pending_handoffs` / `recall` / `status`, docs.
**Out of scope:** changing the markdown export (the label stays DB-only by design); a runtime
taint monitor (that is the deferred Layer B of design #2); gating any tool other than
`pick_up_handoff`.
**Deferred:** updating the vibe-code-handoff skill to pass `source_trust` on `create_handoff` —
note it in docs; apply when the skill is next edited.

## Security & Credentials
- No credentials in scope — `source_trust` is a provenance label, not a secret.
- The label lives in the DB row only, never in the markdown export (which launders provenance).
- Closes the CRITICAL A1 path: a non-`operator` handoff cannot transition to `active` on Codex without operator promotion.
- No tokens or encryption introduced.

---

## Phase 0: Schema + type foundation (Week 1, ~3h)
**Objective:** the `source_trust` column on all four tables + the `SourceTrust` type, via the
versioned ladder. No tool-behavior change.
**Tasks:**
1. Add `SourceTrust = Literal["operator","agent","ingested"]` to `models.py`. — Acceptance: `from bridge_db.models import SourceTrust` imports; type check passes.
2. Bump `SCHEMA_VERSION` 6→7; add `source_trust` to all four tables in `_SCHEMA_DDL`. — Acceptance: a fresh DB has `source_trust` (default `agent`) on `pending_handoffs`, `activity_log`, `context_sections`, `system_snapshots`.
3. Add `_MIGRATION_V6_TO_V7` (ALTER + conservative backfill) into the version runner. — Acceptance: `test_migration.py` runs v6→v7 idempotently on a populated v6 fixture; backfill values match (context/handoffs→operator, activity/snapshots→agent); re-run is a no-op.
**Verification checklist:**
- [ ] `uv run pytest tests/test_db.py tests/test_migration.py -q` → green
- [ ] Fresh DB `PRAGMA user_version` == 7; populated v6 upgrades without data loss
- [ ] Migration re-run is idempotent
**Parallel Dispatch Proposal (≥3 disjoint tasks):**
- Dispatchable in parallel: Task 1 (type), the `_SCHEMA_DDL` edit (Task 2), and migration-fixture authoring (part of Task 3) once the column spec is fixed.
- Subagent type: coder
- Rationale: type and fresh-create DDL are independent; the migration consumes the agreed spec.
**Risks:**
- ADD COLUMN with CHECK rejected: Mitigation → plain default + app-layer enum → Fallback: rename+recreate per the v1→v2 pattern.
- Backfill mislabels history: Mitigation → conservative per-table rule + documented assumption → Fallback: all `agent`.
**Phase-end review:** Run `/ultrareview`. Address all findings before marking the phase complete.

---

## Phase 1: Writer defaults (Week 1, ~3h)
**Objective:** writers can set provenance; conservative defaults persist it.
**Tasks:**
1. Add `source_trust: SourceTrust = "agent"` to `create_handoff` and persist it in the INSERT. — Acceptance: `source_trust="operator"` stores `operator`; omission stores `agent`; `test_handoffs.py` asserts both.
2. Add the same param + persistence to `log_activity`, `update_section`, `save_snapshot`. — Acceptance: each writer stores provided/default; existing tests pass (params optional).
3. Record the chosen `source_trust` in the audit log on `create_handoff`. — Acceptance: the `log_audit` entry carries the trust level (asserted).
**Verification checklist:**
- [ ] `uv run pytest tests/test_handoffs.py tests/test_activity.py tests/test_context.py tests/test_snapshots.py -q` → green
- [ ] Omitting `source_trust` on every writer yields `agent` (backward compatible)
**Parallel Dispatch Proposal (≥3 disjoint tasks):**
- Dispatchable in parallel: the four writer edits across `handoffs.py`, `activity.py`, `context.py`, `snapshots.py` — independent files.
- Subagent type: coder
- Rationale: separate tool modules, no shared state beyond the Phase-0 column.
**Risks:**
- Param ordering breaks callers: Mitigation → append as keyword-only with default → Fallback: keep positional signature stable.
**Phase-end review:** Run `/ultrareview`. Address all findings before marking the phase complete.

---

## Phase 2: The pickup gate (Week 1–2, ~4h) — the security win
**Objective:** `pick_up_handoff` enforces provenance; non-`operator` requires confirmation; Codex
takes the strict path.
**Tasks:**
1. Read `source_trust` in `pick_up_handoff`; add `confirm: bool = False`. If trust != `operator` and not `confirm`: return `{ok: False, requires_confirmation: True, source_trust, reason}` and do NOT transition. — Acceptance: agent-trust pickup without confirm stays `pending` + returns `requires_confirmation`; with `confirm=True` → `active`.
2. Strict path for `caller='codex'`: a non-`operator` handoff is refused (not confirmable inline), with guidance to promote to `operator` first. — Acceptance: `codex` + agent-trust → refusal, row stays `pending`; `codex` + operator-trust → `active` in one call.
3. Record the gate decision (allowed / confirmation-required / refused) in the audit log. — Acceptance: each path logs a distinct audit event.
**Verification checklist:**
- [ ] `uv run pytest tests/test_handoffs.py tests/test_audit.py -q` → green
- [ ] `operator`-trust pickup unchanged from today (one call) for both `cc` and `codex`
- [ ] No state transition on any gated/refused path
**Parallel Dispatch Proposal (≥3 disjoint tasks):**
- Dispatchable in parallel: (a) the gate logic, (b) the cc-confirm test scenarios, (c) the codex-strict test scenarios — one code site, three independent test sets.
- Subagent type: coder
- Rationale: gate logic is one function but the test matrix splits cleanly three ways.
**Risks:**
- Confirm protocol confuses callers: Mitigation → explicit `reason` + `requires_confirmation` flag → Fallback: document the two-call protocol in the docstring.
- Operator-trust handoffs accidentally gated: Mitigation → assert the operator fast path in tests → Fallback: feature-flag the gate.
**Phase-end review:** Run `/ultrareview`. Address all findings before marking the phase complete.

---

## Phase 3: Surfacing + docs (Week 2, ~3h)
**Objective:** provenance is visible wherever a provider reads the store.
**Tasks:**
1. Add `source_trust` to `get_pending_handoffs` output and `recall` hit records. — Acceptance: both include the field; `test_recall.py` asserts a recalled handoff/activity carries its trust.
2. Add a `source_trust` breakdown (counts by level) to `status`/`health`. — Acceptance: `status` shows `{operator: n, agent: m, ingested: k}`; `test_health.py` extended.
3. Document the provenance model + gate protocol in `README.md`; add a `CHANGELOG` entry; note the vibe-code-handoff skill should pass `source_trust` on `create_handoff` going forward. — Acceptance: doc section exists; CHANGELOG dated entry present.
**Verification checklist:**
- [ ] `uv run pytest -q` → entire suite green
- [ ] `recall` and `get_pending_handoffs` both surface `source_trust`
**Parallel Dispatch Proposal (≥3 disjoint tasks):**
- Dispatchable in parallel: `recall.py` surfacing, `health.py` counts, and docs — independent files.
- Subagent type: coder
- Rationale: separate files, consume the Phase-0 column only.
**Risks:**
- recall result-shape change breaks consumers: Mitigation → additive field, never remove keys → Fallback: gate behind a verbose flag.
**Phase-end review:** Run `/ultrareview`. Address all findings before marking the phase complete.

---

## Feature-level Definition of Done
- All four instruction-bearing tables carry `source_trust`; fresh DBs and migrated v6 DBs both at `user_version` 7.
- Writers set provenance with a conservative `agent` default; omission is backward compatible.
- `pick_up_handoff` gates: non-`operator` → confirmation for `cc`, refusal-until-promoted for `codex`; `operator`-trust fast path unchanged; no state transition on any gated path.
- `get_pending_handoffs`, `recall`, and `status` surface `source_trust`; the label never enters the markdown export.
- `uv run pytest -q` fully green (incl. extended `test_db`, `test_migration`, `test_handoffs`, `test_recall`, `test_audit`); README + CHANGELOG updated.
