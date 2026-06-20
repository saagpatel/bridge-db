# bridge-db

![CI](https://github.com/saagpatel/bridge-db/actions/workflows/ci.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)

SQLite-backed MCP server for shared state across Claude.ai, Claude Code, Codex, and related local ops tools.

bridge-db replaces ad hoc edits to a shared markdown file with a structured SQLite store and a focused MCP tool surface: cross-system state, FTS5 lexical `recall`, shipped-event sync receipts, shipped-event dispositions, and observability over the audit and recall logs. The markdown bridge file is regenerated from the DB via `export_bridge_markdown` and remains available as a fallback for file-based clients.

## Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** — fast Python package manager

```bash
git clone https://github.com/saagpatel/bridge-db.git
cd bridge-db
uv sync          # install all deps into .venv
uv run pytest    # verify the install
```

## Status

- Steady maintenance. Scope is cross-system *state* coordination, lexical `recall`, and observability — not a general knowledge store.
- Schema v10: context sections carry monotonic `version` tokens; stale writes and raced handoff claims produce durable `write_conflicts` receipts.
- FTS5 `content_index` mirrors all content tables; `health` and `status` verify source-row / FTS-row alignment.
- 26 MCP tools across 10 modules (activity, handoffs, context, snapshots, cost, export, health, recall, audit, conflicts).

## Architecture

```
Claude.ai ──────────────────────────────────────────────┐
  (direct MCP via Claude Desktop)                       │
  (fallback: markdown file via Filesystem MCP)          │
                                                         ▼
CC skills ──► MCP stdio ──► bridge-db process ──► SQLite (WAL)
Codex      ──► MCP stdio ──► bridge-db process ──►  ~/.local/share/bridge-db/bridge.db
                                                         │
                                              export_bridge_markdown
                                                         │
                                                         ▼
                                           ~/.claude/projects/<encoded-home>/
                                           memory/claude_ai_context.md
```

No shared daemon. Each MCP client spawns its own `bridge-db` process via stdio. WAL mode + `PRAGMA busy_timeout=15000` handles concurrent writer waiting; logical stale-write protection comes from CAS on mutable context sections.

## Tools

Verify the current tool count from source with
`rg '@mcp\.tool' src/bridge_db -c`. As of the 2026-06-20 source check, the
surface is 26 tools across these 10 modules:

| Module | Tools |
|---|---|
| activity | `log_activity`, `get_recent_activity`, `get_activity_signal`, `get_shipped_events`, `confirm_shipped_sync`, `record_shipped_event_disposition`, `mark_shipped_processed` |
| handoffs | `create_handoff`, `get_pending_handoffs`, `pick_up_handoff`, `clear_handoff` |
| context | `update_section`, `get_section`, `get_all_sections`, `sync_from_file` |
| snapshots | `save_snapshot`, `get_latest_snapshot` |
| cost | `record_cost`, `get_cost_history` |
| export | `export_bridge_markdown` |
| health | `health`, `status` |
| recall | `recall`, `recall_stats` |
| audit | `audit_tail` |
| conflicts | `get_write_conflicts` |

Write tools enforce `caller` ownership, so systems can only write the slices of state they own. Recent hardening also added `notion_os` and `personal_ops` as first-class activity and cost writers.

## Provenance & the pickup gate

Instruction-bearing rows carry a `source_trust` label — `operator`, `agent`, or `ingested` — recording who authored the content. It lives only in the DB (schema v7+, on `pending_handoffs`, `activity_log`, `context_sections`, `system_snapshots`) and is **never serialized into the markdown export**, which would otherwise launder provenance.

- **Writers** set it via an optional `source_trust` param; the conservative default is `agent` (a Claude-dispatched write is agent-authored unless the operator asserts otherwise). `update_section` preserves an existing section's label on a content-only re-sync.
- **The gate** lives at the one dangerous transition — `pick_up_handoff` moving a handoff `pending → active`:
  - `operator`-trust → picks up in one call (`cc` and `codex`).
  - `cc` + non-`operator` → returns `requires_confirmation` and does **not** transition; re-invoke with `confirm=True` to proceed.
  - `codex` + non-`operator` → **refused** (Codex runs with `danger-full-access`; `confirm` cannot bypass it). Promote the handoff to `operator` trust first.
- **Visibility:** `get_pending_handoffs`, `get_section`, `get_all_sections`, `get_recent_activity`, `get_activity_signal`, `get_shipped_events`, `get_latest_snapshot`, and `recall` hits carry `source_trust` plus `instruction_boundary` metadata that tells consumers returned content is stored data, not instructions. Lifecycle aggregates use a trust summary and `source_trust="mixed"` when rows differ. `status` reports `pending_handoffs_by_trust` and `health` a full per-table `source_trust_breakdown`. Each gate decision (`allowed` / `confirmation_required` / `refused`) is written to the audit log.

> Consumers authoring an operator-directed handoff (e.g. the `vibe-code-handoff` skill) should pass `source_trust="operator"` on `create_handoff` so it picks up without confirmation.

## CAS & Conflict Receipts

Context sections are the mutable bridge surface, so they carry a monotonic
`version` token. Consumers should read with `get_section`, edit locally, then
call `update_section(..., if_match_version=<version>)`. A stale token returns
`ok=false`, `conflict=true`, and a `receipt_id` instead of clobbering a newer
row. `if_match_updated_at` remains as a compatibility guard, but `version` is
the preferred token because timestamps have one-second resolution.

Existing-row blind writes are temporarily allowed in canary mode and returned as
`legacy_blind_write=true`; set `BRIDGE_DB_CONTEXT_CAS_MODE=enforce` to reject
them. New section inserts without CAS remain allowed.

`export_bridge_markdown` records the exported version/hash for each rendered
Claude.ai-owned context section. Later `sync_from_file` imports a changed
fallback-file section only if the DB still matches that exported base. If the DB
has advanced, the import is rejected and recorded in `write_conflicts`.

Use `get_write_conflicts(status="open")` to inspect stale section writes,
stale markdown imports, and raced handoff claims.

## Commands

```bash
uv run pytest              # run all tests
uv run pyright             # type check (strict mode)
uv run ruff check          # lint
uv run python -m bridge_db --doctor  # local environment diagnostics
uv run python -m bridge_db --status  # compact operator summary
uv run python -m bridge_db --dogfood # read-only observability dogfood pass
uv run python -m bridge_db --rebuild-content-index  # repair FTS recall index drift
uv run python -m bridge_db --log-session-boundary bridge-db  # FTS-safe CC hook logging
uv run python -m bridge_db          # start MCP server (stdio)
uv run python -m bridge_db.migration  # migrate from bridge markdown
```

## Registration

Replace `/path/to/bridge-db` below with the absolute path to your clone of this repo
(e.g. `$(pwd)` if you are already inside it, or `~/Projects/bridge-db` as a common convention).

**Claude Code (user-scoped):**
```bash
claude mcp add --scope user bridge-db -- uv run --directory /path/to/bridge-db python -m bridge_db
```

**Codex (`~/.codex/config.toml`):**
```toml
[mcp_servers.bridge-db]
command = "uv"
args = ["run", "--directory", "/path/to/bridge-db", "python", "-m", "bridge_db"]
```

## Data

- **DB**: `~/.local/share/bridge-db/bridge.db`
- **Bridge file**: `~/.claude/projects/<encoded-home>/memory/claude_ai_context.md`
  (Claude Code encodes your home dir path by replacing `/` with `-`; the default is derived
  automatically at runtime — override via `BRIDGE_FILE_PATH` if needed)
- Retention: 50 activity entries per source; 10 snapshots per system family
  (Codex operating and consulted-node snapshots are retained independently)
- Health check: `health` MCP tool or `uv run python -m bridge_db --doctor`
- Operator summary: `uv run python -m bridge_db --status`
- Dogfood pass: `uv run python -m bridge_db --dogfood` bundles the status, FTS index, WAL, recall, and shipped-sync audit checks used after bridge-sync runs
- FTS repair: `uv run python -m bridge_db --rebuild-content-index` rebuilds the local `content_index` from source tables when health reports recall-index drift
- Session boundary logging: Claude Code's SessionEnd hook should call `uv run --directory /path/to/bridge-db python -m bridge_db --log-session-boundary <project>` rather than writing SQLite directly; this path adds the FTS row and does not run activity retention pruning
- Migration: `uv run python -m bridge_db.migration` (idempotent — safe to re-run)

`activity_log` retention and shipped-sync receipts are separate surfaces:
activity rows are recent context, while `shipped_sync_receipts` is the proof
ledger for downstream shipped-event syncs. Treat `processed_shipped_without_receipt=0`
and `fts_missing=0` as primary clean signals, and use
`actionable_unprocessed_shipped=0` when policy dispositions explain why raw
`unprocessed_shipped` remains nonzero.

`get_recent_activity` is the raw compatibility feed: it returns individual
activity rows exactly as stored, including high-volume lifecycle telemetry such
as Claude Code `SessionEnd` rows tagged `session-boundary`. Operator-facing
consumers should use `get_activity_signal` instead. It keeps substantive rows
visible while compressing repeated lifecycle rows into aggregates keyed by
source, project, summary family, and hour/day time bucket. Raw rows and audit
events remain available for debugging and forensic review.

For Notion reconciliation, treat each shipped event's `notion_sync` object as the
machine-readable gate:

- `ready`: fetch the explicit `notion_page_id`, update only that row, fetch it
  again, then call `confirm_shipped_sync` with the readback proof.
- `meta_no_target`: do not update Notion. Confirm the event with
  `downstream_system=policy` and `downstream_ref` pointing to the configured
  policy file after verifying the policy applies to the event.
- `unmatched`, `no_notion_target`, or `registry_unavailable`: leave the event
  unprocessed and repair the project registry or mapping source first.

For non-receipt handling, use `record_shipped_event_disposition` only when an
operator policy says the row should remain auditable but should not proceed to a
downstream receipt. The disposition appears as `policy_disposition` on
`get_shipped_events`; it does not write a receipt and does not add `PROCESSED`.
Do not use `mark_shipped_processed` for `SHIPPED` rows; it is retained only for
non-shipped operational events such as `TASK_DONE`, `APPROVAL_SENT`,
`PLANNING_APPLIED`, or `REVIEW_CLOSED`.

## Startup Sync

Claude.ai may still write its owned sections directly to the bridge markdown file. To keep those edits from being overwritten on the next export, `sync_from_file` imports the four Claude.ai-owned sections (`career`, `speaking`, `research`, `capabilities`) from `BRIDGE_FILE_PATH` into `context_sections` before bridge consumers read from SQLite.

Claude Code's `/start` workflow now runs `mcp__bridge_db__sync_from_file()` before calling bridge read tools, so file edits are pulled into the DB at session start instead of waiting for a later export cycle.

The current operating model is:
- MCP is the primary coordination path.
- `sync_from_file` is the compatibility safety net for Claude.ai-owned file edits.
- `export_bridge_markdown` keeps the fallback markdown artifact in sync for file-based consumers.

## Docs

- [`integration-spec.md`](integration-spec.md) — Claude.ai direct MCP and fallback-file integration
- [`ROADMAP.md`](ROADMAP.md) — Execution roadmap for the next integration phases
- [`docs/EXTERNAL-WRITER-AUDIT.md`](docs/EXTERNAL-WRITER-AUDIT.md) — Direct bridge-db writer audit outside the repo
- [`docs/BRANCH-RETIREMENT.md`](docs/BRANCH-RETIREMENT.md) — Branch cleanup and stale-ref disposition policy

### Internal / maintainer docs

- [`docs/internal/OPERATOR-CHECKLIST.md`](docs/internal/OPERATOR-CHECKLIST.md) — Local verification checklist
- [`docs/internal/POST-SYNC-REVIEW.md`](docs/internal/POST-SYNC-REVIEW.md) — Bridge-sync post-run evidence checklist
- [`docs/internal/PHASE-3-DECISION.md`](docs/internal/PHASE-3-DECISION.md) — Architectural decision: watcher vs startup sync
- [`docs/internal/codex-migration.md`](docs/internal/codex-migration.md) — Per-consumer migration instructions
