# Claude.ai Integration Spec

This document describes how Claude.ai interacts with bridge-db — current direct MCP
usage, the file fallback path, and the remaining limitations.

See `ROADMAP.md` for the closed roadmap state, and `docs/internal/OPERATOR-CHECKLIST.md` for the
local verification and registration checklist.

## Current State

Claude.ai has two supported paths:

- **Primary path:** direct bridge-db MCP tools through Claude Desktop.
- **Fallback path:** the markdown file at
  `~/.claude/projects/<encoded-home>/memory/claude_ai_context.md` via the Filesystem MCP
  server.

The direct MCP path has been validated locally for read access and owned write
behavior. The fallback file remains compatibility infrastructure for file-based
clients and for any Claude.ai workflow that has not moved to direct MCP calls.

### What Claude.ai reads

- Its own sections: Career, Speaking, Research, Capabilities
- CC State Snapshot and Codex State Snapshot (read-only)
- Recent CC Activity and Recent Codex Activity
- Pending Handoffs
- Diagnostics and observability through `health`, `status`, `recall_stats`, and
  `audit_tail`

### What Claude.ai writes

- Updates to Career, Speaking, Research, Capabilities through `update_section`
- Handoffs through `create_handoff`
- Compatibility file edits to the same four Claude.ai-owned sections when direct MCP is
  not used

### How it stays in sync

- Direct MCP writes update SQLite first.
- Consumers call `export_bridge_markdown` after DB writes to keep the fallback file
  current.
- Exports delimit each editable section with explicit
  `bridge-db:owned-section` HTML markers, so nested `##` headings remain section
  content instead of being mistaken for document boundaries.
- Legacy pre-marker files are accepted only when their four owned headings and
  known generated document headings parse unambiguously. The first authenticated
  export converts a matching legacy file to the marked format and records its
  whole-file export state.
- Claude.ai may still edit its owned sections directly in the fallback markdown file.
- Claude Code's `/start` skill calls `mcp__bridge_db__sync_from_file()` before
  bridge-db reads, importing the four Claude.ai-owned sections from the file into
  `context_sections`.

`health` and `status` report `projection_health="untracked"` when the
whole-file export state is absent, even if the editable section text matches the
DB. Do not run `sync_from_file` merely to clear that state: first verify that the
DB and legacy file contain the same complete owned sections, then use
`export_bridge_markdown` to establish the tracked projection.

**Current limitation:** fallback file edits are synchronized into the DB on the next
Claude Code startup or explicit `sync_from_file` call, not continuously. That closes
the export-stomp gap, but it is still startup-triggered sync rather than a live
watcher.

---

## Claude.ai Direct MCP Path

### Registration (Claude Desktop)

Register bridge-db in Claude Desktop's MCP config:

```json
{
  "mcpServers": {
    "bridge-db": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "<path-to-bridge-db>",
        "python",
        "-m",
        "bridge_db"
      ],
      "env": {
        "BRIDGE_DB_PRINCIPAL_TOKEN": "<claude_ai-token>",
        "BRIDGE_DB_AUTH_MODE": "warn"
      }
    }
  }
}
```

The `claude_ai` enrollment token (from `--enroll claude_ai`) goes in
`BRIDGE_DB_PRINCIPAL_TOKEN`, and `BRIDGE_DB_AUTH_MODE` sets the rollout dial.
Most write tools retain the staged `off` / `warn` / `enforce` behavior, but
`create_handoff` always requires this exact channel binding, including in `off`
mode, because dispatch crosses a sensitive execution boundary.

This gives Claude.ai access to all 26 MCP tools under `mcp__bridge_db__*`, including
the read-only `health` and `status` diagnostics, the file-import helper `sync_from_file`,
the `recall` FTS5 lexical search (Phase −1 of the semantic memory layer), and the
observability tools `recall_stats`, `audit_tail`, and `get_write_conflicts`.

This exact `uv`-based stdio launch path is the documented local target and has been
validated in the current setup.

The convergence target is the immutable launcher at
`<private-runtime-root>/current/bin/bridge-db-mcp`. Each client must use that
same pointer after controlled activation; a mutable checkout remains supported
for development but `health`/`status` label it `mutable_direct_path`, not a
verified installed generation. Staging, rollback, drain, and no-secret-output
Codex binding are specified in
`docs/internal/EXECUTION-GENERATIONS.md`.

