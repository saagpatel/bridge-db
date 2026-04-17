# Codex → bridge-db Migration Guide

This document specifies how Codex skills and automations should be updated to use
bridge-db MCP tools instead of writing directly to `claude_ai_context.md`.

## 1. Codex Config Registration

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.bridge-db]
command = "uv"
args = ["run", "--directory", "/Users/d/Projects/bridge-db", "python", "-m", "bridge_db"]
```

After registration, bridge-db tools appear as `bridge-db__log_activity`, etc.

## 2. Available MCP Tools

| Tool | Caller | Purpose |
|---|---|---|
| `bridge-db__log_activity` | `"codex"` | Log a session activity entry |
| `bridge-db__get_recent_activity` | — | Read recent activity (CC + Codex) |
| `bridge-db__get_shipped_events` | — | Get SHIPPED-tagged events |
| `bridge-db__mark_shipped_processed` | — | Mark entries as PROCESSED after Notion sync |
| `bridge-db__create_handoff` | `"claude_ai"` | Create a project handoff (Claude.ai only) |
| `bridge-db__get_pending_handoffs` | — | List pending handoffs |
| `bridge-db__pick_up_handoff` | `"codex"` | Mark handoff as active |
| `bridge-db__clear_handoff` | `"codex"` | Clear completed handoff |
| `bridge-db__save_snapshot` | `"codex"` | Save Codex state snapshot |
| `bridge-db__get_latest_snapshot` | — | Get latest snapshot for a system |
| `bridge-db__record_cost` | `"codex"` | Record monthly cost |
| `bridge-db__get_cost_history` | — | Query cost records |
| `bridge-db__get_section` | — | Read a context section |
| `bridge-db__get_all_sections` | — | Read all context sections |
| `bridge-db__sync_from_file` | — | Import Claude.ai-owned file edits into SQLite |
| `bridge-db__export_bridge_markdown` | — | Regenerate the markdown file |
| `bridge-db__health` | — | Read DB and bridge file health metrics |
| `bridge-db__status` | — | Read compact operator summary data |

**Notes:**
- `update_section` requires `caller="claude_ai"` — Codex cannot write Claude.ai's sections.
- Claude Code `/start` now runs `sync_from_file` before bridge reads, so Claude.ai file edits are pulled into SQLite at session start.

## 3. Per-Skill Migration

### bridge-sync (weekly automation)

**Current behavior:** Reads and writes `claude_ai_context.md` directly.

**New behavior:**

*Export (write Codex state):*
```
bridge-db__save_snapshot(
    caller="codex",
    data={
        "infrastructure": "...",
        "automation_digest": "...",
        "active_projects": "..."
    },
    snapshot_date="YYYY-MM-DD"
)
bridge-db__export_bridge_markdown()
```

*Read CC state (import):*
```
bridge-db__get_latest_snapshot(system="cc")      → active_projects, lessons, etc.
bridge-db__get_pending_handoffs()                 → any dispatched work
bridge-db__get_recent_activity(source="cc", limit=10)
```

*Shipped events sync to Notion:*
```
bridge-db__get_shipped_events(unprocessed_only=True)
# ... sync to Notion ...
bridge-db__mark_shipped_processed(activity_ids=[...])
```

**Fallback:** If bridge-db unavailable, read/write `claude_ai_context.md` directly (existing behavior).

---

### bridge-scaffolding (one-time setup)

No changes needed — this was a one-time setup that is now superseded by `migration.py`.

---

### cost-oracle

**Current behavior:** Reads cost data from `claude_ai_context.md` cost table.

**New behavior:**
```
bridge-db__get_cost_history(system="codex")  → monthly cost records
bridge-db__record_cost(caller="codex", month="YYYY-MM", amount=X.XX)
```

**Fallback:** Parse cost table from `claude_ai_context.md` if bridge-db unavailable.

---

### portfolio-intelligence

**Current behavior:** Reads CC active projects from bridge file.

**New behavior:**
```
bridge-db__get_latest_snapshot(system="cc")
# Use snap["data"]["active_projects"] for project list
```

**Fallback:** Read `## Claude Code State Snapshot` section from file.

---

### cross-provider-review

**Current behavior:** Reads recent CC activity from bridge file.

**New behavior:**
```
bridge-db__get_recent_activity(source="cc", limit=20)
bridge-db__get_shipped_events(unprocessed_only=True)
```

**Fallback:** Parse `## Recent Claude Code Activity` from file.

---

### morning-brief

**Current behavior:** Reads pending handoffs and recent activity from bridge file.

**New behavior:**
```
bridge-db__get_pending_handoffs()
bridge-db__get_recent_activity(limit=10)
bridge-db__get_latest_snapshot(system="cc")
```

**Fallback:** Read from `claude_ai_context.md` directly.

---

### skill-evolution

No bridge-db interaction needed — reads CC skill files directly.

---

## 4. Logging Activity Entries

Every Codex session that does meaningful work should log to bridge-db:

```
bridge-db__log_activity(
    caller="codex",
    project_name="bridge-sync",
    summary="Weekly sync: 3 shipped projects synced to Notion",
    tags=["SHIPPED"],   # if projects were marked shipped
    timestamp="2026-04-21"
)
bridge-db__export_bridge_markdown()
```

This replaces manual appending to `## Recent Codex Activity`.

## 5. Rollback

If bridge-db is down or misconfigured:
1. All Codex skills have explicit fallback instructions pointing at `claude_ai_context.md`
2. The markdown file is kept in sync by `export_bridge_markdown` on every write — so it's always current
3. To disable bridge-db: remove `[mcp_servers.bridge-db]` from `~/.codex/config.toml`

## 6. Current State

- Tool surface and docs were cleaned up on 2026-04-15 to match the live MCP server.
- The Claude.ai file-write overwrite gap is closed by `sync_from_file` plus the Claude Code `/start` hook.
- Direct Claude.ai MCP adoption is now the main remaining integration improvement area.

## 7. Tool Name Differences

Codex MCP tool names use underscores: `bridge-db__log_activity` (not `bridge-db__log-activity`).

If the tool registration uses the server name `bridge-db`, the tools will be prefixed
`bridge_db__` (with underscores) in Codex. Adjust config if needed.