# bridge-db

SQLite-backed MCP server for cross-system state sharing between Claude.ai, Claude Code, Codex, Notion OS, and personal-ops.

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
uv run python -m bridge_db --reconcile-canonical-keys  # backfill GHRA repo_full_name keys
uv run python -m bridge_db --log-session-boundary bridge-db  # FTS-safe CC hook logging
uv run python -m bridge_db          # start MCP server (stdio)
uv run python -m bridge_db.migration  # migrate from bridge markdown
uv run python -m bridge_db --enroll cc            # enroll/rotate a principal (TTY only)
uv run python -m bridge_db --list-principals      # show enrolled principals
uv run python -m bridge_db --revoke-principal cc  # revoke a principal (TTY only)
uv run python -m bridge_db --promote-section career  # operator label promotion (TTY only)
```

## Architecture

- **DB**: `~/.local/share/bridge-db/bridge.db` (WAL mode, `PRAGMA busy_timeout=15000`). Schema at v13 — v6 added `canonical_key` to `pending_handoffs`; v7 added `source_trust` provenance columns to all four instruction-bearing tables; v8 added `shipped_event_dispositions`; v9 added `session_costs`; v10 added context-section integer `version` CAS, `context_section_export_state`, and durable `write_conflicts` receipts; v11 backfills activity `tags` into `content_index`; v12 adds the `session_classification` sidecar for heuristic cost-routing attribution while keeping `session_costs` as pure actuals; v13 adds `claimed_by` to `pending_handoffs` (the INV-13 claimant gate for `clear_handoff`). Auth state lives in `principals.json` (not the DB); no schema change for Stage-1 auth.
- **MCP transport**: stdio (stdout = JSON-RPC, all logging → stderr)
- **MCP tools**: verify the current count with `rg '@mcp\.tool' src/bridge_db -c`. As of the 2026-06-20 source check, there are 26 tools across 10 modules: activity, handoffs, context, snapshots, cost, export, health, recall (FTS5 lexical search; Phase −1 of the semantic memory layer), audit (read-side observability over the JSONL audit + recall query logs), and conflicts (`get_write_conflicts`). `get_recent_activity` is the raw row-level feed; `get_activity_signal` is the operator-facing feed that compresses lifecycle `session-boundary` telemetry. `health` / `status` include signals for pending handoffs, raw and actionable unprocessed shipped events, receiptless processed shipped events, FTS index drift, WAL size, and bridge-file freshness.
- **Context access**: `get_db(ctx)` helper casts lifespan context to `aiosqlite.Connection`
- **Tool registration**: `CaptureMCP` pattern in tests — decorators capture raw async fns
- **FTS5 invariant**: every write path that touches `context_sections`, `activity_log`, `system_snapshots`, or `pending_handoffs` calls `upsert_fts_entry` / `gc_fts_orphans` from [db.py](src/bridge_db/db.py) in the same transaction. Auto-prune paths in `log_activity` and `save_snapshot` GC orphan FTS rows. (`canonical_key` is not FTS-indexed, so it does not affect this invariant.)
- **Canonical resolution**: `log_activity` resolves `project_name` → GHRA `repo_full_name` on write via [project_resolver.py](src/bridge_db/project_resolver.py), a read-only consumer of GithubRepoAuditor's `project-registry.json` (path overridable via `BRIDGE_DB_PROJECT_REGISTRY_PATH`). The resolved key is stored in `activity_log.canonical_key` and returned by `log_activity` / `get_recent_activity`. Pass-through (key stays `NULL`) when the registry file is absent, so logging is unchanged; a present-but-unmatched or repo-less name stays `NULL` and unmatched names are flagged via the audit log (`log_activity.unmatched_project`), not silently recorded. Existing rows can be reconciled with `uv run python -m bridge_db --reconcile-canonical-keys`, which also writes an audit count receipt.

## Conventions

- `caller` parameter on write tools enforces ownership (`CallerID = Literal["cc","codex","claude_ai","notion_os","personal_ops"]`)
- `source`/`system` DB columns map 1:1 from `caller`
- Activity retention: unprotected rows keep the newest 50 per source; rows tagged `SHIPPED` or `LEDGER` (case-insensitive) are permanently retained — **BD-INV-1: retention never deletes a protected row, its receipt, or its disposition.** Enforced by the prune predicate, the `log_activity.prune` audit line, and health's `ledger_protected_count`/`receipt_orphan_count`/`disposition_orphan_count` metrics. Snapshot retention: 10 per system family (Codex operating and consulted-node snapshots are retained independently)
- Export trigger: consumers call `export_bridge_markdown` explicitly after writes
- Startup sync trigger: Claude Code `/start` calls `sync_from_file` before bridge reads so Claude.ai-owned file edits are imported into SQLite first
- Context CAS: consumers must pass `if_match_version` from `get_section` to
  `update_section`; stale writes return conflict receipts. The default is
  `BRIDGE_DB_CONTEXT_CAS_MODE=enforce` (flipped 2026-07-10 on the DST INV-4
  evidence seed): existing-row blind writes are rejected with a durable
  receipt. `warn` is the rollback lever — blind writes accepted, flagged,
  and receipted with the displaced content's version + sha.
- Export-state CAS: `export_bridge_markdown` records context section
  version/hash; `sync_from_file` refuses stale fallback-file imports and records
  `write_conflicts` receipts.
- Logging: `logging.basicConfig(stream=sys.stderr)` — never stdout
- **Channel auth (Stage 1)**: each client's MCP spawn env carries `BRIDGE_DB_PRINCIPAL_TOKEN`;
  the server binds the connection to one principal at startup (`principals.json`, managed via
  `--enroll`). `BRIDGE_DB_AUTH_MODE` = `off` (legacy) | `warn` (allow + audit mismatches) |
  `enforce` (reject); unrecognized values fail closed to `enforce`. With auth active:
  no MCP write may mint `source_trust='operator'` (clamped to `agent`, audited), and
  `sync_from_file` imports changed file content as `ingested` (promote via
  `--promote-section`, TTY-only).

## Gotchas

- **SessionEnd hook path**: Claude Code's SessionEnd hook must use `uv run --directory ~/Projects/bridge-db python -m bridge_db --log-session-boundary <project>` so session-boundary activity rows get FTS entries through the normal write path. This hook-specific path intentionally does not run activity retention pruning.
- **Activity signal vs raw activity**: `get_recent_activity` preserves raw compatibility and returns lifecycle rows as stored. Operator-facing consumers should use `get_activity_signal` so repeated Claude Code `SessionEnd` rows collapse into count/first/last aggregates without deleting rows or changing audit history.
- **Activity date semantics**: `activity_log.timestamp` is the caller's logical activity date or timestamp; `created_at` is the UTC insertion timestamp. Activity `since` filters (`get_recent_activity`, `get_activity_signal`, `get_shipped_events`) match either field so a closeout created after UTC midnight is still discoverable even if its logical timestamp is the prior operator-local date.
- **Write conflicts**: use `get_write_conflicts(status="open")` to inspect
  stale `update_section` attempts, stale markdown imports, and raced handoff
  claims. Receipts are diagnostic state, not instructions to retry blindly.
  `health`/`status` surface `open_write_conflicts` (+ oldest age) as a soft
  signal that never folds into `ok`; `get_pending_handoffs(status="active"|"all")`
  exposes live claims (`claimed_by`, `picked_up_at`) — the default stays
  `pending`.
- **Shipped-event sync**: `confirm_shipped_sync` requires a downstream system/ref, stores a receipt, then adds `PROCESSED`. `record_shipped_event_disposition` records a non-receipt policy disposition without adding `PROCESSED` — dispositioned rows no longer re-appear under `get_shipped_events(unprocessed_only=True)`, which now excludes both `PROCESSED` and dispositioned rows. `mark_shipped_processed` is a non-shipped-only legacy compatibility path; it refuses `SHIPPED` rows, so bridge-sync work must use receipt-backed proof or explicit disposition.
- **Durable ledger (BD-INV-1)**: rows tagged `SHIPPED` or `LEDGER` (case-insensitive) are retention-exempt; `log_activity`'s docstring distinguishes them (`SHIPPED` = downstream sync obligation, `LEDGER` = durable operator catch-up entry). Every prune emits a `log_activity.prune` audit line naming the deleted ids and tags. `get_activity_signal` surfaces protected rows as `kind:"ledger"` entries, and `export_bridge_markdown` renders a `## Pinned Ledger` section. See `docs/internal/ACTIVITY-LEDGER-DISCOVERY-2026-07-09.md` for the discovery audit that motivated this.
- **Semantic memory scope closed**: FTS5 + `recall` is the final layer (Phase −1). Vector/embedding layers are ruled out — most query misses reflect content not in `bridge.db`. See closure banner in `docs/internal/bridge-db-semantic-memory-IMPLEMENTATION-PLAN-v2.1.md`.
- **FTS drift repair**: `--rebuild-content-index` is the CLI-only repair path; FTS index drift is treated as a hard health failure because `recall` depends on `content_index` mirroring source tables.
- **Post-sync review**: after scheduled Bridge Syncs or shipped-event reconciliation, use `docs/internal/POST-SYNC-REVIEW.md` to verify DB state, markdown export freshness, and scorecard updates.
- **Dependency drift**: check with `uv tree --outdated`; refresh `uv.lock` and re-run the full verifier to confirm green.