Generation verification is exact-tree and fail-closed: unexpected bytes,
owner/mode drift, interpreter drift, source-copy races, and generation-ID
mismatch are non-green. The external interpreter executable is digest-bound, and
shared broker readiness requires current `BridgeRuntimeDependencyEvidenceV1` for
the declared project runtime dependency closure. That evidence binds installed
distribution files for `aiosqlite`, `mcp`, `pydantic`, `uvicorn`, and their
relevant transitive packages by path, identity metadata, mtime/ctime, and
SHA-256. The collector holds no-follow descriptors open through digesting and
revalidates every configured and resolved path against the still-open complete
file set before accepting evidence. Packages outside the authenticated set, the
standard library, shared libraries, and OS runtime remain outside the claim;
lockfile binding is not a managed environment-convergence claim.

Each direct MCP process and each shared broker publishes a private
owner/principal/generation lease with request timing, lifecycle reason, PID
ancestry, and RSS. Inventory V2 distinguishes live identity-bound processes,
stale lease records, unknown process state, current RSS, and the historical RSS
last persisted in each lease. Health/status expose a separate readiness claim
for that inventory and fail closed for missing, unverified, stale, or unknown
tenancy evidence before reporting top-level green health. Obsolete generations
refuse new requests, finish active requests, and cooperatively cancel their own
server task. Lifecycle apply is exact-target and cannot terminate another
process. Forward activation requires a private
`BridgeMcpTenancyActivationEvidenceV1` bundle, recomputes the policy from its
exact replay rows, and requires the documented client and lifecycle-scenario
coverage for the exact requested generation before any pointer write. Rollback
remains the safety path and does not require new replay evidence. Snapshot
refusal storage stays an additive extension over core SQLite
`user_version=23`, preserving open/read compatibility with exact previous
merged generation `d7272d489873faa5ed84c81734636ffc8cecb095`; rollback loses
the refusal API until roll-forward but does not discard its rows. Activation or
rollback calls the owning recovery lifecycle verification and requires current
verified anchor/seal evidence after any pending-journal recovery. Missing,
stale, invalid, or unsealed evidence prevents a new pointer mutation.

Claude Code `~/.claude.json` and Claude Desktop
`~/Library/Application Support/Claude/claude_desktop_config.json` converge
through `bridge_db.client_rebinding`: only the exact legacy `bridge-db`
command/args are changed, environment values are preserved without output, and
each target gets a private exact-byte backup plus digest-bound restore. Codex
uses the source-owned `config/bridge-db-mcp-immutable` install input, which
parses the two required credential keys plus the optional reviewed
`BRIDGE_DB_TRANSPORT_MODE=direct|shared` key. `direct` is the default and exact
rollback. In `shared` mode, no-argument MCP launches remain stdio to the client
but exec a Python relay to one credential- and complete-launch-contract-bound
broker over an owner-only Unix socket. The wrapper pins broker launches to the
stable launcher path. Broker receipts carry a credential-bound HMAC and the
published socket identity. Each relay renews a short-lived one-use request
capability in process memory, verifies the connected socket identity against
the authenticated receipt, and omits the value from argv, durable header files,
logs, socket names, leases, and receipts. CLI operations such as `--checkpoint`
always bypass the relay and execute directly.
The checkpoint LaunchAgent input invokes that same launcher with `--checkpoint`
through the existing receipt wrapper. Live installation and reconnect/reload
remain separate governed effects.

### vibe-code-handoff (updated workflow)

**Fallback (file-based):**
```
vibe-code-handoff appends to ## Pending Handoffs section of claude_ai_context.md
```

**Primary (DB-backed):**
```python
mcp__bridge_db__create_handoff(
    caller="claude_ai",
    project_name="<project>",
    project_path="~/Projects/<project>",
    roadmap_file="ROADMAP.md",   # optional
    phase="Phase 2",             # optional
)
```

The MCP call stores `agent` trust even if the request asks for `operator`. For a
Codex-bound pickup, the operator reviews and promotes the exact pending row in an
interactive terminal before pickup:

```bash
uv run python -m bridge_db --promote-handoff <handoff-id>
```

The ceremony displays the row identity and digest, then rechecks the complete
reviewed state under a write lock. It refuses a row that changed or left
`pending` after review. Both Claude Code and Codex refuse non-operator pickup;
an MCP client cannot supply its own approval or bypass this promotion step.

