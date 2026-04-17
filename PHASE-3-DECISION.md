# Phase 3 Decision: No Live Watcher For Now

Decision date: 2026-04-15

## Decision

Do not build a live file watcher for Claude.ai section edits right now.

Keep the current model:
- SQLite is the primary shared state store.
- `export_bridge_markdown` remains the compatibility export path.
- `sync_from_file` remains the startup safety net for Claude.ai-owned file edits.

## Why This Decision Wins

The original reason to consider a watcher was to prevent file-based Claude.ai edits from drifting away from the DB and getting stomped later. That urgent risk is now materially reduced because:

1. Claude.ai direct MCP access is live.
2. Direct Claude.ai-owned write paths have been proven.
3. `sync_from_file` imports Claude.ai-owned file edits into SQLite before Claude Code bridge reads.

That means a watcher is no longer solving a high-severity correctness problem. It would now be a convenience feature.

## Evidence

- Direct Claude.ai read access is confirmed.
- Direct Claude.ai-owned writes are confirmed.
- `sync_from_file` exists and is already integrated into the startup path.
- Current project risk is documentation drift and overbuilding, not data-loss from file edits.

## Tradeoff

What we gain by not building a watcher:
- Less background complexity.
- Fewer moving parts across projects.
- Lower debugging burden.
- Clearer ownership of when file edits are imported.

What we accept:
- File-based Claude.ai edits are synchronized on startup rather than continuously.
- Very fresh file edits may not appear in SQLite until the next startup sync or explicit `sync_from_file` call.

## Revisit Conditions

Reopen the watcher decision only if one of these becomes true:

1. Claude.ai continues to use file editing heavily even after MCP adoption.
2. Startup-triggered synchronization creates real coordination friction.
3. Teams need near-real-time propagation of Claude.ai section edits into SQLite.

If none of those show up in practice, keep the watcher deferred indefinitely.

## Next Move

Phase 4 should begin now.

Recommended first hardening tasks:
- add end-to-end tests for multi-tool workflows
- add coverage for mixed-source reads and export fidelity
- keep docs aligned whenever MCP surfaces change