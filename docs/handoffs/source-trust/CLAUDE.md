# bridge-db `source_trust` Governance Control

## Overview
Feature addition to the existing `bridge-db` repo (`~/Projects/bridge-db`): add a
`source_trust` provenance label (`operator | agent | ingested`) to instruction-bearing rows and
**gate `pick_up_handoff`** on it, so an untrusted-origin handoff can't be executed by Codex with
`danger-full-access`. **Read `src/bridge_db/db.py` (schema ladder), `tools/handoffs.py` (the gate
point), and `models.py` (Literal types) first.** Additive, backward-compatible.

## Tech Stack
- Python 3.12+ — matches the repo `.python-version`
- `aiosqlite` + FastMCP — existing; no new deps
- `pytest` via `uv run pytest` — existing runner

## Development Conventions
- Follow the versioned schema ladder: bump `SCHEMA_VERSION`, add a `_MIGRATION_Vx_TO_Vy` guarded on PRAGMA `user_version`, keep it idempotent
- Additive ALTER only — no table rename/recreate (the new column's CHECK is satisfiable on ADD COLUMN)
- Types as `Literal` aliases in `models.py` (mirror `CallerID`)
- New tool params are optional with conservative defaults; never break existing callers
- The label lives in the DB row only — never serialize it into the markdown export
- Tests before commit; match the existing `tests/test_*.py` structure

## Current Phase
**Phase 0: Schema + type foundation**
See IMPLEMENTATION-ROADMAP.md for full phase details.

## Key Decisions
| Decision | Choice | Why |
|----------|--------|-----|
| Label values | `operator \| agent \| ingested` (Literal `SourceTrust`) | three origin classes from the red-team; matches `CallerID` pattern |
| Write default | `agent` | a Claude-dispatched handoff is agent-authored unless the operator asserts |
| Gated transition | `pick_up_handoff` only | pickup (`pending → active`) is the dangerous step |
| Gate semantics | cc → confirm; codex → refuse-until-promoted | Codex is the highest-severity sink (A1) |
| Export boundary | label is DB-only, never in markdown | markdown is a regenerated projection that launders provenance |

## Phase-Boundary Review
At the end of every phase, run `/ultrareview` before committing the phase-final code. Do not skip
on phases that "feel small."

## Do NOT
- Do not add features not in the current phase of IMPLEMENTATION-ROADMAP.md.
- Do not rewrite tables or change existing CHECK constraints — the migration is additive ALTER only, guarded on `user_version < 7`.
- Do not serialize `source_trust` into the markdown export, and do not gate any tool other than `pick_up_handoff`.
- Do not break existing tool callers — new params are optional with an `agent` default; the only behavior change is the pickup gate on non-`operator` handoffs.