Claude Code's `/start` skill already reads `mcp__bridge_db__get_pending_handoffs()` —
it now runs `mcp__bridge_db__sync_from_file()` first, then reads pending handoffs.
The handoff appears immediately in the next CC session.

After operator promotion, `pick_up_handoff` returns the exact `handoff_id` plus a
one-time `completion_capability` and non-secret `claim_session_id` / expiry. The
claiming session keeps the bearer value in context and passes it back to
`clear_handoff`; it must not serialize it into the fallback file, `HANDOFF.md`,
logs, or durable memory. A different same-role session sees the active claim and
expiry but cannot complete it. Missing or wrong capability evidence fails closed.
After expiry, an operator can retire a genuinely orphaned claim only through the
TTY-gated `--recover-orphaned-handoff` ceremony, which records a durable receipt.

### weekly-review (updated workflow)

**Fallback (file-based):**
```
weekly-review reads claude_ai_context.md via Filesystem MCP
```

**Primary (DB-backed):**
```python
mcp__bridge_db__get_all_sections()          # career, speaking, research, capabilities
mcp__bridge_db__get_latest_snapshot("cc")   # CC active projects, lessons, patterns
mcp__bridge_db__get_latest_snapshot("codex") # Codex infrastructure state
mcp__bridge_db__get_recent_activity(limit=20) # raw mixed activity feed
mcp__bridge_db__get_activity_signal(limit=20) # operator feed with lifecycle rows compressed
mcp__bridge_db__get_shipped_events(unprocessed_only=False) # shipped projects
mcp__bridge_db__record_disposition(...) # terminal sync state: 'synced' receipt or policy disposition
mcp__bridge_db__get_cost_history()          # cost trend
```

`save_snapshot` defaults to `retention_policy="preserve_existing"`. The tool
serializes the family-capacity check and insert under SQLite's writer slot. An
under-limit write returns `retention_policy="preserve_existing"` and
`pruned_count=0`; a full family returns
`ok=false`, `reason_code="snapshot.retention_would_prune"`, and
`mutation_performed=false` without inserting, pruning, or garbage-collecting
snapshot FTS rows. The caller must not retry without a separate decision that
explicitly permits `retention_policy="prune_oldest"`.

Snapshot owners should call `get_snapshot_capacity(caller=..., data=...)`
before assembling a write. A preserve-mode capacity refusal returns a durable
`refusal_id`, `acknowledgement_required=true`, and an explicit `next_state`.
The same bound owner must call `acknowledge_snapshot_refusal` with its decision;
foreign principals cannot acknowledge it, and no acknowledgement authorizes
history deletion. Unacknowledged refusals remain visible in `health` and
`status`. The private Codex seed and one-time empty-system markdown migration
use this same admission service, so there is no repository-owned direct writer
that can silently prune snapshot history.

`get_pending_handoffs` is a bounded paged read. It returns at most 100 rows by
default (maximum 200); pass the last returned `id` as `before_id` to fetch the
next page. Creation is rejected atomically when 100 rows are already `pending`
or `active`, or when 10,000 total history rows already exist. A full legacy
history is preserved and rejects creation rather than deleting records, so
consumers must claim or clear existing work while operators make an explicit
retention decision.

Use `get_recent_activity` when raw row-level activity is required. It includes
each stored row, including Claude Code `SessionEnd` lifecycle telemetry tagged
`session-boundary`. Use `get_activity_signal` for startup briefs, dashboards,
morning briefs, cross-provider review intake, and other operator-facing
contexts; it returns substantive rows plus compressed lifecycle aggregates with
counts and first/last timestamps. This is a read-side signal policy only: it
does not delete activity rows, alter audit history, or change shipped/handoff
semantics.

Rows tagged `SHIPPED` or `LEDGER` (case-insensitive) are retention-exempt —
BD-INV-1: retention never deletes a protected row, its receipt, or its
disposition. `get_activity_signal` surfaces protected rows as `kind:"ledger"`
entries alongside the substantive/aggregate rows, and `export_bridge_markdown`
renders a `## Pinned Ledger` section covering protected rows across all
sources.

Activity entries carry both `timestamp` and `created_at`. `timestamp` is the
caller-supplied logical activity date or timestamp; `created_at` is the UTC
insertion timestamp. For `since` filters on activity reads, bridge-db matches
either field, so `since="YYYY-MM-DD"` includes rows inserted on that UTC date
even when the logical activity date is the previous operator-local day.

