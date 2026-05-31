# bridge-db

SQLite-backed MCP server for cross-system state sharing between Claude.ai, Claude Code, and Codex.

## Commands

```bash
uv run pytest              # run all tests
uv run pyright             # type check (strict mode)
uv run ruff check          # lint
uv run ruff check --fix    # lint + auto-fix
uv run python -m bridge_db --doctor  # local environment diagnostics
uv run python -m bridge_db --status  # compact operator summary
uv run python -m bridge_db --dogfood # read-only observability dogfood pass
uv run python -m bridge_db --rebuild-content-index  # repair FTS recall index drift
uv run python -m bridge_db --log-session-boundary bridge-db  # FTS-safe CC hook logging
uv run python -m bridge_db          # start MCP server (stdio)
uv run python -m bridge_db.migration  # migrate from bridge markdown
```

## Architecture

- **DB**: `~/.local/share/bridge-db/bridge.db` (WAL mode, `PRAGMA busy_timeout=5000`). Schema at v4 — adds `content_index` FTS5 vtable mirroring all source rows for lexical search, plus `shipped_sync_receipts` for downstream proof before shipped events are marked processed.
- **MCP transport**: stdio (stdout = JSON-RPC, all logging → stderr)
- **23 MCP tools** across 9 modules: activity, handoffs, context, snapshots, cost, export, health, recall (FTS5 lexical search; Phase −1 of the semantic memory layer), audit (read-side observability over the JSONL audit + recall query logs). `health` / `status` include signals for pending handoffs, unprocessed shipped events, receiptless processed shipped events, FTS index drift, WAL size, and bridge-file freshness.
- **Context access**: `get_db(ctx)` helper casts lifespan context to `aiosqlite.Connection`
- **Tool registration**: `CaptureMCP` pattern in tests — decorators capture raw async fns
- **FTS5 invariant**: every write path that touches `context_sections`, `activity_log`, `system_snapshots`, or `pending_handoffs` calls `upsert_fts_entry` / `gc_fts_orphans` from [db.py](src/bridge_db/db.py) in the same transaction. Auto-prune paths in `log_activity` and `save_snapshot` GC orphan FTS rows.

## Conventions

- `caller` parameter on write tools enforces ownership (`CallerID = Literal["cc","codex","claude_ai","notion_os","personal_ops"]`)
- `source`/`system` DB columns map 1:1 from `caller`
- Activity retention: 50 per source; snapshot retention: 10 per system (auto-pruned on insert)
- Export trigger: consumers call `export_bridge_markdown` explicitly after writes
- Startup sync trigger: Claude Code `/start` calls `sync_from_file` before bridge reads so Claude.ai-owned file edits are imported into SQLite first
- Logging: `logging.basicConfig(stream=sys.stderr)` — never stdout

## Gotchas

- **SessionEnd hook path**: Claude Code's SessionEnd hook must use `uv run --directory ~/Projects/bridge-db python -m bridge_db --log-session-boundary <project>` so session-boundary activity rows get FTS entries through the normal write path. This hook-specific path intentionally does not run activity retention pruning.
- **Shipped-event sync**: `confirm_shipped_sync` requires a downstream system/ref, stores a receipt, then adds `PROCESSED`. `mark_shipped_processed` is a legacy compatibility path — prefer receipt-backed `confirm_shipped_sync` for bridge-sync work.
- **Semantic memory scope closed**: FTS5 + `recall` is the final layer (Phase −1). Vector/embedding layers are ruled out — most query misses reflect content not in `bridge.db`. See closure banner in `bridge-db-semantic-memory-IMPLEMENTATION-PLAN-v2.1.md`.
- **FTS drift repair**: `--rebuild-content-index` is the CLI-only repair path; FTS index drift is treated as a hard health failure because `recall` depends on `content_index` mirroring source tables.
- **Post-sync review**: after scheduled Bridge Syncs or shipped-event reconciliation, use `POST-SYNC-REVIEW.md` to verify DB state, markdown export freshness, and scorecard updates.
- **Dependency drift**: check with `uv tree --outdated`; refresh `uv.lock` and re-run the full verifier to confirm green.

## Registration

```bash
claude mcp add --scope user bridge-db -- uv run --directory ~/Projects/bridge-db python -m bridge_db
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

This project is in steady-state maintenance. The codebase is stable, the DB is live, core features are shipped and documented, and observability over the two JSONL logs is now closed (was a half-built feedback loop). 23 MCP tools across 9 modules, 148 tests green, pyright + ruff clean. Scope is explicitly pinned to cross-system *state* coordination plus lexical `recall`, shipped-event sync receipts, plus observability — expansion into a knowledge store is ruled out.

## Stack

- **Language**: Python 3.12+
- **MCP transport**: stdio (MCP SDK)
- **Database**: SQLite via `aiosqlite`
- **Type checking**: pyright (strict)
- **Lint**: ruff
- **Test**: pytest (148 tests)

## How To Run

```bash
uv run pytest              # run all tests
uv run pyright             # type check (strict mode)
uv run ruff check          # lint
uv run ruff check --fix    # lint + auto-fix
uv run python -m bridge_db --doctor  # local environment diagnostics
uv run python -m bridge_db --status  # compact operator summary
uv run python -m bridge_db --dogfood # read-only observability dogfood pass
uv run python -m bridge_db --rebuild-content-index  # repair FTS recall index drift
uv run python -m bridge_db --log-session-boundary bridge-db  # FTS-safe CC hook logging
uv run python -m bridge_db          # start MCP server (stdio)
uv run python -m bridge_db.migration  # migrate from bridge markdown
```

## Known Risks

- Documentation can drift behind implementation, especially around tool counts, supported callers, and test totals.
- Claude.ai still has a file-based path, so cross-client expectations should be checked against `integration-spec.md` before changing ownership rules.
- It is now easy to overbuild a watcher; `sync_from_file` removed the urgent data-loss need, so any watcher work should be justified by real remaining friction.

## Next Recommended Move

Scope is closed. The semantic-memory layer stops at Phase −1 (FTS5 + `recall`); observability is shipped as Phase 6. Any further work should be maintenance-only: doc drift, dependency updates, consumer-side fixes, and dogfooding `recall_stats` / `audit_tail` to see whether those feedback loops surface anything worth acting on. If a new coordination surface is wanted, introduce it explicitly — don't expand `bridge.db` into a knowledge store.

<!-- portfolio-context:end -->
