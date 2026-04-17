# bridge-db Roadmap

This roadmap captures the current post-cleanup state of bridge-db and the next sensible work from here. The repo is now audited, hardened, and locally verified; the goal going forward is to expand the operating surface carefully without reintroducing drift.

## Current Position

- Core MCP server is stable, typed, and test-backed.
- SQLite schema and migration path are in place.
- Markdown export works as a compatibility layer for file-based clients.
- Claude.ai direct MCP read and write paths have both been proven locally.
- The file path remains compatibility infrastructure, not the primary coordination path.
- A startup sync path now imports Claude.ai-owned file edits into SQLite before Claude Code reads bridge state.
- Recent audit hardening closed the remaining correctness gaps around handoff clearing, future-schema detection, and degraded health reporting.
- The repo is currently green at `97` tests with `ruff` and `pyright` also passing.

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

## Recommended Next Task

Start Phase 5 operator readiness:
- add a compact bridge summary/status surface for human operators
- add a few scenario-style workflow tests that mirror real use across Claude.ai, CC, and Codex
- keep docs and tool contracts versioned and aligned as new workflows are added