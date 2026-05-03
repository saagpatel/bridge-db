# AGENTS.md

## Communication Contract

- Follow `/Users/d/.codex/policies/communication/BigPictureReportingV1.md` for all user-facing updates.
- Use exact section labels from `BigPictureReportingV1.md` for default status/progress updates.
- Keep default updates beginner-friendly, big-picture, and low-noise.
- Keep technical details in internal artifacts unless explicitly requested by the user.
- Honor toggles literally: `simple mode`, `show receipts`, `tech mode`, `debug mode`.

## Project Goal

bridge-db is a SQLite-backed MCP server for cross-system state sharing between Claude.ai, Claude Code, Codex, Notion OS, and personal-ops. Keep it scoped to cross-system state coordination, lexical recall, and observability.

## First Read

- `README.md` for project overview and setup.
- `CLAUDE.md` for current project state, command list, and scope boundaries.
- `integration-spec.md` before changing ownership, caller, or cross-client behavior.
- `bridge-db-semantic-memory-IMPLEMENTATION-PLAN-v2.1.md` before touching recall or semantic-memory-adjacent scope.

## Core Rules

- Do not expand bridge-db into a general knowledge store.
- Keep stdout reserved for MCP JSON-RPC; logging belongs on stderr.
- Preserve caller ownership rules and source/system mapping.
- Preserve FTS5 consistency for every write path that touches indexed tables.
- Prefer maintenance-only changes unless a new coordination surface is explicitly requested.

## Codex App Usage

- Use Codex App Projects for repo-specific implementation, review, and verification in this checkout.
- Use a Worktree when changing schema, migrations, MCP tool contracts, caller ownership, recall behavior, audit behavior, or export/sync paths.
- Use connectors/MCP read-first for live bridge state only when the task needs cross-system truth.
- Use artifacts for integration notes, verification summaries, migration plans, and handoff docs.
- Use thread automations for short follow-ups tied to a specific bridge health check or consumer retest.
- Avoid browser or computer use unless debugging an external client integration that cannot be verified through CLI, tests, or MCP.

## Verification

- Use `.codex/verify.commands` as the canonical verifier for routine Codex work.
- Current canonical verifier:
  - `uv run pytest`
  - `uv run pyright`
  - `uv run ruff check`
- Treat runtime checks such as `uv run python -m bridge_db --doctor` and `uv run python -m bridge_db --status` as task-specific add-ons when live bridge state matters.
- If a command is missing, unclear, or unsafe to run, stop and report the blocker instead of guessing.

## Done Criteria

- The requested change is implemented.
- Relevant tests or checks were run, or the exact reason they were not run is stated.
- Docs are updated when tool contracts, schema, ownership rules, or operating workflows change.
- Assumptions, risks, and next steps are summarized before closeout.
