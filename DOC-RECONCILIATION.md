# DOC-RECONCILIATION.md

> Historical artifact only. This file is not a current source of truth.

This file used to contain generated `/doc-truth-up` reconciliation findings.
Those findings were removed because they preserved stale exact schema, tool,
and test-count claims after the live source had moved on.

## Current Truth Sources

- Schema version: verify with `rg '^SCHEMA_VERSION\s*=' src/bridge_db/db.py`.
- MCP tool surface: verify with `rg '@mcp\.tool' src/bridge_db -c` and sum the
  per-file counts.
- Commands: verify CLI flags in `src/bridge_db/__main__.py`, test/type/lint
  configuration in `pyproject.toml`, and the routine verifier in
  `.codex/verify.commands`.
- Test count and green state: use fresh `uv run pytest`, `uv run pyright`, and
  `uv run ruff check` output. Do not copy old exact counts.

## 2026-06-14 Source Check

- `src/bridge_db/db.py` sets `SCHEMA_VERSION = 8`.
- `rg '@mcp\.tool' src/bridge_db -c` reports 24 tool decorators across 9 tool
  modules.
- README, CLAUDE, ROADMAP, and OPERATOR-CHECKLIST point operators toward
  source-verification commands instead of stale hardcoded test totals.

Future reconciliation passes should regenerate a new dated artifact instead of
reviving this historical file as an authority.
