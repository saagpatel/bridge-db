# Claude.ai Integration Spec

This document describes how Claude.ai interacts with bridge-db — current state, known
limitations, and the planned path to full DB integration.

## Current State (File-Based)

Claude.ai accesses the shared context through the markdown file at
`~/.claude/projects/-Users-d/memory/claude_ai_context.md` via the Filesystem MCP server.

### What Claude.ai reads:
- Its own sections: Career, Speaking, Research, Capabilities
- CC State Snapshot and Codex State Snapshot (read-only)
- Recent CC Activity and Recent Codex Activity
- Pending Handoffs (to dispatch work to Claude Code)

### What Claude.ai writes:
- Updates to Career, Speaking, Research, Capabilities sections (direct Edit tool)
- Appends to Pending Handoffs when dispatching work via `vibe-code-handoff`

### How it stays in sync:
- CC skills (`/end`, `sync-bridge`) call `export_bridge_markdown` after every DB write,
  keeping the markdown file current for Claude.ai reads
- Claude.ai writes go directly to the markdown file, not to the DB — they appear in
  the `context_sections` table only after a full `sync-bridge` export (which reads
  Claude.ai's sections from the file and does not overwrite them)

**Limitation:** Claude.ai writes to the file bypass the DB entirely. The DB's
`context_sections` table may lag Claude.ai's file edits until CC runs `sync-bridge`.

---

## Claude.ai as MCP Client (Future)

### Registration (Claude Desktop)

To give Claude.ai direct DB access, register bridge-db in Claude Desktop's MCP config:

```json
{
  "mcpServers": {
    "bridge-db": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/Users/d/Projects/bridge-db",
        "python",
        "-m",
        "bridge_db"
      ]
    }
  }
}
```

This gives Claude.ai access to all 16 MCP tools under `mcp__bridge_db__*`.

**Prerequisite:** Verify that the Claude Desktop version in use supports custom stdio
MCP servers with `uv`-based Python launchers. As of mid-2026, Claude Desktop MCP
support is stable for Node.js servers; Python + uv support may require testing.

### vibe-code-handoff (updated workflow)

**Current (file-based):**
```
vibe-code-handoff appends to ## Pending Handoffs section of claude_ai_context.md
```

**Target (DB-backed):**
```python
mcp__bridge_db__create_handoff(
    caller="claude_ai",
    project_name="<project>",
    project_path="/Users/d/Projects/<project>",
    roadmap_file="ROADMAP.md",   # optional
    phase="Phase 2",             # optional
)
```

Claude Code's `/start` skill already reads `mcp__bridge_db__get_pending_handoffs()` —
no changes needed on the CC side. The handoff appears immediately in the next CC session.

### weekly-review (updated workflow)

**Current (file-based):**
```
weekly-review reads claude_ai_context.md via Filesystem MCP
```

**Target (DB-backed):**
```python
mcp__bridge_db__get_all_sections()          # career, speaking, research, capabilities
mcp__bridge_db__get_latest_snapshot("cc")   # CC active projects, lessons, patterns
mcp__bridge_db__get_latest_snapshot("codex") # Codex infrastructure state
mcp__bridge_db__get_recent_activity(limit=20) # mixed CC + Codex activity feed
mcp__bridge_db__get_shipped_events(unprocessed_only=False) # shipped projects
mcp__bridge_db__get_cost_history()          # cost trend
```

### update_section (Claude.ai writes)

When Claude.ai edits Career, Speaking, Research, or Capabilities sections:

```python
mcp__bridge_db__update_section(
    caller="claude_ai",
    section_name="career",
    content="<new content>",
)
mcp__bridge_db__export_bridge_markdown()  # keep file in sync for Codex fallback
```

The `update_section` tool enforces ownership — only `caller="claude_ai"` can write
these sections. CC and Codex calls with these section names will receive a ToolError.

---

## File Watcher Path (Future)

A background file watcher would sync Claude.ai's direct file edits into the DB
without requiring Claude.ai to call MCP tools explicitly. This would eliminate the
lag described in the Current State section.

**Approach:**
1. `notification-hub` (already running) watches `claude_ai_context.md` for changes
2. On change: extract Claude.ai-owned sections from the file
3. Call `bridge_db` internals (or a new `sync_sections_from_file()` helper) to update
   `context_sections` rows
4. No `export_bridge_markdown` needed — the file is already current

**Status:** Not implemented. The `notification-hub` watcher at
`/Users/d/Projects/notification-hub/src/notification_hub/watcher.py` handles activity
line parsing but not section sync. This would require a new `SectionSyncHandler`.

**Priority:** Low. Claude.ai → DB writes are rare (weekly review, occasional career
updates). The lag is acceptable. Implement if Claude.ai is registered as MCP client
and section staleness becomes a problem.

---

## Ownership Invariants (All Paths)

Regardless of how Claude.ai accesses bridge-db, these ownership rules hold:

| Section | Writer | Readable by |
|---|---|---|
| career, speaking, research, capabilities | claude_ai only | all |
| cc_snapshot, cc_activity | cc only | all |
| codex_snapshot, codex_activity | codex only | all |
| pending_handoffs | claude_ai (create), cc (clear) | all |
| cost_records | cc, codex (own system) | all |

The `update_section` tool enforces this at the DB layer — no path bypasses it
(file-based writes are the current exception, handled by the file watcher future work).

---

## No Daemon Needed

Each MCP client (CC, Codex, Claude Desktop) launches its own `bridge-db` process via
stdio. All processes share the same SQLite file at `~/.local/share/bridge-db/bridge.db`
with WAL mode + `PRAGMA busy_timeout=5000` for safe concurrent access.

There is no shared bridge-db daemon, no HTTP transport, and no need for a LaunchAgent.
The stdio model is client-managed: the server process lives exactly as long as the
client session that spawned it.
