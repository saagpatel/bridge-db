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
uv run python -m bridge_db --shared-runtime-status  # no-secret broker/client inventory
uv run python -m bridge_db --dogfood # read-only observability dogfood pass
uv run python -m bridge_db.evidence_policy plan  # content-bound evidence inventory
uv run python -m bridge_db --rebuild-content-index  # repair FTS recall index drift
uv run python -m bridge_db --reconcile-canonical-keys  # backfill GHRA repo_full_name keys
uv run python -m bridge_db --log-session-boundary bridge-db  # FTS-safe CC hook logging
uv run python -m bridge_db --seal-recovery-batch <batch-id>  # scoped CC/Codex terminal recovery receipt
uv run python -m bridge_db          # start MCP server (stdio)
uv run python -m bridge_db.migration  # migrate from bridge markdown
uv run python -m bridge_db --enroll cc            # enroll/rotate a principal (TTY only)
uv run python -m bridge_db --upgrade-principals-v2  # preserve v1 hashes; add scoped 90-day grants
uv run python -m bridge_db --list-principals      # show enrolled principals
uv run python -m bridge_db --revoke-principal cc  # revoke a principal (TTY only)
uv run python -m bridge_db --promote-section career  # operator label promotion (TTY only)
uv run python -m bridge_db --promote-handoff 42      # reviewed handoff promotion (TTY only)
uv run python -m bridge_db --cancel-handoff 42 --cancel-reason "superseded"  # unclaimed only
uv run python -m bridge_db --recover-orphaned-handoff 42 --recovery-reason "session vanished"  # expired/legacy active claim
uv run python -m bridge_db --quarantine-cleared-operator-handoffs  # recoverable legacy relabel
uv run python -m bridge_db --restore-handoff-trust 42  # exact recovery image
python -m bridge_db.execution_generation readback --root <private-runtime-root>
python -m bridge_db.tenancy derive-activation-evidence --observations <private-replay-observations.json> --generation-id <generation-id>
python -m bridge_db.client_rebinding rebind --client claude-code --config-path /Users/d/.claude.json --backup-root <private-backup-root>
python -m bridge_db.tenancy status --root <private-tenancy-root>
```

Immutable-generation verification binds the exact release file set, ownership,
modes, reviewed source, launcher, external interpreter bytes, and the declared
project runtime dependency closure needed by shared broker startup. Dependency
lockfiles remain source evidence; runtime dependency files are read through
no-follow descriptors, held as one complete authenticated file set through
digesting, and revalidated against their configured paths before acceptance,
while the standard library, shared libraries, OS runtime, and packages outside
the authenticated runtime dependency set remain outside the claim. Tenancy drain
is cooperative: reject new work, finish active work, then close the obsolete
server. After repairing any pending journal, activation and rollback fail closed
unless the owning recovery lifecycle reports a current verified anchor and seal.
Forward activation also requires an exact private replay-evidence bundle whose
policy, client coverage, and lifecycle-scenario coverage recompute successfully;
rollback remains the safety path without new replay evidence.

## Architecture

- **DB**: `~/.local/share/bridge-db/bridge.db` (WAL mode, `PRAGMA busy_timeout=15000`). Core schema is v23: v21 adds non-destructive exact conflict aggregation and explicit overflow counters; v22 adds non-destructive handoff cancellation/quarantine recovery tables; v23 adds hash-only session capabilities and orphan-recovery receipts. Durable owner-bound snapshot refusals use the additive `BridgeSnapshotRefusalSchemaV1` extension without advancing `user_version`, preserving core compatibility with the exact previous merged v23 runtime. Auth state lives in `principals.json` v2 (not the DB): grants expire after 90 days, carry a generation, and are limited to a caller-specific tool scope.
- **MCP transport**: client-facing stdio (stdout = JSON-RPC, all logging -> stderr). Direct mode owns one server lease per client. Opt-in shared mode execs a Python stdio relay and one credential/complete-launch-contract broker over a private Unix socket. The wrapper pins broker launches to the stable launcher path; each HTTP request renews a short-lived one-use relay capability in process memory, verifies the connected socket identity against a credential-authenticated broker receipt, and the broker consumes the matching lease hash before forwarding. Receipts retain only hashes, socket identity, and lifecycle metadata. The broker serializes database access across sessions and exits after its bounded no-client window. `BridgeSharedRuntimeInventoryV1` supplies aggregate no-secret readback, while health/status use `BridgeSharedRuntimeReadinessV1` to require the exact current group, broker identity, credential-authenticated receipt, and connected socket identity; an unrelated active group cannot make selected shared transport green. Obsolete generations refuse new work and cooperatively close only after active requests finish.
- **MCP tools**: verify the current count with `rg '@mcp\.tool' src/bridge_db -c`. There are 26 tools across 10 modules: activity, handoffs, context, snapshots, cost, export, health, recall (FTS5 lexical search; Phase −1 of the semantic memory layer), audit (read-side observability over the JSONL audit + recall query logs), and conflicts (`get_write_conflicts`). Snapshot callers can inspect family capacity before writing and durably acknowledge an exact refusal. `get_recent_activity` is the raw row-level feed; `get_activity_signal` is the operator-facing feed that compresses lifecycle `session-boundary` telemetry. `health` / `status` include signals for pending handoffs, snapshot refusals, raw and actionable unprocessed shipped events, receiptless processed shipped events, FTS index drift, WAL size, and bridge-file freshness.
- **Context access**: `get_db(ctx)` helper casts lifespan context to `aiosqlite.Connection`
- **Tool registration**: `CaptureMCP` pattern in tests — decorators capture raw async fns
- **FTS5 invariant**: every write path that touches `context_sections`, `activity_log`, `system_snapshots`, or `pending_handoffs` calls `upsert_fts_entry` / `gc_fts_orphans` from [db.py](src/bridge_db/db.py) in the same transaction. Auto-prune paths in `log_activity` and `save_snapshot` GC orphan FTS rows. (`canonical_key` is not FTS-indexed, so it does not affect this invariant.)
- **Canonical resolution**: `log_activity` resolves `project_name` → GHRA `repo_full_name` on write via [project_resolver.py](src/bridge_db/project_resolver.py), a read-only consumer of GithubRepoAuditor's `project-registry.json` (path overridable via `BRIDGE_DB_PROJECT_REGISTRY_PATH`). The resolved key is stored in `activity_log.canonical_key` and returned by `log_activity` / `get_recent_activity`. Pass-through (key stays `NULL`) when the registry file is absent, so logging is unchanged; a present-but-unmatched or repo-less name stays `NULL` and unmatched names are flagged via the audit log (`log_activity.unmatched_project`), not silently recorded. Existing rows can be reconciled with `uv run python -m bridge_db --reconcile-canonical-keys`, which also writes an audit count receipt.

## Conventions

- `caller` parameter on write tools enforces ownership (`CallerID = Literal["cc","codex","claude_ai","notion_os","personal_ops"]`)
- `source`/`system` DB columns map 1:1 from `caller`
- Activity retention: unprotected rows keep the newest 50 per source; rows tagged `SHIPPED` or `LEDGER` (case-insensitive) are permanently retained — **BD-INV-1: retention never deletes a protected row, its receipt, or its disposition.** Enforced by the prune predicate, the `log_activity.prune` audit line, and health's `ledger_protected_count`/`receipt_orphan_count`/`disposition_orphan_count` metrics. At the v14 boundary the two `*_orphan_count` metrics kept their names but changed meaning: shipped-sync state moved onto `activity_log` `sync_*` columns, so instead of FK-orphans they now measure disposition malformation (a `synced` row missing downstream proof; a disposition on a non-SHIPPED row or a policy disposition missing its reason) — the compensating detection control for the field requirements the old NOT NULL child-table columns enforced, and must always read 0. Snapshot retention: 10 per system family (Codex operating and consulted-node snapshots are retained independently); `get_snapshot_capacity` exposes pre-write capacity, preserve-mode refusal returns a durable ID and next state, and `acknowledge_snapshot_refusal` is owner-bound and never grants deletion authority. Explicit `prune_oldest` writes still emit `save_snapshot.prune` and return `pruned_count`.
- Export trigger: consumers call `export_bridge_markdown` explicitly after writes
- Recovery batch seal: a current, scoped `cc` or `codex` channel credential may
  run `--seal-recovery-batch <batch-id>` after the final authorized write.
  Identity comes from the bound token, not a caller argument. Success publishes
  one `RecoverySealReceiptV1` while SQLite's writer slot is still held;
  stale success replay fails closed, distinct retained batch IDs are capped at
  1024, and interruption/failure stay explicitly unsealed. See
  `docs/internal/RECOVERY-SEAL-PROTOCOL.md`.
- Startup sync trigger: Claude Code `/start` calls `sync_from_file` before bridge reads so Claude.ai-owned file edits are imported into SQLite first
- Context CAS: consumers must pass `if_match_version` from `get_section` to
  `update_section`; stale writes return conflict receipts. Existing-row
  blind writes (no `if_match_version`) are rejected unconditionally with a
  durable `missing_cas` receipt — there is no config dial (the
  `BRIDGE_DB_CONTEXT_CAS_MODE` canary that gated this behind `warn`/`enforce`
  was cut 2026-07-12 after a live-caller audit found zero blind writers).
  New-section inserts need no CAS token.
- Context ownership/provenance: `update_section` always requires an exact
  channel-bound registered steward (`claude_ai` for the four owned narrative
  sections, `cc` for `portfolio`). Every accepted MCP content version is
  relabeled `agent` by default (or explicit `ingested`); operator trust is
  restored only through the exact-version `--promote-section` ceremony.
- Export-state CAS: `export_bridge_markdown` records context section
  version/hash; `sync_from_file` refuses stale fallback-file imports and records
  `write_conflicts` receipts.
- Logging: `logging.basicConfig(stream=sys.stderr)` — never stdout
- **Channel auth (Stage 1)**: each client's MCP spawn env carries `BRIDGE_DB_PRINCIPAL_TOKEN`;
  the server binds the connection to one principal at startup (`principals.json`, managed via
  `--enroll`). `BRIDGE_DB_AUTH_MODE` = `off` (legacy) | `warn` (allow + audit mismatches) |
  `enforce` (reject); unrecognized values fail closed to `enforce`. With auth active,
  no MCP write may mint `source_trust='operator'` (clamped to `agent`, audited).
  In every auth mode, `sync_from_file` imports changed file content as `ingested` (promote via
  `--promote-section`, TTY-only, with exact content/version/hash review and locked recheck).
  `create_handoff` is stricter than the rollout dial:
  it always requires an exact channel-bound `claude_ai` principal and always clamps
  requested operator trust. Review and promote an exact pending row with
  `--promote-handoff <id>` (TTY-only) before either Claude Code or Codex pickup;
  consuming MCP clients cannot self-confirm non-operator handoffs.
  Version-1 registries fail closed in the new runtime; use
  `--upgrade-principals-v2` first to preserve deployed token hashes while adding
  issued/expiry timestamps, generation 1, and the closed caller scope.
- **Handoff completion**: role identity is necessary but no longer treated as
  session identity. `pick_up_handoff` returns a one-time 24-hour completion
  capability; only its hash is stored. `clear_handoff` requires the exact ID and
  bearer value, binds them to project/session/role/transition, and consumes the
  capability atomically. Never persist the bearer value. Pending and
  claimant-less rows use cancellation; expired or legacy claimed rows use the
  exact-row `--recover-orphaned-handoff` ceremony and durable receipt.

## Gotchas

- **SessionEnd hook path**: Claude Code's SessionEnd hook must use `uv run --directory ~/Projects/bridge-db python -m bridge_db --log-session-boundary <project>` so session-boundary activity rows get FTS entries through the normal write path. This hook-specific path intentionally does not run activity retention pruning.
- **Activity signal vs raw activity**: `get_recent_activity` preserves raw compatibility and returns lifecycle rows as stored. Operator-facing consumers should use `get_activity_signal` so repeated Claude Code `SessionEnd` rows collapse into count/first/last aggregates without deleting rows or changing audit history.
- **Activity date semantics**: `activity_log.timestamp` is the caller's logical activity date or timestamp; `created_at` is the UTC insertion timestamp. Activity `since` filters (`get_recent_activity`, `get_activity_signal`, `get_shipped_events`) match either field so a closeout created after UTC midnight is still discoverable even if its logical timestamp is the prior operator-local date. When `timestamp` is omitted, `log_activity` defaults to the UTC calendar date via the clock seam (`clock.now()`), consistent with snapshots; the grep-guard in `tests/test_clock_seam.py` keeps wall-clock reads out of every module except `clock.py`.
- **Write conflicts**: use `get_write_conflicts(status="open")` to inspect
  stale `update_section` attempts, stale markdown imports, and raced handoff
  claims. Receipts are diagnostic state, not instructions to retry blindly.
  Exact evidence identities aggregate with `occurrence_count`, `first_seen_at`,
  and `last_seen_at`; legacy rows remain labeled `legacy`. After 10,000 distinct
  new identities, further novel conflicts aggregate into an explicit
  `capacity_overflow` row per surface rather than growing the table.
  `health` reports `open_write_conflicts` + `oldest_open_conflict_age_hours`;
  `status` carries the count in `signals`; `--status`/`--dogfood` print it.
  All soft signals — never folded into `ok` and never a dogfood gate.
  `get_pending_handoffs(status="active"|"all")` exposes live claims
  (`claimed_by`, `picked_up_at`, non-secret `claim_session_id`, and capability
  expiry) — the default stays `pending`.
- **Shipped-event sync**: one verb, `record_disposition(caller, activity_id, disposition, ...)`, writes a SHIPPED row's terminal sync state onto the `activity_log` `sync_*` columns (schema v14). Every terminal disposition is source-owned and requires an exact channel-bound caller matching the activity row's `source`. `disposition='synced'` additionally REQUIRES `downstream_system` + `downstream_ref` and adds `PROCESSED`. A policy `disposition` (`unsynced_by_policy` / `no_durable_target` / `superseded_without_receipt` / `declined_mapping`) REQUIRES a `reason`, records why the event is not receipt-backed, and does NOT add `PROCESSED`. Cross-source receipt verification or policy adjudication needs an explicit delegation contract and cannot borrow the source caller. A row that already carries `synced` proof cannot be downgraded to a policy disposition. `get_shipped_events(unprocessed_only=True)` excludes both `PROCESSED` rows and any row with a `sync_disposition`. This replaces the former `confirm_shipped_sync` / `record_shipped_event_disposition` / `mark_shipped_processed` trio; the legacy non-shipped `PROCESSED`-marking path is retired (`record_disposition` is SHIPPED-only). NOTE: `~/.claude/hooks/mcp-guard.sh` still carries a now-dead `mark_shipped_processed` pattern — operator handles that separately.
- **Durable ledger (BD-INV-1)**: rows tagged `SHIPPED` or `LEDGER` (case-insensitive) are retention-exempt; `log_activity`'s docstring distinguishes them (`SHIPPED` = downstream sync obligation, `LEDGER` = durable operator catch-up entry). Every prune emits a `log_activity.prune` audit line naming the deleted ids and tags. `get_activity_signal` surfaces protected rows as `kind:"ledger"` entries, and `export_bridge_markdown` renders a `## Pinned Ledger` section. See `docs/internal/ACTIVITY-LEDGER-DISCOVERY-2026-07-09.md` for the discovery audit that motivated this.
- **Capacity limits**: new activity, handoff, snapshot, and context writes use
  the documented UTF-8/JSON budgets in README. Oversized input is rejected
  before mutation with a stable code. Legacy oversized rows remain readable;
  no migration deletes or truncates them. The open handoff quota is 100 and
  the total retained handoff quota is 10,000; a full legacy history rejects
  creation without deleting rows. `get_pending_handoffs` pages with `limit` +
  `before_id`.