`record_disposition(caller, activity_id, disposition, ...)` writes a SHIPPED
row's single terminal sync disposition onto the `activity_log` `sync_*` columns
(schema v14). `disposition='synced'` REQUIRES `downstream_system` +
`downstream_ref` and an exact channel-bound caller matching the activity row's
`source`; it records the downstream reference and adds `PROCESSED` — the only
path that claims a durable downstream sync. A policy `disposition`
(`unsynced_by_policy` / `no_durable_target` / `superseded_without_receipt` /
`declined_mapping`) also requires the bound source owner plus a `reason`, does
not add `PROCESSED`, and does not claim sync. Cross-source receipt verification
or policy adjudication requires a future explicit delegation contract and
cannot claim the source caller. A row already carrying `synced` proof cannot be downgraded to a
policy disposition. Dispositioned rows do not re-appear on repeat sync runs:
`get_shipped_events(unprocessed_only=True)` excludes both `PROCESSED` rows and
any row with a `sync_disposition`.

`get_shipped_events` also takes a `limit` param (default 200, max 1000, newest
first) alongside `since` and `unprocessed_only`, so a client passing its own
`limit` is now honored instead of silently ignored.

Each returned shipped event includes `delivery_state`. The state is limited to
bridge-owned receipt facts: `downstream_sync_pending`, `downstream_synced`,
`policy_dispositioned`, or `unknown`. Its dimensions deliberately leave local
completion, commit, push, merge, default-branch, deploy, and production
readback as `unknown`; consumers must not infer those outcomes from a bridge
sync receipt.

`record_disposition` replaces the former `confirm_shipped_sync` /
`record_shipped_event_disposition` / `mark_shipped_processed` trio. It is
SHIPPED-only; the legacy non-shipped `PROCESSED`-marking path is retired. A
SHIPPED row can never be marked resolved without either downstream proof
(`synced`) or an explicit reasoned policy disposition. If
`status.processed_shipped_without_receipt` is nonzero, treat it as historical or
manual drift until proven otherwise.

### update_section (Claude.ai writes)

When Claude.ai edits Career, Speaking, Research, or Capabilities sections, it
should first read the current section and then pass the returned `version` back
as `if_match_version`:

```python
current = mcp__bridge_db__get_section(section_name="career")
mcp__bridge_db__update_section(
    caller="claude_ai",
    section_name="career",
    content="<new content>",
    if_match_version=current["version"],
)
mcp__bridge_db__export_bridge_markdown()  # keep file in sync for Codex fallback
```

The `update_section` tool enforces ownership with an exact channel-bound caller,
independent of the global auth rollout mode. Only `caller="claude_ai"` can write
these sections; `portfolio` is separately owned by `cc`. Every accepted MCP
content version defaults to `source_trust="agent"` and cannot inherit an older
operator label. CC and Codex calls with Claude.ai section names receive a ToolError.
If the section changed since `current` was read, `update_section` returns
`ok=False`, `conflict=True`, and a `receipt_id`; re-read, merge deliberately, and
retry with the new version. A write to an existing section without
`if_match_version` is rejected the same way (`reason_code="missing_cas"`) —
there is no blind-write path for existing sections.

### sync_from_file (startup safety net)

When Claude.ai edits its owned sections through the markdown file instead of MCP tools:

```python
mcp__bridge_db__sync_from_file()
```

This reads `BRIDGE_FILE_PATH`, extracts only the four Claude.ai-owned headings, and
imports them into `context_sections` with `owner="claude_ai"` only when the DB still
matches the version/hash last written by `export_bridge_markdown`. It does not touch
handoffs, snapshots, activity, or any CC/Codex-owned section content.
If the DB changed after the fallback file was exported, the file import is
rejected for that section and recorded in `write_conflicts`; the DB row wins until
an operator or client re-reads and merges.

The file is an unauthenticated channel in every auth mode. Changed or new
sections are imported as `source_trust='ingested'`
and reported in the `demoted` list of the return value. Sections whose content is
identical to what is already in the DB are skipped and their existing label is
preserved. Exported stored-data boundary lines and advisory `source_trust` labels
are reserved projection metadata: the importer strips them from section bodies
and never treats them as authority. The operator reviews demoted sections and promotes reviewed ones via
`uv run python -m bridge_db --promote-section <section>` (TTY-gated). The ceremony
prints the exact content, version, and SHA-256 digest, requires confirmation, then
revalidates that same row under a write lock before promotion. Claude Code's
`/start` skill surfaces `demoted` sections when they are present.

