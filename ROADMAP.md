# bridge-db Roadmap

This roadmap captures the current scope-closed state of bridge-db. All originally planned phases are complete or explicitly closed. The project is in steady maintenance.

## Current Position

- Core MCP server is stable, typed, and test-backed. Schema at v3 (adds FTS5 `content_index`).
- SQLite schema and migration path are in place; step-wise migrations proven through v1→v2→v3.
- 22 MCP tools across 9 modules: activity, handoffs, context, snapshots, cost, export, health, recall, audit.
- Markdown export works as a compatibility layer for file-based clients.
- Claude.ai direct MCP read and write paths have both been proven locally.
- The file path remains compatibility infrastructure, not the primary coordination path.
- A startup sync path imports Claude.ai-owned file edits into SQLite before Claude Code reads bridge state.
- Audit hardening closed the correctness gaps around handoff clearing, future-schema detection, degraded health reporting, and latent v1→v2 migration gaps.
- Phase −1 of the semantic memory arc (FTS5 + `recall`) shipped; subsequent phases closed (see below).
- Repo green at `136` tests, `ruff` and `pyright` clean.

## Outcomes We Want

1. Claude.ai can read and write bridge state through MCP where practical.
2. File-based fallback becomes compatibility support, not the primary operating path.
3. Bridge surfaces stay documented, testable, and easy to evolve without drift.

## Phase 1: Integration Readiness

Goal: make the existing MCP path easy to trust and easy to adopt.

Scope
- Confirm every documented tool contract matches the real MCP surface.
- Add a small operator checklist for Claude Desktop registration and first-run validation.
- Add one command or doc flow that confirms `health`, snapshots, handoffs, and export are working end to end.

Done looks like
- README, CLAUDE, and integration docs all point at the same setup path.
- A new contributor can verify bridge-db locally without reading the code.
- No tool-count or caller-support drift remains in the primary docs.
- The startup sync behavior is documented so file-based Claude.ai edits are not a hidden rule.

## Phase 2: Claude.ai Direct MCP Path

Goal: reduce reliance on direct file edits for Claude.ai-owned state.

Scope
- Validate Claude Desktop registration with the real local setup.
- Move at least one Claude.ai workflow from file-editing to direct MCP writes.
- Prioritize `update_section` and `create_handoff` first, because they have the clearest ownership model and the biggest coordination payoff.

Done looks like
- Claude.ai can update at least one owned section through MCP successfully.
- Claude.ai can dispatch a handoff through `create_handoff` without file editing.
- The markdown file is still regenerated for fallback consumers after DB writes.

Status
- Completed on 2026-04-15.
- Claude Desktop registration has been added to the real local config.
- Claude.ai read access is confirmed via a successful `health` call after restart.
- Direct owned write behavior has been proven from the Claude Desktop side.
- The `sync_from_file` safety net is implemented, wired into Claude Code `/start`, and verified end to end.
- Claude.ai can now participate through MCP without relying on file edits for the core verified workflows.

## Phase 3: File Sync Strategy

Goal: choose whether the repo should keep a passive fallback model or add active file-to-DB synchronization.

Options
- Keep the current model: DB is primary for CC/Codex, markdown is fallback for Claude.ai.
- Add a lightweight section-sync watcher for Claude.ai file edits.

Recommendation
- Only build the watcher if Claude.ai remains meaningfully file-based after Phase 2.
- If direct MCP works reliably, keep the file path simple and avoid watcher complexity.

Decision gate
- If section staleness is still happening after direct MCP adoption, implement the watcher.
- If not, keep the markdown path read-mostly and document it as compatibility infrastructure.

Current read
- The `sync_from_file` startup import reduces the urgency of a watcher substantially.
- A watcher is now a convenience feature, not a data-loss fix.

Decision
- Phase 3 is closed for now with a "no live watcher" decision.
- Keep startup sync plus direct MCP usage as the operating model.
- Reopen only if startup-triggered synchronization creates real friction in practice.

## Phase 4: Product Hardening

Goal: make future bridge growth safer.

Scope
- Add higher-level integration tests that cover multi-tool workflows instead of only single-tool behavior.
- Add explicit acceptance coverage for mixed-source activity and cost history reads.
- Add one regression test around exported markdown fidelity for new source types.

Done looks like
- The highest-value user flows are covered by end-to-end tests.
- New MCP tools can be added without silent doc or export drift.

Status
- Completed in this cleanup cycle.
- Added multi-tool workflow coverage and startup-sync/export integration coverage.
- Expanded validation around mixed-source activity and health/readiness behavior.
- Closed audit findings on duplicate handoff clearing, future-schema rejection, and fallback-aware health reporting.

## Suggested Execution Order

1. Complete Phase 1 docs and validation checklist.
2. Prove one real Claude.ai MCP write path in Phase 2.
3. Reassess whether Phase 3 is needed.
4. Use Phase 4 to harden the path that actually wins.

## Risks To Watch

- Claude Desktop support for the exact local MCP launch path may still be environment-sensitive.
- Adding a watcher too early could create unnecessary complexity.
- If docs are not updated when the MCP surface changes, the repo will drift again even if the code stays healthy.

## Phase 1 Status

Completed on 2026-04-15.

What was done
- Added an operator verification checklist.
- Linked the checklist from the primary docs.
- Confirmed the current local Claude Desktop config location.
- Verified the pre-registration state and captured the exact target config path.

## Phase 5 Status

Completed in the Phase −1 hardening cycle:
- Compact operator-facing `status` MCP tool and `--status` CLI subcommand shipped.
- Scenario-style workflow tests across Claude.ai / CC / Codex paths added.
- Doc and tool-count drift caught and corrected in CLAUDE.md, README, integration-spec.

## Phase −1: Semantic Memory Lexical Layer

Goal: give all three systems a fast "have I seen this before" search over bridge-db content.

Shipped
- FTS5 `content_index` virtual table mirroring sections, activity, snapshots, handoffs.
- `recall(query, limit, scope)` MCP tool with bm25 ranking, snippet highlights, source-row previews.
- OR-semantic query sanitizer so multi-token queries return partial matches rather than requiring every token to co-occur.
- Every write path hooked (4 tool modules + migration.py + codex_seed.py); auto-prune paths GC orphan FTS rows.
- `recall_query_log.jsonl` logs every query for usage analysis.

## Phases 0 / 1 / 2 of the Semantic Memory Arc: CLOSED

A dry-run of the 20-query eval set against the live DB showed that **most "missed" queries target content that isn't in `bridge.db`** (it lives in memory files, plan docs, or Notion). Vector/embedding layers would not help — they can't find what isn't indexed. Rather than expand bridge-db into a knowledge store, the project's scope is pinned:

- **bridge.db is a cross-system *state* bridge**, not a knowledge store.
- Lexical `recall` is sufficient over that scope.
- If unified recall across memory / plans / Notion becomes a priority, it will be a *separate* project.

Historical artifacts kept for reference: `bridge-db-semantic-memory-IMPLEMENTATION-PLAN-v2.1.md` (and its v2 predecessor), `eval-set-handoff-package.md`, `semantic_quality_set.json`. Read the closure banner at the top of v2.1 for full decision context.

## Steady State

Future work is maintenance-only:
- Keep docs and tool contracts aligned when MCP surfaces change.
- Watch for WAL bloat if activity volume rises; run `PRAGMA wal_checkpoint(TRUNCATE)` if it exceeds a few MB.
- Apply security/dependency updates to `mcp`, `aiosqlite`, `pydantic`.
- Reopen the roadmap only if a concrete new cross-system coordination need surfaces — not to expand scope into knowledge search.