- **Semantic memory scope closed**: FTS5 + `recall` is the final layer (Phase −1). Vector/embedding layers are ruled out — most query misses reflect content not in `bridge.db`. See closure banner in `docs/internal/bridge-db-semantic-memory-IMPLEMENTATION-PLAN-v2.1.md`.
- **Durable evidence lifecycle**: audit and minimized recall telemetry use
  locked, fsync'd, lossless active-file rotation; no segment is automatically
  deleted. A primary audit-write failure creates an independent minimized
  durable receipt and degrades health; if both evidence paths fail,
  `AuditUnavailableError` prevents a silent auditable-success claim. Verified
  migration backups include digest/integrity/schema readback plus metadata and
  remain approval-gated for cleanup. `bridge_db.evidence_policy` can plan,
  verified-copy, and acknowledge exact evidence snapshots without granting
  cleanup authority. With explicit approval, its archive-bound legacy recall
  redaction preserves records and aggregate semantics while prepared/completed
  receipts expose partial work as degraded health. It never grants authority
  to delete records, segments, backups, archives, or recovery evidence. See
  `docs/internal/EVIDENCE-LIFECYCLE.md`.
- **Recovery lifecycle ownership**: `RecoveryAnchorV1` remains strictly
  source-current. The batch-seal command is available only to current `cc` and
  `codex` grants carrying `seal_recovery_batch`; existing grants fail closed
  until separately re-enrolled. The receipt identifies the sealer without
  claiming ownership of another principal's rows or snapshots. The checkpoint
  LaunchAgent remains WAL-only, and reads never auto-seal or create recovery
  seal evidence.