---

## File Watcher Path (Future)

A background file watcher would sync Claude.ai's direct file edits into the DB
without requiring Claude.ai to call MCP tools explicitly. This would eliminate the
lag described in the Current State section.

**Approach:**
1. `notification-hub` (already running) watches `claude_ai_context.md` for changes
2. On change: extract Claude.ai-owned sections from the file
3. Call `bridge_db` internals (or a new `sync_sections_from_file()` helper) to update
   `context_sections` rows
4. No `export_bridge_markdown` needed — the file is already current

**Status:** Not implemented. The `notification-hub` watcher at
`~/Projects/notification-hub/src/notification_hub/watcher.py` handles activity
line parsing but not section sync. This would require a new `SectionSyncHandler`.

**Priority:** Deferred by current architecture decision. `/start` imports file edits before
bridge reads, so a watcher should only be reconsidered if continuous sync becomes a
real coordination need.

---

## Ownership Invariants (All Paths)

Regardless of how Claude.ai accesses bridge-db, these ownership rules hold:

| Section | Writer | Readable by |
|---|---|---|
| career, speaking, research, capabilities | claude_ai only | all |
| portfolio | cc only | all |
| cc_snapshot, cc_activity | cc only | all |
| codex_snapshot, codex_activity | codex only | all |
| pending_handoffs | claude_ai (create), cc/codex (pick up and clear) | all |
| cost_records | cc, codex, notion_os, personal_ops (own system) | all |

The `update_section` tool enforces this at the DB layer — no path bypasses it
for MCP writes. Compatibility file edits are limited to Claude.ai-owned sections
and are imported through `sync_from_file` on Claude Code startup or explicit call;
the live watcher remains deferred.

## Principal Capability Matrix

Auth is currently a canary, not a global enforcement flip. In
`BRIDGE_DB_AUTH_MODE=warn`, a connection-bound principal/caller mismatch is
allowed but audited through `auth.mismatch`; in future `enforce` canaries, the
same mismatch is rejected. Do not move the machine-wide default to `enforce`
until each active client has a current enrolled token, verified spawn env, and a
green read/write smoke check.

All principals may use read-side tools for bridge-owned state:
`health`, `status`, `get_recent_activity`, `get_activity_signal`, `get_shipped_events`,
`get_pending_handoffs`, `get_section`, `get_all_sections`, `get_latest_snapshot`,
`get_cost_history`, `recall`, `recall_stats`, and `audit_tail`.

`health` and `status` also expose `evidence_lifecycle`. JSONL rotation is
lossless and does not imply retention deletion. An `audit_degraded` signal means
the primary audit projection failed and an independent durable failure receipt
exists; generic storage health remains non-green until that evidence is
reconciled under an approved operator policy.
The maintenance-only `bridge_db.evidence_policy` workflow can emit a
content-bound plan, create a verified archive copy, or append an operator
acknowledgement. Acknowledgement is review evidence only: it never authorizes
cleanup, clears degradation, or rewrites historical records.

| Principal | Write capabilities | Boundaries |
|---|---|---|
| `codex` | `log_activity(caller="codex")`, `get_snapshot_capacity` / `save_snapshot` / owner-bound `acknowledge_snapshot_refusal`, `record_cost(caller="codex")`, source-owned `record_disposition(caller="codex")`, `pick_up_handoff`/`clear_handoff` where their gates allow it, and scoped CLI `--seal-recovery-batch` | Owns Codex truth and verification; a recovery seal proves the complete current image but does not authorize writing or refreshing `cc` snapshots or Claude.ai sections |
| `cc` | `log_activity(caller="cc")`, `get_snapshot_capacity` / `save_snapshot` / owner-bound `acknowledge_snapshot_refusal`, `record_cost(caller="cc")`, source-owned `record_disposition(caller="cc")`, `pick_up_handoff`/`clear_handoff` where their gates allow it, and scoped CLI `--seal-recovery-batch` | Owns Claude Code state and session telemetry; a recovery seal proves the complete current image but does not authorize writing Codex snapshots or Claude.ai sections |
| `claude_ai` | `update_section` for `career`, `speaking`, `research`, and `capabilities`; channel-bound `create_handoff(caller="claude_ai")`; compatibility file edits to those four sections followed by `sync_from_file` | Advisory and dispatch surface; MCP handoffs cannot mint operator trust and must not act as local execution proof |
| `notion_os` | `log_activity(caller="notion_os")`, `record_cost(caller="notion_os")`, `record_disposition(caller="notion_os")` | Owns Notion-side receipts/activity it actually verified; must not infer project mappings beyond `notion_sync` |
| `personal_ops` | `log_activity(caller="personal_ops")`, `record_cost(caller="personal_ops")`, `record_disposition(caller="personal_ops")` | Owns operator-facing coordination receipts; must not replace repo-local or bridge-db verification |

