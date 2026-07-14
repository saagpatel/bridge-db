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
- Claude.ai may still edit its owned sections directly in the fallback markdown file.
- Claude Code's `/start` skill calls `mcp__bridge_db__sync_from_file()` before
  bridge-db reads, importing the four Claude.ai-owned sections from the file into
  `context_sections`.

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
`pending` after review. Claude Code may continue to use its existing explicit
confirmation path for non-operator handoffs.

Claude Code's `/start` skill already reads `mcp__bridge_db__get_pending_handoffs()` —
it now runs `mcp__bridge_db__sync_from_file()` first, then reads pending handoffs.
The handoff appears immediately in the next CC session.

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

The `update_section` tool enforces ownership — only `caller="claude_ai"` can write
these sections. CC and Codex calls with these section names will receive a ToolError.
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

**With auth active (`BRIDGE_DB_AUTH_MODE` != `off`):** the file is an unauthenticated
channel by design. Changed or new sections are imported as `source_trust='ingested'`
and reported in the `demoted` list of the return value. Sections whose content is
identical to what is already in the DB are skipped and their existing label is
preserved. The operator reviews demoted sections and promotes reviewed ones via
`uv run python -m bridge_db --promote-section <section>` (TTY-gated). Claude Code's
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

| Principal | Write capabilities | Boundaries |
|---|---|---|
| `codex` | `log_activity(caller="codex")`, `save_snapshot(caller="codex")`, `record_cost(caller="codex")`, source-owned `record_disposition(caller="codex")`, and `pick_up_handoff`/`clear_handoff` where their gates allow it | Owns Codex truth and verification; must not write or refresh `cc` snapshots or Claude.ai sections |
| `cc` | `log_activity(caller="cc")`, `save_snapshot(caller="cc")`, `record_cost(caller="cc")`, source-owned `record_disposition(caller="cc")`, and `pick_up_handoff`/`clear_handoff` where their gates allow it | Owns Claude Code state and session telemetry; must not write Codex snapshots or Claude.ai sections |
| `claude_ai` | `update_section` for `career`, `speaking`, `research`, and `capabilities`; channel-bound `create_handoff(caller="claude_ai")`; compatibility file edits to those four sections followed by `sync_from_file` | Advisory and dispatch surface; MCP handoffs cannot mint operator trust and must not act as local execution proof |
| `notion_os` | `log_activity(caller="notion_os")`, `record_cost(caller="notion_os")`, `record_disposition(caller="notion_os")` | Owns Notion-side receipts/activity it actually verified; must not infer project mappings beyond `notion_sync` |
| `personal_ops` | `log_activity(caller="personal_ops")`, `record_cost(caller="personal_ops")`, `record_disposition(caller="personal_ops")` | Owns operator-facing coordination receipts; must not replace repo-local or bridge-db verification |

`record_disposition` is the sole shipped-event write verb above. It is
SHIPPED-only and writes the row's terminal `sync_disposition`; the former
`mark_shipped_processed` non-shipped `PROCESSED`-marking path is retired.

---

## No Daemon Needed

Each MCP client (CC, Codex, Claude Desktop) launches its own `bridge-db` process via
stdio. All processes share the same SQLite file at `~/.local/share/bridge-db/bridge.db`
with WAL mode + `PRAGMA busy_timeout=15000` for concurrent writer waiting. Logical
lost-update protection is handled by context-section CAS and handoff claim guards,
not by WAL alone.

There is no shared bridge-db daemon, no HTTP transport, and no need for a LaunchAgent.
The stdio model is client-managed: the server process lives exactly as long as the
client session that spawned it.
