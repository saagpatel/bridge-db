# Claude.ai Integration Spec

This document describes how Claude.ai interacts with bridge-db — current direct MCP
usage, the file fallback path, and the remaining limitations.

See `ROADMAP.md` for the closed roadmap state, and `OPERATOR-CHECKLIST.md` for the
local verification and registration checklist.

## Current State

Claude.ai has two supported paths:

- **Primary path:** direct bridge-db MCP tools through Claude Desktop.
- **Fallback path:** the markdown file at
  `~/.claude/projects/-Users-d/memory/claude_ai_context.md` via the Filesystem MCP
  server.

The direct MCP path has been validated locally for read access and owned write
behavior. The fallback file remains compatibility infrastructure for file-based
clients and for any Claude.ai workflow that has not moved to direct MCP calls.

### What Claude.ai reads

- Its own sections: Career, Speaking, Research, Capabilities
- CC State Snapshot and Codex State Snapshot (read-only)
- Recent CC Activity and Recent Codex Activity
- Pending Handoffs
- Diagnostics and observability through `health`, `status`, `recall_stats`, and
  `audit_tail`

### What Claude.ai writes

- Updates to Career, Speaking, Research, Capabilities through `update_section`
- Handoffs through `create_handoff`
- Compatibility file edits to the same four Claude.ai-owned sections when direct MCP is
  not used

### How it stays in sync

- Direct MCP writes update SQLite first.
- Consumers call `export_bridge_markdown` after DB writes to keep the fallback file
  current.
- Claude.ai may still edit its owned sections directly in the fallback markdown file.
- Claude Code's `/start` skill calls `mcp__bridge_db__sync_from_file()` before
  bridge-db reads, importing the four Claude.ai-owned sections from the file into
  `context_sections`.

**Current limitation:** fallback file edits are synchronized into the DB on the next
Claude Code startup or explicit `sync_from_file` call, not continuously. That closes
the export-stomp gap, but it is still startup-triggered sync rather than a live
watcher.

---

## Claude.ai Direct MCP Path

### Registration (Claude Desktop)

Register bridge-db in Claude Desktop's MCP config:

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

This gives Claude.ai access to all 22 MCP tools under `mcp__bridge_db__*`, including
the read-only `health` and `status` diagnostics, the file-import helper `sync_from_file`,
the `recall` FTS5 lexical search (Phase −1 of the semantic memory layer), and the
observability tools `recall_stats` and `audit_tail` over the JSONL logs.

This exact `uv`-based stdio launch path is the documented local target and has been
validated in the current setup.

### vibe-code-handoff (updated workflow)

**Fallback (file-based):**
```
vibe-code-handoff appends to ## Pending Handoffs section of claude_ai_context.md
```

**Primary (DB-backed):**
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
it now runs `mcp__bridge_db__sync_from_file()` first, then reads pending handoffs.
The handoff appears immediately in the next CC session.

### weekly-review (updated workflow)

**Fallback (file-based):**
```
weekly-review reads claude_ai_context.md via Filesystem MCP
```

**Primary (DB-backed):**
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

### sync_from_file (startup safety net)

When Claude.ai edits its owned sections through the markdown file instead of MCP tools:

```python
mcp__bridge_db__sync_from_file()
```

This reads `BRIDGE_FILE_PATH`, extracts only the four Claude.ai-owned headings, and
upserts them into `context_sections` with `owner="claude_ai"`. It does not touch
handoffs, snapshots, activity, or any CC/Codex-owned section content.

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

**Priority:** Deferred by current architecture decision. `/start` imports file edits before
bridge reads, so a watcher should only be reconsidered if continuous sync becomes a
real coordination need.

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