`record_disposition` is the sole shipped-event write verb above. It is
SHIPPED-only and writes the row's terminal `sync_disposition`; the former
`mark_shipped_processed` non-shipped `PROCESSED`-marking path is retired.

## Recovery batch lifecycle

After an authorized multi-write workflow commits its final BridgeDB mutation,
its local CC or Codex executor may terminate the batch with:

```console
python -m bridge_db --seal-recovery-batch <batch-id>
```

This is a CLI lifecycle operation, not an MCP content-write tool. The runtime
derives the seal owner from a current `BRIDGE_DB_PRINCIPAL_TOKEN` grant carrying
the `seal_recovery_batch` scope. No caller argument can impersonate the owner,
and auth `off` does not bypass the scope. Existing v2 grants must be separately
re-enrolled before activation; the repository does not rewrite live principal
state.

The sealer records one immutable attempt, serializes concurrent calls, and
publishes at most one terminal `RecoverySealReceiptV1` per batch. Distinct
retained batch IDs are capped at 1024; exact-ID replay remains available at
capacity, while a new distinct ID fails closed before creating more evidence.
Success is published while SQLite's writer slot is still held and requires
anchor digest, integrity, semantic readback, and live-source fingerprint
agreement. Failures return or preserve `recovery_unsealed`; stale sealed
receipts are preserved as history but not replayed as current success, and an
interrupted attempt is visible but is never repaired by a read.

`health.evidence_lifecycle.recovery_lifecycle_ready` is the strict batch proof.
It requires the latest success receipt to match the exact current anchor and
live source. The narrower `current_recovery_ready` remains the physical
anchor/source-current result for compatibility. See
`docs/internal/RECOVERY-SEAL-PROTOCOL.md` for the complete state machine.

The existing checkpoint LaunchAgent only truncates WAL. It does not own a write
batch, rotate an anchor, or emit a recovery seal receipt. Health and status
validate existing recovery-seal evidence but never create, prune, or repair the
seal directory.

---

## Direct and shared local runtimes

The client-facing contract is always stdio. `BRIDGE_DB_TRANSPORT_MODE=direct`
keeps one full `bridge-db` process per client. The opt-in `shared` mode keeps a
small stdio relay per client and multiplexes requests through Streamable HTTP
over a private Unix domain socket to one broker per credential and complete
launch contract. The grouping contract binds the database, normalized auth
mode, immutable generation/runtime source, principal registry, projection and
audit/evidence paths, tenancy owner/root, logging, and idle policy. No TCP
listener, LaunchAgent, secret in argv, or cross-principal/config broker is
introduced.

Relay references are private PID/start-identity leases. Every HTTP request also
requires that relay's unique capability, minted in process memory and stored
only as a lease hash. Authorization fails unless the current PID/start identity
still matches the lease. The relay opens the Unix socket itself and verifies the
connected path identity against the credential-authenticated broker receipt
before sending the request, so a later pathname replacement cannot supply the
response. The broker preserves stale reference history, serializes tool calls
across session DB connections, and cooperatively exits 300 seconds after the
last live relay disappears. It never signals another process. Broker startup
fails closed after 10 seconds; setting the transport mode back to `direct` is
the exact rollback and affects only future client spawns. Existing transports
are never disconnected by this path. `BridgeSharedRuntimeInventoryV1` is
available from health/status and `--shared-runtime-status` for aggregate
no-secret readback. `BridgeSharedRuntimeReadinessV1` separately gates
health/status on the exact current launch group, broker PID/start identity,
credential-authenticated receipt, and connected socket identity, so unrelated
group activity cannot make shared mode green.

Both modes use the same SQLite file at `~/.local/share/bridge-db/bridge.db` with
WAL mode + `PRAGMA busy_timeout=15000`. Logical lost-update protection remains
the responsibility of context-section CAS and handoff claim guards, not WAL
alone.
