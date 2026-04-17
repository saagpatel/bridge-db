# bridge-db

SQLite-backed MCP server for cross-system state sharing between Claude.ai, Claude Code, and Codex.

## Commands

```bash
uv run pytest              # run all tests (97 total)
uv run pyright             # type check (strict mode)
uv run ruff check          # lint
uv run ruff check --fix    # lint + auto-fix
uv run python -m bridge_db --doctor  # local environment diagnostics
uv run python -m bridge_db          # start MCP server (stdio)
uv run python -m bridge_db.migration  # migrate from bridge markdown
```

## Architecture

- **DB**: `~/.local/share/bridge-db/bridge.db` (WAL mode, `PRAGMA busy_timeout=5000`)
- **MCP transport**: stdio (stdout = JSON-RPC, all logging → stderr)
- **19 MCP tools** across 7 modules: activity, handoffs, context, snapshots, cost, export, health
- **Context access**: `get_db(ctx)` helper casts lifespan context to `aiosqlite.Connection`
- **Tool registration**: `CaptureMCP` pattern in tests — decorators capture raw async fns

## Key conventions

- `caller` parameter on write tools enforces ownership (`CallerID = Literal["cc","codex","claude_ai","notion_os","personal_ops"]`)
- `source`/`system` DB columns map 1:1 from `caller`
- Activity retention: 50 per source; snapshot retention: 10 per system (auto-pruned on insert)
- Export trigger: consumers call `export_bridge_markdown` explicitly after writes
- Startup sync trigger: Claude Code `/start` now calls `sync_from_file` before bridge reads so Claude.ai-owned file edits are imported into SQLite first
- Logging: `logging.basicConfig(stream=sys.stderr)` — never stdout
- Diagnostics: MCP `health` and `status` tools plus CLI `--doctor` and `--status`

## Current project state

- Phase 1 doc and operator-readiness cleanup is complete.
- Claude Desktop registration is verified locally.
- Claude.ai read access is verified.
- Claude.ai direct write behavior is also verified.
- The Claude.ai file-write overwrite gap is closed: `sync_from_file` is implemented and `/start` runs it before bridge reads.
- End-to-end verification succeeded from the Claude Desktop side.
- Recent audit hardening closed the remaining correctness gaps around duplicate handoff clearing, future-schema mismatch handling, and degraded health reporting.
- The repo is currently green at `97` tests with `ruff` and `pyright` also passing.
- The project is now past cleanup and into a Phase 5-style operator-readiness state.

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

This project is active, in regular local use, and past the bootstrap stage. The codebase is stable, the DB is live, and the current focus is finishing the shift from mixed file-based Claude.ai workflows toward more direct MCP usage without adding unnecessary watcher complexity.

## Stack

- **Language**: Python 3.12+
- **MCP transport**: stdio (MCP SDK)
- **Database**: SQLite via `aiosqlite`
- **Type checking**: pyright (strict)
- **Lint**: ruff
- **Test**: pytest (97 tests)

## How To Run

```bash
uv run pytest              # run all tests (97 total)
uv run pyright             # type check (strict mode)
uv run ruff check          # lint
uv run ruff check --fix    # lint + auto-fix
uv run python -m bridge_db --doctor  # local environment diagnostics
uv run python -m bridge_db          # start MCP server (stdio)
uv run python -m bridge_db.migration  # migrate from bridge markdown
```

## Known Risks

- Documentation can drift behind implementation, especially around tool counts, supported callers, and test totals.
- Claude.ai still has a file-based path, so cross-client expectations should be checked against `integration-spec.md` before changing ownership rules.
- It is now easy to overbuild a watcher; `sync_from_file` removed the urgent data-loss need, so any watcher work should be justified by real remaining friction.

## Next Recommended Move

Phase 3 is closed with a "no watcher for now" decision, and Phase 4 hardening is complete. Next work should focus on operator readiness: add a compact summary/status surface, expand scenario-style workflow tests, and keep tool contracts and docs aligned as the bridge grows.

<!-- portfolio-context:end -->