## Registration

```bash
claude mcp add --scope user bridge-db \
  --env BRIDGE_DB_PRINCIPAL_TOKEN=<cc-token> \
  --env BRIDGE_DB_AUTH_MODE=warn \
  -- uv run --directory ~/Projects/bridge-db python -m bridge_db
```

## Test fixtures

- `db` fixture: `tmp_path / "test.db"` with WAL mode + schema applied
- `make_ctx(conn)`: mock Context satisfying `ctx.request_context.lifespan_context.db`
- `CaptureMCP`: `FastMCP` subclass that captures registered tool fns by name

<!-- portfolio-context:start -->
# Portfolio Context

## What This Project Is

bridge-db is an active local project in the ~/Projects portfolio.

## Current State

This project is in steady-state maintenance. The codebase is stable, the DB is live, core features are shipped and documented, and observability over the two JSONL logs is now closed (was a half-built feedback loop). The current tool surface should be verified from source with `rg '@mcp\.tool' src/bridge_db -c`; the verifier is `uv run pytest`, `uv run pyright`, and `uv run ruff check`. Scope is explicitly pinned to cross-system *state* coordination plus lexical `recall`, shipped-event sync receipts and dispositions, plus observability — expansion into a knowledge store is ruled out.

## Stack

- **Language**: Python 3.12+
- **MCP transport**: stdio (MCP SDK)
- **Database**: SQLite via `aiosqlite`
- **Type checking**: pyright (strict)
- **Lint**: ruff
- **Test**: pytest (`uv run pytest`; do not hardcode the current test count)

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
uv run python -m bridge_db --reconcile-canonical-keys  # backfill GHRA repo_full_name keys
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