- **Execution generations**: staging requires an exact clean reviewed SHA and
  binds tracked source, dependency, contract, interpreter, and launcher
  digests. Activation uses atomic `current`/`previous` pointers, interruption
  journals, exact readback receipts, and cooperative drain markers. It never
  kills a process or deletes a release. Forward activation requires content-bound
  replay observations and their exactly recomputed tenancy policy before any
  pointer write. Runtime health is verified only when
  the complete immutable release reads back. Codex credential rotation/binding
  accepts the secret only through a protected descriptor and never emits the
  secret or digest. Exact Claude JSON rebinding writes a private backup and
  preserves environment values without returning them. Source-owned Codex and
  checkpoint launch inputs converge on the stable `current` launcher, but need
  a separately controlled install/reconnect before they are live. See
  `docs/internal/EXECUTION-GENERATIONS.md`.
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
- **MCP transport**: stdio (MCP SDK), with an opt-in private Unix-socket shared broker behind the stable wrapper
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
uv run python -m bridge_db --shared-runtime-status  # no-secret broker/client inventory
uv run python -m bridge_db --dogfood # read-only observability dogfood pass
uv run python -m bridge_db --rebuild-content-index  # repair FTS recall index drift
uv run python -m bridge_db --reconcile-canonical-keys  # backfill GHRA repo_full_name keys
uv run python -m bridge_db --log-session-boundary bridge-db  # FTS-safe CC hook logging
uv run python -m bridge_db --promote-handoff 42  # reviewed handoff promotion (TTY only)
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
