# bridge-db

SQLite-backed MCP server for cross-system state sharing between Claude.ai, Claude Code, and Codex.

## Commands

```bash
uv run pytest              # run all tests (65 total)
uv run pyright             # type check (strict mode)
uv run ruff check          # lint
uv run ruff check --fix    # lint + auto-fix
uv run python -m bridge_db          # start MCP server (stdio)
uv run python -m bridge_db.migration  # migrate from bridge markdown
```

## Architecture

- **DB**: `~/.local/share/bridge-db/bridge.db` (WAL mode, `PRAGMA busy_timeout=5000`)
- **MCP transport**: stdio (stdout = JSON-RPC, all logging → stderr)
- **16 MCP tools** across 6 modules: activity, handoffs, context, snapshots, cost, export
- **Context access**: `get_db(ctx)` helper casts lifespan context to `aiosqlite.Connection`
- **Tool registration**: `CaptureMCP` pattern in tests — decorators capture raw async fns

## Key conventions

- `caller` parameter on write tools enforces ownership (`CallerID = Literal["cc","codex","claude_ai"]`)
- `source`/`system` DB columns map 1:1 from `caller`
- Activity retention: 50 per source; snapshot retention: 10 per system (auto-pruned on insert)
- Export trigger: consumers call `export_bridge_markdown` explicitly after writes
- Logging: `logging.basicConfig(stream=sys.stderr)` — never stdout

## Registration

```bash
claude mcp add --scope user bridge-db -- uv run --directory /Users/d/Projects/bridge-db python -m bridge_db
```

## Test fixtures

- `db` fixture: `tmp_path / "test.db"` with WAL mode + schema applied
- `make_ctx(conn)`: mock Context satisfying `ctx.request_context.lifespan_context.db`
- `CaptureMCP`: `FastMCP` subclass that captures registered tool fns by name

<!-- portfolio-context:start -->
# Portfolio Context

## What This Project Is

bridge-db is an active local project in the /Users/d/Projects portfolio.

## Current State

Portfolio truth currently marks this project as `active` with `boilerplate` context. Phase 104 recovered minimum-viable context so future sessions can resume without rediscovery.

## Stack

- Primary stack: Python

## How To Run

```bash
uv run pytest              # run all tests (65 total)
uv run pyright             # type check (strict mode)
uv run ruff check          # lint
uv run ruff check --fix    # lint + auto-fix
uv run python -m bridge_db          # start MCP server (stdio)
uv run python -m bridge_db.migration  # migrate from bridge markdown
```

## Known Risks

- This repo only has minimum-viable recovery context today; deeper handoff details may still live in the README and supporting docs.

## Next Recommended Move

Use this context plus the README and supporting docs to resume the next active task, then promote the repo beyond minimum-viable by capturing a dedicated handoff, roadmap, or discovery artifact.

<!-- portfolio-context:end -->
