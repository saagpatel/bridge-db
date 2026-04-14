# bridge-db

SQLite-backed MCP server for cross-system state sharing between Claude.ai, Claude Code, and Codex.

Replaces direct edits to `claude_ai_context.md` with a structured database and 16 MCP tools. The markdown file is kept in sync via `export_bridge_markdown` and serves as a fallback for clients that can't reach the DB.

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

## Tools (16)

| Module | Tools |
|---|---|
| activity | `log_activity`, `get_recent_activity`, `get_shipped_events`, `mark_shipped_processed` |
| handoffs | `create_handoff`, `get_pending_handoffs`, `pick_up_handoff`, `clear_handoff` |
| context | `update_section`, `get_section`, `get_all_sections` |
| snapshots | `save_snapshot`, `get_latest_snapshot` |
| cost | `record_cost`, `get_cost_history` |
| export | `export_bridge_markdown` |

Write tools enforce `caller` ownership — Codex cannot write Claude.ai's sections, etc.

## Commands

```bash
uv run pytest              # run all tests (65 total)
uv run pyright             # type check (strict mode)
uv run ruff check          # lint
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
- Migration: `uv run python -m bridge_db.migration` (idempotent — safe to re-run)

## Docs

- [`codex-migration.md`](codex-migration.md) — Per-skill migration instructions for Codex consumers
- [`integration-spec.md`](integration-spec.md) — Claude.ai integration path (current + future)
