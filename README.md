# bridge-db

SQLite-backed MCP server for shared state across Claude.ai, Claude Code, Codex, and related local ops tools.

bridge-db replaces ad hoc edits to `claude_ai_context.md` with a structured SQLite store and a focused MCP tool surface across state, diagnostics, FTS5 lexical `recall`, shipped-event sync receipts, shipped-event dispositions, and observability over the audit and recall logs. The markdown bridge file is regenerated from the DB via `export_bridge_markdown` and remains available as a fallback for file-based clients.

## Current State

- Cleanup and audit hardening are complete.
- Direct Claude.ai MCP read and write paths have both been validated locally.
- Startup sync from the bridge markdown file is the chosen fallback strategy; Phase 3 closed with a "no live watcher for now" decision.
- Recent hardening closed the remaining audit findings around duplicate handoff clearing, future-schema rejection, and health signaling for missing fallback state.
- Phase −1 of the semantic memory arc shipped and is the **final layer**: `content_index` FTS5 vtable mirrors all content tables, `recall(query, limit, scope)` exposes it via MCP with OR-semantic multi-token queries, and health/status now verify that source rows and FTS rows stay aligned. Vector/embedding phases were closed after a dry-run showed that "missed" queries targeted content not actually in `bridge.db`. See the closure banner at the top of [bridge-db-semantic-memory-IMPLEMENTATION-PLAN-v2.1.md](bridge-db-semantic-memory-IMPLEMENTATION-PLAN-v2.1.md).
- **Phase 6 observability shipped (2026-04-17):** `recall_stats` reads the recall query log, `audit_tail` reads the audit log, and `health` now surfaces `wal_size_bytes` + `wal_warning`. All three close half-built feedback loops without expanding scope. See the Phase 6 section in [ROADMAP.md](ROADMAP.md).
- Shipped-event sync hardening shipped: `confirm_shipped_sync` records downstream proof in `shipped_sync_receipts` before marking a `SHIPPED` activity event `PROCESSED`.
- Shipped-event disposition support keeps non-receipt policy decisions separate
  from proof receipts: `record_shipped_event_disposition` records why a
  `SHIPPED` row is intentionally not receipt-ready without adding `PROCESSED`
  and without writing to `shipped_sync_receipts`.
- The legacy `mark_shipped_processed` path is now non-shipped-only. It refuses
  any `SHIPPED` activity id before updating rows; shipped artifacts require
  `confirm_shipped_sync` with downstream proof or an explicit
  `record_shipped_event_disposition` decision.
- `get_shipped_events` now includes a computed `notion_sync` contract from the
  canonical project registry. Bridge-sync agents should update Notion only when
  `notion_sync.state == "ready"` and the referenced page readback proves the
  expected Project Portfolio row. `meta_no_target` events are policy-backed
  local/meta receipts that should be processed against the policy reference, not
  Notion. `unmatched`, `no_notion_target`, and `registry_unavailable` events
  stay pending instead of being guessed through fuzzy search.
- `health` / `status` also surface `processed_shipped_without_receipt` as a soft drift signal for historical or manual receiptless paths, `actionable_unprocessed_shipped` as the unprocessed shipped count after explicit dispositions are excluded, and `fts_missing` / `fts_orphaned` as hard recall-index health signals. Prefer `confirm_shipped_sync` for new downstream syncs.
- Local verification should be refreshed from source before making current-state
  claims: run `uv run pytest`, `uv run pyright`, `uv run ruff check`, and the
  live `--doctor`, `--status`, and `--dogfood` checks.
- Project is in steady maintenance. Scope is pinned to cross-system *state* coordination plus lexical `recall` plus observability; it is not a knowledge store.
- The Bridge Sync burn-in heartbeat has been retired after a clean post-run
  review. The 2026-05-30 dependency refresh updated the lockfile for current
  patch/runtime drift.
- 2026-05-30 checkpoint: bridge-db is idle in steady maintenance. The shipped-event
  queue was reconciled to Notion proof, dependency drift was refreshed, and the
  clean signals are `unprocessed_shipped=0`, `processed_shipped_without_receipt=0`,
  and `fts_missing=0`. Use `POST-SYNC-REVIEW.md` after future Bridge Sync runs
  and keep new work scoped to real cross-system state coordination needs.
- CAS/provenance hardening added schema v10: context sections now carry integer
  `version` tokens, `update_section` accepts `if_match_version`, stale writes and
  raced handoff claims create durable `write_conflicts` receipts, and
  `export_bridge_markdown` records the exported section version/hash so
  `sync_from_file` can reject stale fallback-file imports.

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
                                           ~/.claude/projects/-Users-d/
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

**Claude Code (user-scoped):**
```bash
claude mcp add --scope user bridge-db -- uv run --directory ~/Projects/bridge-db python -m bridge_db
```

**Codex (`~/.codex/config.toml`):**
```toml
[mcp_servers.bridge-db]
command = "uv"
args = ["run", "--directory", "~/Projects/bridge-db", "python", "-m", "bridge_db"]
```

## Data

- **DB**: `~/.local/share/bridge-db/bridge.db`
- **Bridge file**: `~/.claude/projects/-Users-d/memory/claude_ai_context.md`
- Retention: 50 activity entries per source; 10 snapshots per system family
  (Codex operating and consulted-node snapshots are retained independently)
- Health check: `health` MCP tool or `uv run python -m bridge_db --doctor`
- Operator summary: `uv run python -m bridge_db --status`
- Dogfood pass: `uv run python -m bridge_db --dogfood` bundles the status, FTS index, WAL, recall, and shipped-sync audit checks used after bridge-sync runs
- FTS repair: `uv run python -m bridge_db --rebuild-content-index` rebuilds the local `content_index` from source tables when health reports recall-index drift
- Session boundary logging: Claude Code's SessionEnd hook should call `uv run --directory ~/Projects/bridge-db python -m bridge_db --log-session-boundary <project>` rather than writing SQLite directly; this path adds the FTS row and does not run activity retention pruning
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

- [`OPERATOR-CHECKLIST.md`](OPERATOR-CHECKLIST.md) — Local verification and Claude.ai registration checklist
- [`POST-SYNC-REVIEW.md`](POST-SYNC-REVIEW.md) — Bridge-sync and shipped-event post-run evidence checklist
- [`docs/EXTERNAL-WRITER-AUDIT.md`](docs/EXTERNAL-WRITER-AUDIT.md) — Direct bridge-db writer audit outside the repo
- [`ROADMAP.md`](ROADMAP.md) — Execution roadmap for the next integration phases
- [`PHASE-3-DECISION.md`](PHASE-3-DECISION.md) — Architectural decision on watcher vs startup sync
- [`codex-migration.md`](codex-migration.md) — Per-skill migration instructions for Codex consumers
- [`integration-spec.md`](integration-spec.md) — Claude.ai direct MCP and fallback-file integration
