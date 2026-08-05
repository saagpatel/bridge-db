# bridge-db

![CI](https://github.com/saagpatel/bridge-db/actions/workflows/ci.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)

SQLite-backed MCP server for shared state across Claude.ai, Claude Code, Codex, and related local ops tools.

bridge-db replaces ad hoc edits to a shared markdown file with a structured SQLite store and a focused MCP tool surface: cross-system state, FTS5 lexical `recall`, shipped-event sync receipts, shipped-event dispositions, and observability over the audit and recall logs. The markdown bridge file is regenerated from the DB via `export_bridge_markdown` and remains available as a fallback for file-based clients.

## Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** — fast Python package manager

```bash
git clone https://github.com/saagpatel/bridge-db.git
cd bridge-db
uv sync          # install all deps into .venv
uv run pytest    # verify the install
```

## Status

- Steady maintenance. Scope is cross-system *state* coordination, lexical `recall`, and observability — not a general knowledge store.
- Schema v10: context sections carry monotonic `version` tokens; stale writes and raced handoff claims produce durable `write_conflicts` receipts.
- Schema v12: adds the `session_classification` sidecar for heuristic cost-routing attribution while keeping `session_costs` as pure actuals. Schema v11 backfills activity `tags` into `content_index` so lifecycle tags (SHIPPED, DECISION, ...) are recall-able on existing DBs.
- Schema v13: adds `claimed_by` to `pending_handoffs` (the INV-13 claimant gate for `clear_handoff`). Riding the same migration train, activity retention now exempts rows tagged `SHIPPED` or `LEDGER` (case-insensitive) from the 50-per-source prune — BD-INV-1: retention never deletes a protected row, its receipt, or its disposition.
- Schema v14: collapses the shipped-sync trio (`shipped_sync_receipts` + `shipped_event_dispositions`) into `activity_log` `sync_*` disposition columns and drops the two child tables. A shipped event's terminal sync state (a `synced` downstream receipt or a policy disposition) now lives on the activity row itself, written by the single `record_disposition` verb. Because the state is the row, BD-INV-1's guarantee is structural — no FK-CASCADE can orphan a receipt.
- Schema v21: adds non-destructive write-conflict identity aggregation. Exact repeat conflicts increment `occurrence_count`; distinct identity growth is capped at 10,000 and then rolls into an explicit per-surface overflow aggregate. Legacy receipts remain unchanged and are labeled `aggregation_state="legacy"`.
- Schema v22 adds append-only handoff cancellation receipts and exact-row trust quarantine images. Legacy cleared operator rows can be relabeled `ingested` without deletion and restored only while the captured row still matches.
- Schema v23 adds one-time, 24-hour handoff completion capabilities and append-only orphan-recovery receipts. The database stores only capability hashes; the claiming session receives the sole bearer value.
- The additive `BridgeSnapshotRefusalSchemaV1` extension over core v23 adds durable, owner-bound snapshot-capacity refusal receipts and explicit acknowledgement/next-state handling without advancing `user_version`. A refusal stores only a payload digest, never the rejected snapshot body.
- FTS5 `content_index` mirrors all content tables; `health` and `status` verify source-row / FTS-row alignment.
- `status` includes a native freshness block for owner-specific snapshot, activity, handoff, and shipped-event attention. Freshness attention is advisory: top-level `ok` / `overall` remain tied to DB, schema, fallback-file, and FTS health.
- 26 MCP tools across 10 modules (activity, handoffs, context, snapshots, cost, export, health, recall, audit, conflicts).

## Architecture

```
Claude.ai ──────────────────────────────────────────────┐
  (direct MCP via Claude Desktop)                       │
  (fallback: markdown file via Filesystem MCP)          │
                                                         ▼
CC skills ──► MCP stdio ──► bridge-db process ──► SQLite (WAL)
Codex      ──► MCP stdio ──► bridge-db process ──►  ~/.local/share/bridge-db/bridge.db
                                                         │
                                              export_bridge_markdown
                                                         │
                                                         ▼
                                           ~/.claude/projects/<encoded-home>/
                                           memory/claude_ai_context.md
```

No shared daemon. Each MCP client spawns its own `bridge-db` process via stdio. WAL mode + `PRAGMA busy_timeout=15000` handles concurrent writer waiting; logical stale-write protection comes from CAS on mutable context sections.

## Tools

Verify the current tool count from source with
`rg '@mcp\.tool' src/bridge_db -c`. As of the 2026-07-12 v14 collapse, the
surface is 26 tools across these 10 modules:

| Module | Tools |
|---|---|
| activity | `log_activity`, `get_recent_activity`, `get_activity_signal`, `get_shipped_events`, `record_disposition` |
| handoffs | `create_handoff`, `get_pending_handoffs`, `pick_up_handoff`, `clear_handoff` |
| context | `update_section`, `get_section`, `get_all_sections`, `sync_from_file` |
| snapshots | `save_snapshot`, `get_snapshot_capacity`, `acknowledge_snapshot_refusal`, `get_latest_snapshot` |
| cost | `record_cost`, `get_cost_history` |
| export | `export_bridge_markdown` |
| health | `health`, `status` |
| recall | `recall`, `recall_stats` |
| audit | `audit_tail` |
| conflicts | `get_write_conflicts` |

Write tools enforce `caller` ownership, so systems can only write the slices of state they own. Recent hardening also added `notion_os` and `personal_ops` as first-class activity and cost writers.

## Provenance & the pickup gate

Instruction-bearing rows carry a `source_trust` label — `operator`, `agent`, or `ingested` — recording who authored the content. The canonical label lives in the DB (schema v7+, on `pending_handoffs`, `activity_log`, `context_sections`, `system_snapshots`). Markdown exports include a visible stored-data warning and advisory per-block copies of the DB labels so file consumers do not lose the instruction boundary. Because the fallback is editable, those serialized labels are never proof of operator authorship and are ignored on import.

- **Writers** set it via an optional `source_trust` param; the conservative default is `agent`. `create_handoff` and `update_section` require exact channel-bound owners even when the global auth rollout mode is `off`, and MCP requests for `operator` trust are clamped to `agent`. Every accepted section content change receives fresh provenance; it never inherits operator trust from an older version.
- **The gate** lives at the one dangerous transition — `pick_up_handoff` moving a handoff `pending → active`:
  - `operator`-trust → picks up in one call (`cc` and `codex`).
  - either client + non-`operator` → **refused**. The consuming MCP principal cannot confirm its own input. Review and promote the exact pending row first with `uv run python -m bridge_db --promote-handoff <id>` in an interactive terminal.
- **Visibility:** `get_pending_handoffs`, `get_section`, `get_all_sections`, `get_recent_activity`, `get_activity_signal`, `get_shipped_events`, `get_latest_snapshot`, and `recall` hits carry `source_trust` plus `instruction_boundary` metadata that tells consumers returned content is stored data, not instructions. Lifecycle aggregates use a trust summary and `source_trust="mixed"` when rows differ. `status` reports `pending_handoffs_by_trust` and `health` a full per-table `source_trust_breakdown`. Each gate decision (`allowed` / `refused`) is written to the audit log.

> MCP clients cannot mint operator provenance. An operator-directed handoff is created as `agent`, reviewed in an interactive terminal, and promoted with `uv run python -m bridge_db --promote-handoff <id>`. The promotion rechecks the exact reviewed row under a write lock and refuses changed or non-pending handoffs.

`pick_up_handoff` now returns `claim_session_id`, `completion_capability`,
`capability_expires_at`, and `allowed_transition="clear"`. The client must retain
the capability only in the claiming session and pass it, with the exact
`handoff_id`, to `clear_handoff`. Do not put the bearer value in logs, bridge
markdown, handoff files, or durable memory. `get_pending_handoffs(status="active")`
surfaces the non-secret session ID and expiry for operator diagnosis.

`clear_handoff` retains the channel-bound `cc` / `codex` role check, then binds
completion to the exact handoff, project (including canonical aliases), claiming
session, and `clear` transition. It atomically consumes the capability; missing,
malformed, wrong-target, expired, recovered, and replayed values fail closed with
explicit `reason_code` values and no lifecycle mutation. The new arguments are
optional at the MCP schema boundary so old clients still connect, but a live
claim cannot complete without them.

Pending and legacy claimant-less active rows still require the exact-ID
`--cancel-handoff <id> --cancel-reason <reason>` operator ceremony. A claimed
legacy row without a capability, or a capability-backed claim after expiry, can
be retired only with the TTY-gated
`--recover-orphaned-handoff <id> --recovery-reason <reason>` ceremony. Recovery
rechecks the exact reviewed row under a write lock, retires any expired bearer
state, and records a durable `handoff_orphan_recovery_receipts` row.

## CAS & Conflict Receipts

Context sections are the mutable bridge surface, so they carry a monotonic
`version` token. Consumers should read with `get_section`, edit locally, then
call `update_section(..., if_match_version=<version>)`. A stale token returns
`ok=false`, `conflict=true`, and a `receipt_id` instead of clobbering a newer
row. `if_match_updated_at` remains as a compatibility guard, but `version` is
the preferred token because timestamps have one-second resolution.

Existing-row blind writes are rejected unconditionally with a durable
`missing_cas` receipt — `if_match_version` (or `if_match_updated_at`) is
required for any write to a section that already exists. New section
inserts without CAS remain allowed, since there is nothing to CAS against.

`export_bridge_markdown` records the exported version/hash for each rendered
Claude.ai-owned context section. Later `sync_from_file` imports a changed
fallback-file section only if the DB still matches that exported base. If the DB
has advanced, the import is rejected and recorded in `write_conflicts`. Every
changed or new file section is labeled `ingested` regardless of auth rollout
mode; unchanged content preserves its existing label. Promote reviewed content
with `--promote-section <section>`: the TTY ceremony displays and confirms the
exact version and digest, then rechecks it under a write lock before granting
operator trust.

The export result's `bytes` field and the durable
`bridge_export_receipts.byte_count` value both report the exact UTF-8 byte count
written to the fallback file.

Generated fallback files wrap each editable section in explicit
`bridge-db:owned-section` HTML markers. This keeps nested Markdown headings
inside long-form section content round-trippable. Pre-marker files remain
readable through a strict legacy parser that recognizes only the four owned
headings and known generated document headings as section boundaries. Health
reports the projection as `untracked` until an authenticated export records the
whole-file export state; matching section text alone is not sufficient proof
that a legacy file is safe to overwrite.

Exports to the real Claude.ai fallback path are also guarded against empty
fixture-like output: bridge-db refuses to overwrite that file when all four core
Claude.ai-owned sections would render as `_Not yet populated._`. Set
`BRIDGE_DB_ALLOW_EMPTY_BRIDGE_EXPORT=1` only for an intentional empty bootstrap.

Use `get_write_conflicts(status="open")` to inspect stale section writes,
stale markdown imports, and raced handoff claims.

## Commands

```bash
uv run pytest              # run all tests
uv run pyright             # type check (strict mode)
uv run ruff check          # lint
uv run python -m bridge_db --doctor  # local environment diagnostics
uv run python -m bridge_db --status  # compact operator summary
uv run python -m bridge_db --dogfood # read-only observability dogfood pass
uv run python -m bridge_db --rebuild-content-index  # repair FTS recall index drift
uv run python -m bridge_db --reconcile-canonical-keys  # backfill GHRA repo_full_name keys
uv run python -m bridge_db --log-session-boundary bridge-db  # FTS-safe CC hook logging
uv run python -m bridge_db --upgrade-principals-v2  # preserve v1 hashes; add 90-day scoped grants
uv run python -m bridge_db --promote-handoff 42  # review/promote one pending handoff (TTY only)
uv run python -m bridge_db --cancel-handoff 42 --cancel-reason "superseded"  # exact unclaimed cancellation
uv run python -m bridge_db --recover-orphaned-handoff 42 --recovery-reason "claiming session vanished"  # expired/legacy active claim
uv run python -m bridge_db --quarantine-cleared-operator-handoffs  # recoverable legacy relabel
uv run python -m bridge_db --restore-handoff-trust 42  # exact-row recovery
python -m bridge_db.execution_generation readback --root <private-runtime-root>
python -m bridge_db.secure_binding --caller codex --secret-fd 3 --principals-path <path> --binding-path <path>
python -m bridge_db.client_rebinding rebind --client claude-code --config-path /Users/d/.claude.json --backup-root <private-backup-root>
python -m bridge_db.tenancy status --root <private-tenancy-root>
uv run python -m bridge_db          # start MCP server (stdio)
uv run python -m bridge_db.migration  # migrate from bridge markdown
```

The private Codex baseline seed accepts explicit versioned fingerprints and
uses the same no-silent-prune admission service as `save_snapshot`. It never
deletes retained snapshots directly; a full target family produces a durable
refusal ID and blocks the paired activity write until the owner handles the
refusal.
Legacy `snapshot-v1` is accepted only before `2026-08-18T00:00:00Z`; unknown
versions and expired v1 manifests fail closed. New manifests should use the whole-manifest
`manifest-v2` contract documented in
[`docs/internal/CODEX-SEED-MANIFEST.md`](docs/internal/CODEX-SEED-MANIFEST.md).

## Registration

Replace `/path/to/bridge-db` below with the absolute path to your clone of this repo
(e.g. `$(pwd)` if you are already inside it, or `~/Projects/bridge-db` as a common convention).

**Claude Code (user-scoped):**
```bash
claude mcp add --scope user bridge-db -- uv run --directory /path/to/bridge-db python -m bridge_db
```

**Codex (`~/.codex/config.toml`):**
```toml
[mcp_servers.bridge-db]
command = "uv"
args = ["run", "--directory", "/path/to/bridge-db", "python", "-m", "bridge_db"]
```

For immutable activation, point client launchers at
`<private-runtime-root>/current/bin/bridge-db-mcp` instead of a mutable checkout.
See [`docs/internal/EXECUTION-GENERATIONS.md`](docs/internal/EXECUTION-GENERATIONS.md)
for deterministic staging, atomic activation/readback, rollback, cooperative
drain, and the no-secret-output Codex binding path. `health`/`status` label a
direct mutable checkout as operating attention; that is not proof of an
installed generation.

The source-owned `config/bridge-db-mcp-immutable` is the Codex install input. It
parses token/auth-mode plus an optional `BRIDGE_DB_TRANSPORT_MODE=direct|shared`
from an owner-only env file. `direct` is the default rollback. `shared` retains
stdio for the client while a thin shell relay uses one authenticated,
generation-bound broker over a private Unix socket; command-line maintenance
operations remain direct. The broker has no TCP listener or LaunchAgent,
serializes database access across MCP sessions, and exits after the final relay
has been absent for its bounded idle window.
`config/com.saagar.bridge-db-checkpoint.plist` preserves the existing
30-minute receipt wrapper through the reviewed operator-script pointer while
running that launcher with `--checkpoint`.
`bridge_db.client_rebinding` performs exact Claude Code/Desktop JSON command
replacement with private digest-named backups, exact readback, digest-bound
restore, and no environment-value output. None of these source assets establish
that a live client has reloaded them.

## Data

- **DB**: `~/.local/share/bridge-db/bridge.db`
- **Schema compatibility**: core `user_version=23` plus the additive backward-readable refusal extension, verified against exact previous merged generation `d7272d489873faa5ed84c81734636ffc8cecb095`. Activation and pointer rollback enforce the owning recovery lifecycle's current verified anchor/seal verdict after repairing any pending journal; source compatibility is not activation authority.
- **MCP tenancy**: Inventory V2 separates live identity-bound processes from stale lease files and reports current RSS separately from lease-last-observed RSS. Direct servers and shared brokers account for requests, PID ancestry, and generation. Obsolete generations refuse new work and cooperatively close after active requests finish; lifecycle tooling cannot terminate another process.
- **Bridge file**: `~/.claude/projects/<encoded-home>/memory/claude_ai_context.md`
  (Claude Code encodes your home dir path by replacing `/` with `-`; the default is derived
  automatically at runtime — override via `BRIDGE_FILE_PATH` if needed)
- Retention: unprotected activity entries keep the newest 50 per source; rows
  tagged `SHIPPED` or `LEDGER` (case-insensitive) are permanently retained
  (BD-INV-1); 10 snapshots per system family (Codex operating and
  consulted-node snapshots are retained independently). Snapshot writes default
  to `retention_policy="preserve_existing"`: bridge-db takes the SQLite writer
  slot and either inserts without any retention deletion or returns
  `ok=false`, `reason_code="snapshot.retention_would_prune"` before mutation.
  The legacy deletion path requires an explicit
  `retention_policy="prune_oldest"` call.
- Health check: `health` MCP tool or `uv run python -m bridge_db --doctor`
- Operator summary: `uv run python -m bridge_db --status`
- Dogfood pass: `uv run python -m bridge_db --dogfood` bundles the status, FTS index, WAL, recall, and shipped-sync audit checks used after bridge-sync runs
- Current recovery anchor: `uv run python -m bridge_db --create-recovery-anchor`
  creates one private, atomically published `RecoveryAnchorV1` bundle using
  SQLite's online-backup API. It never overwrites an existing anchor. Verify it
  independently with `uv run python -m bridge_db --verify-recovery-anchor`;
  verification opens a disposable copy, runs SQLite integrity and schema
  checks, and compares bounded table counts without returning database content.
  After a separately approved write makes that valid bundle stale, use
  `uv run python -m bridge_db --rotate-recovery-anchor`. Rotation is
  preservation-idempotent when the anchor is already current; otherwise it
  stages and verifies a new bundle, atomically exchanges it into the stable
  current path under SQLite's writer slot, and retains the prior bundle under a
  timestamped `.superseded-*` sibling. After an upward schema migration,
  rotation independently verifies the older anchor against its disposable
  SQLite readback before preserving it and publishing the current-schema
  bundle; future-schema, corrupt, or ambiguously versioned evidence still fails
  closed. Invalid evidence, source races,
  unsupported atomic exchange, and post-exchange verification failures fail
  closed; a failed post-exchange verification rolls back to the previous
  current anchor.
- Evidence lifecycle: audit and recall JSONL active files rotate losslessly at
  configurable byte boundaries under an inter-process lock. All segments are
  preserved pending an approved retention policy. `health`/`status` expose
  active/segment bytes, audit degradation, historical raw-query inventory, and
  current recovery readiness separately from historical migration-backup
  provenance. Legacy backups without creation-time manifests remain
  `historical_unverified` even when SQLite-readable; a verified current anchor
  never rewrites or retroactively blesses them. See
  [docs/internal/EVIDENCE-LIFECYCLE.md](docs/internal/EVIDENCE-LIFECYCLE.md).
- Evidence policy: `uv run python -m bridge_db.evidence_policy plan` emits a
  non-sensitive, content-bound preservation plan. The companion `archive`
  command creates an atomic private copy, `verify` binds later readback to an
  independently retained plan digest, and `acknowledge` records review without
  granting cleanup authority. After explicit approval, `redact-legacy-recall`
  can remove only archived legacy `query` fields while preserving every
  telemetry record and its empty-query aggregate. Exact archive/count gates and
  prepared/completed receipts make interrupted work operator-visible. No
  command deletes records, segments, backups, archives, or recovery evidence.
- FTS repair: `uv run python -m bridge_db --rebuild-content-index` rebuilds the local `content_index` from source tables when health reports recall-index drift
- Canonical-key reconcile: `uv run python -m bridge_db --reconcile-canonical-keys` rewrites stored `activity_log` and `pending_handoffs` `canonical_key` values through GithubRepoAuditor's registry, storing GHRA `repo_full_name` for repo-backed projects and leaving unresolvable rows `NULL`.
- Session boundary logging: Claude Code's SessionEnd hook should call `uv run --directory /path/to/bridge-db python -m bridge_db --log-session-boundary <project>` rather than writing SQLite directly; this path adds the FTS row and does not run activity retention pruning
- Migration: `uv run python -m bridge_db.migration` (idempotent — safe to re-run)

The MCP `status` result separates storage integrity from operating freshness:

- `overall` and `storage_health`: `healthy` or `degraded`, based only on DB,
  schema, fallback-file, and FTS integrity. `overall` remains the compatibility
  alias for existing consumers.
- `operating_state`: `fresh`, `attention`, `stale`, or `unknown`, derived from
  the `freshness` block without changing command success semantics.
- `freshness`: the detailed operating-truth block described below.

- `thresholds_hours`: `snapshot_stale_after=48.0`, `activity_quiet_after=72.0`,
  `pending_handoff_stale_after=168.0`, and
  `active_handoff_stale_after=72.0`.
- `snapshots`: per-owner `cc` and `codex` entries with `state`, `owner`,
  `latest_snapshot_date`, `latest_created_at`, `age_hours`,
  `superseding_activity_id`, and `next_action`.
  Snapshot refresh actions stay owner-specific: `cc_refresh_snapshot` belongs
  to `cc`; `codex_refresh_snapshot` belongs to `codex`.
- `activity_sources`: `cc`, `codex`, `claude_ai`, `notion_os`, and
  `personal_ops` entries with `state`, `latest`, and `age_hours`.
- `handoffs`: pending/active counts, stale counts, oldest ages, and unknown-age
  counts for pending and active handoffs.
- `shipped_events`: raw unprocessed, actionable unprocessed,
  dispositioned/non-actionable unprocessed, processed-without-receipt count, and
  the shipped-event `next_action`.
- `overall`: `fresh`, `attention`, `stale`, or `unknown`.
- `next_actions`: up to five deterministic `{action, owner, reason}` entries.

Freshness states use a narrow vocabulary: `fresh` means recent enough for that
surface; `quiet` means an activity source has no recent rows and is not a
health failure; `superseded` means newer substantive owner activity exists
after the latest snapshot. Lifecycle-only rows tagged `session-boundary` remain
part of activity history and activity-source freshness, but do not supersede a
state snapshot. `stale` means an aged snapshot or handoff needs refresh/review;
`missing` means the expected owner row is absent; and `unknown` means a missing
or unparsable timestamp prevents age calculation.

The CLI status command prints freshness as compact hints:

```text
  Storage health: <healthy|degraded>
  Operating state: <fresh|attention|stale|unknown>
  Freshness: <overall>
  Next actions: <action> (<owner>), ...
```

These lines do not change the command's success semantics. `uv run python -m
bridge_db --status` still exits from bridge health, so freshness `attention` by
itself does not fail the command. bridge-db remains MCP-first and
SQLite-native; the markdown export is a fallback/mirror, and freshness adds no
service, table, migration, tool, or CLI flag.

`activity_log` retention is two-tier: unprotected rows remain recent context
capped at 50 per source, while rows tagged `SHIPPED` or `LEDGER` are
permanently retained (BD-INV-1). The durable ledger and each shipped event's
sync disposition (its downstream receipt or its policy decision) both live on
the activity row itself, in the `sync_*` columns added at schema v14 — a
protected row's receipt or disposition can no longer cascade-die with it,
because the state IS the row and the parent row never prunes. Treat
`processed_shipped_without_receipt=0`
and `fts_missing=0` as primary clean signals, and use
`actionable_unprocessed_shipped=0` with
`dispositioned_unprocessed_shipped>0` when policy dispositions explain why raw
`unprocessed_shipped` remains nonzero.

## Capacity policy

All new instruction-bearing writes are measured as UTF-8 and rejected before
mutation with stable error codes. Existing oversized rows are never deleted,
rewritten, or silently clipped; they remain readable and exportable.

- Activity: project name 4 KiB, summary 64 KiB, branch 4 KiB, timestamp 128
  bytes, at most 64 tags of 1 KiB each, and 128 KiB combined. Each source may
  retain at most 1,000 protected `SHIPPED`/`LEDGER` rows; the atomic insert is
  refused at quota without pruning protected history.
- Handoffs: project name 4 KiB, project path 16 KiB, roadmap path 4 KiB, phase
  64 KiB, and 72 KiB combined. At most 100 `pending` + `active` rows may be
  open, and at most 10,000 total history rows may be retained. A full legacy
  history is preserved and rejects new creation rather than deleting old
  records. `get_pending_handoffs(limit=..., before_id=...)` pages newest IDs
  with a default page of 100 and maximum of 200.
- Snapshots: compact JSON is limited to 256 KiB, depth 32, and 10,000 JSON
  nodes before insert or retention pruning. The default
  `retention_policy="preserve_existing"` atomically admits an under-limit
  family without pruning or refuses a full family before insert with
  `snapshot.retention_would_prune`. The legacy deletion path is available only
  through an explicit `retention_policy="prune_oldest"` call. Call
  `get_snapshot_capacity` before assembling a write. Every preserve-mode
  refusal returns a durable `refusal_id`, `acknowledgement_required=true`, and
  an explicit `next_state`; the bound owner records its decision with
  `acknowledge_snapshot_refusal`. Acknowledgement never grants deletion
  authority. Markdown bootstrap has one narrow migration exemption: it may
  seed a system only while that system has no snapshot rows, through the same
  admission service.
- Context: each section is limited to 256 KiB and the five-section registry to
  1 MiB total. An over-budget legacy database may accept a bounded replacement
  only when it reduces total bytes.
- Write conflicts: full evidence identity includes the surface, target,
  operation, principals, versions/timestamps, trust, content hashes, and
  reason. Exact repeats aggregate into one row. Detail JSON is capped at 16 KiB
  and replaced by an explicit size/hash truncation marker when larger.

`get_recent_activity` is the raw compatibility feed: it returns individual
activity rows exactly as stored, including high-volume lifecycle telemetry such
as Claude Code `SessionEnd` rows tagged `session-boundary`. Operator-facing
consumers should use `get_activity_signal` instead. It keeps substantive rows
visible while compressing repeated lifecycle rows into aggregates keyed by
source, project, summary family, and hour/day time bucket. Raw rows and audit
events remain available for debugging and forensic review.

Activity rows preserve two time concepts:

- `timestamp` is the caller-supplied logical activity date or timestamp. When
  omitted by `log_activity`, it defaults to the operator-local calendar date.
- `created_at` is the UTC insertion timestamp assigned by SQLite.

For activity discovery APIs with `since` (`get_recent_activity`,
`get_activity_signal`, and `get_shipped_events`), a row is visible when either
`timestamp >= since` or `created_at >= since` (with date-only values interpreted
as UTC midnight for `created_at`). This keeps closeouts created just after UTC
midnight discoverable even when their logical activity date is the prior local
day.

For Notion reconciliation, treat each shipped event's `notion_sync` object as the
machine-readable gate:

- `ready`: fetch the explicit `notion_page_id`, update only that row, fetch it
  again, then call `record_disposition(disposition='synced', ...)` with the
  readback proof.
- `meta_no_target`: do not update Notion. Record the event with
  `disposition='synced'`, `downstream_system=policy`, and `downstream_ref`
  pointing to the configured policy file after verifying the policy applies.
- `unmatched`, `no_notion_target`, or `registry_unavailable`: leave the event
  unprocessed and repair the project registry or mapping source first.

Each shipped event also carries a `delivery_state` object. It reports only
receipt-backed bridge facts (`downstream_sync_pending`, `downstream_synced`, or
`policy_dispositioned`); Git, merge, default-branch, deploy, and production
readback dimensions remain `unknown` unless another authority proves them.

A `synced` disposition is source-owned terminal proof: the MCP connection must
be bound to the same principal stored in the activity row's `source`. A
different principal cannot finalize that event, even if it verified a
downstream object. Cross-source verification requires a future explicit
delegation contract; it must not be represented by borrowing the event owner's
caller value. The same ownership rule applies to policy dispositions because
they terminally waive the source's downstream obligation. Cross-source policy
adjudication also requires an explicit delegation contract.

For non-receipt handling, use `record_disposition` with a policy `disposition`
(`unsynced_by_policy` / `no_durable_target` / `superseded_without_receipt` /
`declined_mapping`) and a `reason` when an operator policy says the row should
remain auditable but should not proceed to a downstream receipt. The disposition
appears as `policy_disposition` on `get_shipped_events`; it does not add
`PROCESSED` and does not claim sync. `record_disposition` is SHIPPED-only — the
former `mark_shipped_processed` path for non-shipped operational events
(`TASK_DONE`, `APPROVAL_SENT`, `PLANNING_APPLIED`, `REVIEW_CLOSED`) is retired.

## Startup Sync

Claude.ai may still write its owned sections directly to the bridge markdown file. To keep those edits from being overwritten on the next export, `sync_from_file` imports the four Claude.ai-owned sections (`career`, `speaking`, `research`, `capabilities`) from `BRIDGE_FILE_PATH` into `context_sections` before bridge consumers read from SQLite.

Claude Code's `/start` workflow now runs `mcp__bridge_db__sync_from_file()` before calling bridge read tools, so file edits are pulled into the DB at session start instead of waiting for a later export cycle.

The current operating model is:
- MCP is the primary coordination path.
- `sync_from_file` is the compatibility safety net for Claude.ai-owned file edits.
- `export_bridge_markdown` keeps the fallback markdown artifact in sync for file-based consumers.

## Docs

- [`integration-spec.md`](integration-spec.md) — Claude.ai direct MCP and fallback-file integration
- [`ROADMAP.md`](ROADMAP.md) — Execution roadmap for the next integration phases
- [`docs/EXTERNAL-WRITER-AUDIT.md`](docs/EXTERNAL-WRITER-AUDIT.md) — Direct bridge-db writer audit outside the repo
- [`docs/BRANCH-RETIREMENT.md`](docs/BRANCH-RETIREMENT.md) — Branch cleanup and stale-ref disposition policy

### Internal / maintainer docs

- [`docs/internal/OPERATOR-CHECKLIST.md`](docs/internal/OPERATOR-CHECKLIST.md) — Local verification checklist
- [`docs/internal/POST-SYNC-REVIEW.md`](docs/internal/POST-SYNC-REVIEW.md) — Bridge-sync post-run evidence checklist
- [`docs/internal/PHASE-3-DECISION.md`](docs/internal/PHASE-3-DECISION.md) — Architectural decision: watcher vs startup sync
- [`docs/internal/codex-migration.md`](docs/internal/codex-migration.md) — Per-consumer migration instructions
