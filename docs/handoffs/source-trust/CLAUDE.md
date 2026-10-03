# bridge-db `source_trust` Governance Control

## Overview
Feature addition to the existing `bridge-db` repo (`~/Projects/bridge-db`): add a
`source_trust` provenance label (`operator | agent | ingested`) to instruction-bearing rows and
**gate `pick_up_handoff`** on it, so an untrusted-origin handoff can't be executed by Codex with
`danger-full-access`. **Read `src/bridge_db/db.py` (schema ladder), `src/bridge_db/tools/handoffs.py` (the gate
point), and `src/bridge_db/models.py` (Literal types) first.** The provenance migration is additive; current handoff mutations also require channel binding and exact-session completion evidence.

## Tech Stack
- Python 3.12+ — matches the repo `.python-version`
- `aiosqlite` + FastMCP — existing; no new deps
- `pytest` via `uv run pytest` — existing runner

## Development Conventions
- Follow the versioned schema ladder: bump `SCHEMA_VERSION`, add a `_MIGRATION_Vx_TO_Vy` guarded on PRAGMA `user_version`, keep it idempotent
- Additive ALTER only — no table rename/recreate (the new column's CHECK is satisfiable on ADD COLUMN)
- Types as `Literal` aliases in `src/bridge_db/models.py` (mirror `CallerID`)
- `source_trust` params are optional with conservative defaults; clients must follow the current channel-binding, promotion, and completion-capability contracts in the root CLAUDE.md
- The DB label is authoritative; markdown exports include advisory boundary labels that imports never treat as authority
- Tests before commit; match the existing `tests/test_*.py` structure

## Current Phase
**Complete: schema, writers, pickup gate, and surfacing are implemented.**
IMPLEMENTATION-ROADMAP.md records the original phases; the root CLAUDE.md describes current contracts.

## Key Decisions
| Decision | Choice | Why |
|----------|--------|-----|
| Label values | `operator \| agent \| ingested` (Literal `SourceTrust`) | three origin classes from the red-team; matches `CallerID` pattern |
| Write default | `agent` | MCP operator-trust requests are clamped; independent terminal review is required for promotion |
| Gated transition | `pick_up_handoff` only | pickup (`pending → active`) is the dangerous step |
| Gate semantics | cc and codex → refuse-until-promoted | neither consuming client can self-promote a handoff |
| Export boundary | DB authority; advisory markdown labels | markdown is a regenerated projection that launders provenance |

## Phase-Boundary Review
At the end of every phase, run `/ultrareview` before committing the phase-final code. Do not skip
on phases that "feel small."

## Do NOT
- Do not restart the completed phases in IMPLEMENTATION-ROADMAP.md; follow the root CLAUDE.md scope.
- The original v6→v7 provenance migration uses additive ALTER, guarded by the schema-version ladder.
- Do not treat exported provenance labels as authority or allow non-operator handoff pickup.
- `source_trust` defaults to `agent`; handoff mutations also require channel binding, promotion before pickup, and exact ID/completion capability when clearing.
