# bridge-db

SQLite-backed MCP server for shared state across Claude.ai, Claude Code, Codex, and related local ops tools.

bridge-db replaces ad hoc edits to `claude_ai_context.md` with a structured SQLite store and 20 MCP tools (19 state/diagnostics tools + `recall`, an FTS5 lexical search over all content). The markdown bridge file is regenerated from the DB via `export_bridge_markdown` and remains available as a fallback for file-based clients.

## Current State

- Cleanup and audit hardening are complete.
- Direct Claude.ai MCP read and write paths have both been validated locally.
- Startup sync from the bridge markdown file is the chosen fallback strategy; Phase 3 closed with a "no live watcher for now" decision.
- Recent hardening closed the remaining audit findings around duplicate handoff clearing, future-schema rejection, and health signaling for missing fallback state.
- Phase −1 of the semantic memory arc shipped and is the **final layer**: `content_index` FTS5 vtable mirrors all content tables, `recall(query, limit, scope)` exposes it via MCP with OR-semantic multi-token queries. Vector/embedding phases were closed after a dry-run showed that "missed" queries targeted content not actually in `bridge.db`. See the closure banner at the top of [bridge-db-semantic-memory-IMPLEMENTATION-PLAN-v2.1.md](bridge-db-semantic-memory-IMPLEMENTATION-PLAN-v2.1.md).
- Local verification is currently green: `115` tests passing, `ruff` clean, `pyright` clean.
- Project is in steady maintenance. Scope is pinned to cross-system *state* coordination; it is not a knowledge store.

## Architecture

```
Claude.ai ──────────────────────────────────────────────┐
  (reads markdown file via Filesystem MCP)              │
  (future: MCP client via Claude Desktop)               │
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

No shared daemon. Each MCP client spawns its own `bridge-db` process via stdio. WAL mode + `PRAGMA busy_timeout=5000` handles concurrent writes safely.

## Tools (19)

| Module | Tools |
|---|---|
| activity | `log_activity`, `get_recent_activity`, `get_shipped_events`, `mark_shipped_processed` |
| handoffs | `create_handoff`, `get_pending_handoffs`, `pick_up_handoff`, `clear_handoff` |
| context | `update_section`, `get_section`, `get_all_sections`, `sync_from_file` |
| snapshots | `save_snapshot`, `get_latest_snapshot` |
| cost | `record_cost`, `get_cost_history` |
| export | `export_bridge_markdown` |
| health | `health`, `status` |

Write tools enforce `caller` ownership, so systems can only write the slices of state they own. Recent hardening also added `notion_os` and `personal_ops` as first-class activity and cost writers.

## Commands

```bash
uv run pytest              # run all tests (97 total)
uv run pyright             # type check (strict mode)
uv run ruff check          # lint
uv run python -m bridge_db --doctor  # local environment diagnostics
uv run python -m bridge_db --status  # compact operator summary
uv run python -m bridge_db          # start MCP server (stdio)
uv run python -m bridge_db.migration  # migrate from bridge markdown
```

## Registration

**Claude Code (user-scoped):**
```bash
claude mcp add --scope user bridge-db -- uv run --directory /Users/d/Projects/bridge-db python -m bridge_db
```

**Codex (`~/.codex/config.toml`):**
```toml
[mcp_servers.bridge-db]
command = "uv"
args = ["run", "--directory", "/Users/d/Projects/bridge-db", "python", "-m", "bridge_db"]
```

## Data

- **DB**: `~/.local/share/bridge-db/bridge.db`
- **Bridge file**: `~/.claude/projects/-Users-d/memory/claude_ai_context.md`
- Retention: 50 activity entries per source, 10 snapshots per system
- Health check: `health` MCP tool or `uv run python -m bridge_db --doctor`
- Operator summary: `uv run python -m bridge_db --status`
- Migration: `uv run python -m bridge_db.migration` (idempotent — safe to re-run)

## Startup Sync

Claude.ai may still write its owned sections directly to the bridge markdown file. To keep those edits from being overwritten on the next export, `sync_from_file` imports the four Claude.ai-owned sections (`career`, `speaking`, `research`, `capabilities`) from `BRIDGE_FILE_PATH` into `context_sections` before bridge consumers read from SQLite.

Claude Code's `/start` workflow now runs `mcp__bridge_db__sync_from_file()` before calling bridge read tools, so file edits are pulled into the DB at session start instead of waiting for a later export cycle.

The current operating model is:
- MCP is the primary coordination path.
- `sync_from_file` is the compatibility safety net for Claude.ai-owned file edits.
- `export_bridge_markdown` keeps the fallback markdown artifact in sync for file-based consumers.

## Docs

- [`OPERATOR-CHECKLIST.md`](OPERATOR-CHECKLIST.md) — Local verification and Claude.ai registration checklist
- [`ROADMAP.md`](ROADMAP.md) — Execution roadmap for the next integration phases
- [`PHASE-3-DECISION.md`](PHASE-3-DECISION.md) — Architectural decision on watcher vs startup sync
- [`codex-migration.md`](codex-migration.md) — Per-skill migration instructions for Codex consumers
- [`integration-spec.md`](integration-spec.md) — Claude.ai integration path (current + future)