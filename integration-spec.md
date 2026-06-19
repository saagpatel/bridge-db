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
        "<path-to-bridge-db>",
        "python",
        "-m",
        "bridge_db"
      ],
      "env": {
        "BRIDGE_DB_PRINCIPAL_TOKEN": "<claude_ai-token>",
        "BRIDGE_DB_AUTH_MODE": "warn"
      }
    }
  }
}
```

Once `BRIDGE_DB_AUTH_MODE` leaves `off`, the `env` block above is required: the
`claude_ai` enrollment token (from `--enroll claude_ai`) goes in
`BRIDGE_DB_PRINCIPAL_TOKEN`, and `BRIDGE_DB_AUTH_MODE` sets the rollout dial. In
`off` mode the env block may be omitted and legacy behavior is fully preserved.

This gives Claude.ai access to all 24 MCP tools under `mcp__bridge_db__*`, including
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
    project_path="~/Projects/<project>",
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
mcp__bridge_db__get_recent_activity(limit=20) # raw mixed activity feed
mcp__bridge_db__get_activity_signal(limit=20) # operator feed with lifecycle rows compressed
mcp__bridge_db__get_shipped_events(unprocessed_only=False) # shipped projects
mcp__bridge_db__confirm_shipped_sync(...) # record downstream proof, then mark processed
mcp__bridge_db__record_shipped_event_disposition(...) # record non-receipt policy disposition
mcp__bridge_db__get_cost_history()          # cost trend
```

Use `get_recent_activity` when raw row-level activity is required. It includes
each stored row, including Claude Code `SessionEnd` lifecycle telemetry tagged
`session-boundary`. Use `get_activity_signal` for startup briefs, dashboards,
morning briefs, cross-provider review intake, and other operator-facing
contexts; it returns substantive rows plus compressed lifecycle aggregates with
counts and first/last timestamps. This is a read-side signal policy only: it
does not delete activity rows, alter audit history, or change shipped/handoff
semantics.

`record_shipped_event_disposition` is for non-receipt decisions only. It does
not add `PROCESSED` and does not write to `shipped_sync_receipts`.

`mark_shipped_processed` remains a compatibility path only for non-shipped
operational rows. It refuses `SHIPPED` activity ids before updating anything.
If a blocked `mark_shipped_processed` attempt appears in `audit_tail`, route the
row to `confirm_shipped_sync` with downstream proof or
`record_shipped_event_disposition` with an explicit policy reason. If
`status.processed_shipped_without_receipt` is nonzero, treat it as historical or
manual drift until proven otherwise.

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

**With auth active (`BRIDGE_DB_AUTH_MODE` != `off`):** the file is an unauthenticated
channel by design. Changed or new sections are imported as `source_trust='ingested'`
and reported in the `demoted` list of the return value. Sections whose content is
identical to what is already in the DB are skipped and their existing label is
preserved. The operator reviews demoted sections and promotes reviewed ones via
`uv run python -m bridge_db --promote-section <section>` (TTY-gated). Claude Code's
`/start` skill surfaces `demoted` sections when they are present.

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
`~/Projects/notification-hub/src/notification_hub/watcher.py` handles activity
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
| pending_handoffs | claude_ai (create), cc/codex (pick up and clear) | all |
| cost_records | cc, codex, notion_os, personal_ops (own system) | all |

The `update_section` tool enforces this at the DB layer — no path bypasses it
for MCP writes. Compatibility file edits are limited to Claude.ai-owned sections
and are imported through `sync_from_file` on Claude Code startup or explicit call;
the live watcher remains deferred.

## Principal Capability Matrix

Auth is currently a canary, not a global enforcement flip. In
`BRIDGE_DB_AUTH_MODE=warn`, a connection-bound principal/caller mismatch is
allowed but audited through `auth.mismatch`; in future `enforce` canaries, the
same mismatch is rejected. Do not move the machine-wide default to `enforce`
until each active client has a current enrolled token, verified spawn env, and a
green read/write smoke check.

All principals may use read-side tools for bridge-owned state:
`health`, `status`, `get_recent_activity`, `get_activity_signal`, `get_shipped_events`,
`get_pending_handoffs`, `get_section`, `get_all_sections`, `get_latest_snapshot`,
`get_cost_history`, `recall`, `recall_stats`, and `audit_tail`.

| Principal | Write capabilities | Boundaries |
|---|---|---|
| `codex` | `log_activity(caller="codex")`, `save_snapshot(caller="codex")`, `record_cost(caller="codex")`, `confirm_shipped_sync(caller="codex")`, `record_shipped_event_disposition(caller="codex")`, `pick_up_handoff`/`clear_handoff` where the handoff gate allows it | Owns Codex truth and verification; must not write or refresh `cc` snapshots or Claude.ai sections |
| `cc` | `log_activity(caller="cc")`, `save_snapshot(caller="cc")`, `record_cost(caller="cc")`, `confirm_shipped_sync(caller="cc")`, `record_shipped_event_disposition(caller="cc")`, `pick_up_handoff`/`clear_handoff` where the handoff gate allows it | Owns Claude Code state and session telemetry; must not write Codex snapshots or Claude.ai sections |
| `claude_ai` | `update_section` for `career`, `speaking`, `research`, and `capabilities`; `create_handoff(caller="claude_ai")`; compatibility file edits to those four sections followed by `sync_from_file` | Advisory and dispatch surface; must not write Codex/CC snapshots or act as local execution proof |
| `notion_os` | `log_activity(caller="notion_os")`, `record_cost(caller="notion_os")`, `confirm_shipped_sync(caller="notion_os")`, `record_shipped_event_disposition(caller="notion_os")` | Owns Notion-side receipts/activity it actually verified; must not infer project mappings beyond `notion_sync` |
| `personal_ops` | `log_activity(caller="personal_ops")`, `record_cost(caller="personal_ops")`, `confirm_shipped_sync(caller="personal_ops")`, `record_shipped_event_disposition(caller="personal_ops")` | Owns operator-facing coordination receipts; must not replace repo-local or bridge-db verification |

`mark_shipped_processed` is intentionally absent from the shipped-event write
path above. It remains available only for non-shipped operational rows and
refuses `SHIPPED` activity ids.

---

## No Daemon Needed

Each MCP client (CC, Codex, Claude Desktop) launches its own `bridge-db` process via
stdio. All processes share the same SQLite file at `~/.local/share/bridge-db/bridge.db`
with WAL mode + `PRAGMA busy_timeout=5000` for safe concurrent access.

There is no shared bridge-db daemon, no HTTP transport, and no need for a LaunchAgent.
The stdio model is client-managed: the server process lives exactly as long as the
client session that spawned it.
