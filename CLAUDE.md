# bridge-db

SQLite-backed MCP server for cross-system state sharing between Claude.ai, Claude Code, and Codex.

## Commands

```bash
uv run pytest              # run all tests (137 total)
uv run pyright             # type check (strict mode)
uv run ruff check          # lint
uv run ruff check --fix    # lint + auto-fix
uv run python -m bridge_db --doctor  # local environment diagnostics
uv run python -m bridge_db          # start MCP server (stdio)
uv run python -m bridge_db.migration  # migrate from bridge markdown
```

## Architecture

- **DB**: `~/.local/share/bridge-db/bridge.db` (WAL mode, `PRAGMA busy_timeout=5000`). Schema at v3 — adds `content_index` FTS5 vtable mirroring all source rows for lexical search.
- **MCP transport**: stdio (stdout = JSON-RPC, all logging → stderr)
- **22 MCP tools** across 9 modules: activity, handoffs, context, snapshots, cost, export, health, recall (FTS5 lexical search; Phase −1 of the semantic memory layer), audit (read-side observability over the JSONL audit + recall query logs).
- **Context access**: `get_db(ctx)` helper casts lifespan context to `aiosqlite.Connection`
- **Tool registration**: `CaptureMCP` pattern in tests — decorators capture raw async fns
- **FTS5 invariant**: every write path that touches `context_sections`, `activity_log`, `system_snapshots`, or `pending_handoffs` calls `upsert_fts_entry` / `gc_fts_orphans` from [db.py](src/bridge_db/db.py) in the same transaction. Auto-prune paths in `log_activity` and `save_snapshot` GC orphan FTS rows.

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
- Phase −1 of the semantic memory layer (FTS5 + `recall`) is shipped and is the **final layer**. A post-shipping dry run through the 20-query eval set showed that most query "misses" reflect content not living in `bridge.db` (it's in memory files, plan docs, Notion), so vector/embedding layers wouldn't help. Scope closed — see the closure banner at the top of [bridge-db-semantic-memory-IMPLEMENTATION-PLAN-v2.1.md](bridge-db-semantic-memory-IMPLEMENTATION-PLAN-v2.1.md).
- Phase 6 observability shipped (2026-04-17, PRs #6 + #7): `recall_stats` (read-side of the recall query log), `audit_tail` (read-side of the audit log), and `wal_size_bytes` + `wal_warning` in `health`. Shared `iter_jsonl` helper in `audit.py`. These extend existing state, not scope.
- Tests at `137` green; `ruff` and `pyright` clean.
- The project is now in a steady maintenance state. Scope: cross-system *state* coordination (handoffs, snapshots, activity, four Claude.ai-owned context sections) + lexical `recall` over that content + observability over the JSONL logs.

## Recent session log (2026-04-17)

Three PRs on top of the FTS5 closure:

- **PR #5** — README drift fix: tool count 19→20 (missed `recall`), test count 97→115, added `.serena/` to gitignore, deleted stale `HANDOFF.md`.
- **PR #6** — Observability feature: added `recall_stats`, `audit_tail` (new `tools/audit.py` module), WAL size in `health`, shared `iter_jsonl` helper. Tool count 20→22 across 9 modules.
- **PR #7** — Post-merge polish: server.py `instructions=` string advertises the new tools, regression test pinning `audit_tail` behavior for externally-edited records without `ts`, operator checklist smoke list includes the observability tools.

If resuming: project is idle. Any new work should respect the closed-scope banner in the semantic-memory plan. Next maintenance tasks (low priority): dep updates, watch for Notion OS / personal-ops caller volume changes.

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

This project is in steady-state maintenance. The codebase is stable, the DB is live, core features are shipped and documented, and observability over the two JSONL logs is now closed (was a half-built feedback loop). 22 MCP tools across 9 modules, 137 tests green, pyright + ruff clean. Scope is explicitly pinned to cross-system *state* coordination plus lexical `recall` plus observability — expansion into a knowledge store is ruled out.

## Stack

- **Language**: Python 3.12+
- **MCP transport**: stdio (MCP SDK)
- **Database**: SQLite via `aiosqlite`
- **Type checking**: pyright (strict)
- **Lint**: ruff
- **Test**: pytest (137 tests)

## How To Run

```bash
uv run pytest              # run all tests (137 total)
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

Scope is closed. The semantic-memory layer stops at Phase −1 (FTS5 + `recall`); observability is shipped as Phase 6. Any further work should be maintenance-only: doc drift, dependency updates, consumer-side fixes, and dogfooding `recall_stats` / `audit_tail` to see whether those feedback loops surface anything worth acting on. If a new coordination surface is wanted, introduce it explicitly — don't expand `bridge.db` into a knowledge store.

<!-- portfolio-context:end -->