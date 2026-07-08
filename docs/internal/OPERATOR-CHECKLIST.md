> **Internal — maintainer use only.** This file documents implementation history and operating procedures for the bridge-db maintainer. It is not required to use or integrate bridge-db.

# bridge-db Operator Checklist

Use this checklist when verifying that bridge-db is ready for local use and that the documented Claude.ai registration path matches the current environment.

## Local Verification

First confirm the checkout is current with GitHub:

```bash
git fetch --prune origin
git status --short --branch
git rev-parse HEAD origin/main
```

Expected result
- Local `main` is not behind `origin/main`.
- `HEAD` and `origin/main` match before declaring repo state current.
- Merged local or remote work branches are treated as cleanup candidates.
- Branch cleanup follows [docs/BRANCH-RETIREMENT.md](docs/BRANCH-RETIREMENT.md):
  delete only ancestor-merged, patch-equivalent, or explicitly abandoned refs;
  archive or preserve non-equivalent branches until a disposition is recorded.
  Local `archive/*` refs may be deleted after that abandonment disposition is
  recorded.

Then run these commands from the repo root:

```bash
uv run pytest
uv run pyright
uv run ruff check
uv run python -m bridge_db --doctor
uv run python -m bridge_db --status
uv run python -m bridge_db --dogfood
```

Expected result
- Tests pass.
- Type checking passes.
- Lint passes.
- Doctor reports the DB file, schema, bridge file, and audit log as healthy.
- Status prints a compact operator-facing summary of bridge health, counts, and latest signals.
- Status prints an `Attention:` line when bridge health is degraded or queue/receipt/FTS signals need follow-up.
- Status prints `Freshness: <overall>` from the native `status.freshness`
  block, plus `Next actions: <action> (<owner>), ...` when owner-specific
  follow-up exists. Freshness attention alone is not bridge-health degradation
  and does not fail `--status`.
- The `status.freshness` block includes thresholds, `cc`/`codex` snapshot
  freshness, activity-source freshness for `cc`, `codex`, `claude_ai`,
  `notion_os`, and `personal_ops`, handoff staleness counts, shipped-event
  next action, overall freshness, and up to five deterministic next actions.
- Treat `quiet` activity sources as low-traffic signals, not failures. Treat
  `stale` snapshots/handoffs as refresh or review work, and keep snapshot
  refresh ownership exact: `cc_refresh_snapshot` belongs to `cc`;
  `codex_refresh_snapshot` belongs to `codex`.
- Dogfood prints the read-only post-sync observability summary: status signals, FTS index state, WAL state, recall usage, and shipped-sync audit details.
- The `health` MCP tool should report `ok=True` only when the DB, schema, fallback bridge file, and FTS recall index are all healthy.
- If `fts_missing` or `fts_orphaned` is nonzero, run `uv run python -m bridge_db --rebuild-content-index`, then rerun `--status` and `--dogfood`.

## Claude.ai Registration Check

bridge-db is documented for Claude Desktop registration through `claude_desktop_config.json`.

Checklist
1. Confirm the Claude Desktop config file exists.
2. Confirm the config contains an `mcpServers` block.
3. Confirm there is a `bridge-db` entry with:
   - `command = "uv"`
   - `args = ["run", "--directory", "<path-to-bridge-db>", "python", "-m", "bridge_db"]`
4. Restart Claude Desktop after editing the config.
5. Verify Claude.ai can see bridge-db tools.
6. Run one read-only tool first, preferably `health`.

## Current Verified Local State

Routine verifier refreshed on 2026-06-06. Claude Desktop registration details
below remain from the earlier integration verification.

- `uv run pytest` passes locally. Use fresh output for the current test count;
  do not copy old counts from historical docs.
- `uv run pyright` passes locally.
- `uv run ruff check` passes locally.
- `uv run python -m bridge_db --doctor` passes locally.
- `uv run python -m bridge_db --status` reports bridge health separately from
  freshness attention. Use fresh output for `Freshness:` and `Next actions:`;
  do not treat freshness `attention` as degraded DB/schema/file/FTS health.
- `uv run python -m bridge_db --dogfood` reports shipped-event, handoff, FTS,
  WAL, recall, and audit details. Use fresh output before claiming the shipped
  queue is clean.
- Claude Code SessionEnd logging uses `uv run --directory ~/Projects/bridge-db python -m bridge_db --log-session-boundary <project>` rather than direct SQLite writes.
- `claude mcp list` reports `bridge-db` connected through this repo.
- Codex config includes the `mcp_servers.bridge-db` registration for this repo.
- Claude Desktop config exists at `~/Library/Application Support/Claude/claude_desktop_config.json`.
- That config now contains an `mcpServers.bridge-db` registration pointing at this repo.
- The config file parses as valid JSON after the change.
- Claude.ai read access is confirmed with a successful `mcp__bridge_db__health()` call after restart.
- Claude.ai direct write behavior has been proven through bridge-db MCP tools.
- Startup sync plus export has been verified end to end.
- Latest local repo verification should be stated from fresh verifier output:
  `uv run pytest`, `uv run pyright`, and `uv run ruff check`.
- Live shipped-event state is runtime data. Check fresh `--status`,
  `--dogfood`, or `get_shipped_events` output before claiming pending handoffs,
  actionable unprocessed shipped events, or disposition coverage are clean.
- Dependency drift was refreshed through PR #27 after Dependabot PRs #25 and #26 were superseded.
- Bridge Sync burn-in review is complete and the one-time heartbeat is retired.
- `POST-SYNC-REVIEW.md` is the checklist to use after future scheduled Bridge
  Syncs or shipped-event reconciliation.

Conclusion
- The documented registration path is still the correct target path.
- The local environment is now configured for Claude Desktop to launch `bridge-db`.
- The main cleanup, hardening, and burn-in review cycle is complete.
- The current operating model is: direct MCP first, startup sync for Claude.ai-owned file fallback, and exported markdown as compatibility infrastructure.

## Recommended First Claude.ai Smoke Test

After registration, test in this order:

1. `mcp__bridge_db__health()` — includes `wal_size_bytes` and `wal_warning`  ✅ verified on 2026-04-15
2. `mcp__bridge_db__get_pending_handoffs()`
3. `mcp__bridge_db__get_all_sections()`
4. `mcp__bridge_db__recall_stats(days=7)` — read-side observability over recall log
5. `mcp__bridge_db__audit_tail(limit=5)` — read-side observability over audit log

If those work, move to one owned write path:

1. `mcp__bridge_db__create_handoff(...)`  ← recommended next proof step
2. `mcp__bridge_db__update_section(...)`
3. `mcp__bridge_db__export_bridge_markdown()`

## Startup Sync Verification

To confirm the Claude.ai file-write safety net is active:

1. Edit one of the Claude.ai-owned sections in `claude_ai_context.md`
2. Start a fresh Claude Code session so `/start` runs
3. Confirm `mcp__bridge_db__sync_from_file()` ran before bridge reads
4. Run `mcp__bridge_db__export_bridge_markdown()`
5. Confirm the Claude.ai edit is still present after export

Status
- Verified in this cleanup cycle.

## Steady-State Observability Habit

Use these read-only checks when bridge-db is healthy but you want to confirm the
coordination layer is still earning its keep:

Fast path:

```bash
uv run python -m bridge_db --dogfood
```

Manual equivalent:

1. After Bridge Syncs or shipped-event reconciliation, run
   `mcp__bridge_db__audit_tail(limit=10)` and confirm new shipped events use
   `confirm_shipped_sync` with downstream proof instead of bare
   `mark_shipped_processed`.
2. If shipped-event drift is suspected, run
   `mcp__bridge_db__audit_tail(tool="confirm_shipped_sync", limit=10)` and
   `uv run python -m bridge_db --status`.
   Read `status.freshness.shipped_events.next_action`; actionable unprocessed
   shipped rows should route to `confirm_shipped_sync` with downstream proof or
   `record_shipped_event_disposition` with an explicit policy reason.
3. If a compatibility `mark_shipped_processed` row appears, inspect its
   `detail` field for the requested `activity_ids`, `updated_ids`, and
   `missing_ids`, then confirm `status` still reports
   `processed_shipped_without_receipt=0`.
4. Use `mcp__bridge_db__recall_stats(days=7)` only to evaluate recall over
   bridge-owned state: sections, activity, snapshots, and handoffs.
5. Confirm `uv run python -m bridge_db --status` reports `fts_missing=0` and
   `fts_orphaned=0` before trusting recall results after unusual writes.
6. Read the compact freshness lines from `--status`. `Freshness: fresh` means
   the freshness surfaces have no current advisory action; `attention`,
   `stale`, or `unknown` should be handled through the listed owner-specific
   next actions, not by assuming bridge health is degraded.
7. After a Claude Code session ends, confirm the newest `CC session ended` row is
   indexed:

   ```bash
   sqlite3 ~/.local/share/bridge-db/bridge.db \
     "PRAGMA query_only=ON; SELECT a.id, a.timestamp, a.project_name, CASE WHEN ci.source_id IS NULL THEN 0 ELSE 1 END AS indexed FROM activity_log a LEFT JOIN content_index ci ON ci.source_type='activity' AND ci.source_id=CAST(a.id AS TEXT) WHERE a.source='cc' AND a.summary LIKE 'CC session ended%' ORDER BY a.id DESC LIMIT 3;"
   ```

   Expected result: the newest row has `indexed=1`, and `--status` still reports
   `fts_missing=0`.
7. Do not treat recall misses for repo docs, Notion pages, memory files, or
   planning artifacts as bridge-db bugs. Those sources are intentionally outside
   the DB; bridge-db is a state bridge, not a general knowledge store.

## Retention And Receipts

Activity retention and shipped-sync receipts answer different questions:

- `activity_log` keeps the latest 50 rows per source for ordinary activity
  writes. This is a convenience retention policy for recent context, not a
  proof ledger.
- `activity_log.timestamp` is the caller's logical activity date or timestamp,
  while `activity_log.created_at` is the UTC insertion timestamp. Activity
  `since` filters match either field so closeouts inserted just after UTC
  midnight remain discoverable even if the logical timestamp is the prior local
  day.
- `system_snapshots` keeps the latest 10 rows per system family; Codex
  operating snapshots and consulted-node snapshots are retained independently.
- `shipped_sync_receipts` is the downstream proof ledger for shipped events that
  were confirmed through `confirm_shipped_sync`.
- Claude Code SessionEnd logging uses `--log-session-boundary`; that path adds
  an FTS row and intentionally does not run activity retention pruning.
- If activity counts move, verify health through `--status`, `--dogfood`,
  `processed_shipped_without_receipt=0`, and `fts_missing=0` before assuming
  shipped-sync proof is broken.

## Post-Sync Review

Use [POST-SYNC-REVIEW.md](POST-SYNC-REVIEW.md) after scheduled Bridge Syncs,
manual shipped-event reconciliation, or a bridge-sync burn-in heartbeat.

Minimum clean proof:

1. `uv run python -m bridge_db --status` reports healthy state.
2. `uv run python -m bridge_db --dogfood` reports no pending handoffs, no
   actionable unprocessed shipped events, no receiptless processed shipped
   events, and no FTS drift or WAL warning.
3. `mcp__bridge_db__export_bridge_markdown()` returns `ok=true` and a nonzero
   byte count.
4. A follow-up status check shows the markdown bridge file is fresh.
5. Scheduled automation output is classified from runtime/session evidence, not
   from config or inventory alone.

If a scheduled bridge-sync session lacks a clean final report, keep that fact
visible while grounding the project verdict in live bridge-db status, health,
dogfood, shipped-event, and export proof.

## Failure Clues

- If Claude.ai cannot see the tools, check whether the `mcpServers` block was added to the correct config file.
- Restart Claude Desktop after config changes before assuming registration failed.
- If the server launches but tools fail, run `uv run python -m bridge_db --doctor` locally again.
- If `health()` reports `ok: false`, check whether the bridge markdown file is missing or stale before assuming the DB is broken.
- If `health()` reports FTS drift, run `uv run python -m bridge_db --rebuild-content-index` rather than editing `content_index` manually.
- If writes succeed but the markdown file looks stale, run `export_bridge_markdown` and re-check the bridge file timestamp.

## Stage 1 Rollout — Channel Auth

**Phase A — Enroll (one TTY session):**
1. `uv run python -m bridge_db --enroll cc` / `--enroll codex` / `--enroll claude_ai` / `--enroll notion_os` / `--enroll personal_ops` — capture each token once.

**Phase B — Wire envs (each client spawns its own server process):**
2. **CC:** `claude mcp remove bridge-db -s user`, then re-add with `--env BRIDGE_DB_PRINCIPAL_TOKEN=<cc> --env BRIDGE_DB_AUTH_MODE=warn` (full command in CLAUDE.md Registration).
3. **Claude Desktop (claude_ai):** add `"env": {"BRIDGE_DB_PRINCIPAL_TOKEN": "<claude_ai>", "BRIDGE_DB_AUTH_MODE": "warn"}` to the bridge-db entry in `claude_desktop_config.json`.
4. **Codex:** add the same two vars to the bridge-db server's `env` table in `~/.codex/config.toml`.
5. **personal-ops:** set both vars in the spawn env in `~/.local/share/personal-ops/app/src/bridge-db.ts` (it spawns bridge-db as an MCP subprocess).
6. **notion-os:** locate its bridge-db spawn (`rg -n "bridge_db|bridge-db" ~/Projects/Notion/src`) and set both vars there.
7. Add `~/.local/share/bridge-db/principals.json` to the harness sensitive-path guard list (`mcp-gate-policy.json`) — both CC and Codex hooks read it live.

**Phase C — Warn burn-in (≥1 week):**
8. Watch `audit_tail(tool="auth.mismatch")` and `audit_tail(tool="auth.trust_clamped")` every few days. Expected findings: any consumer skill or prompt still passing a wrong `caller` or `source_trust='operator'`. Fix consumers (grep `~/.claude/skills` and Claude.ai project prompts for `create_handoff`/`update_section` call sites — known lesson: MCP param changes don't auto-propagate to skills).
9. Watch for `auth.bind` failures (token present but not enrolled = a client wired with a stale/wrong token).

**Phase D — Enforce:**
10. Flip `BRIDGE_DB_AUTH_MODE=enforce` in all five client configs. Verify each client can still write (one `log_activity` per system) and that a deliberately wrong-caller call is rejected.

**Phase E — Legacy label cleanup (one-time):**
11. The 20 pre-existing `operator`-labeled pending handoffs were labeled before minting was gated. Review and relabel the unconsumed ones so the pickup gate actually gates:
    `sqlite3 ~/.local/share/bridge-db/bridge.db "UPDATE pending_handoffs SET source_trust='agent' WHERE status='pending' AND source_trust='operator';"`
    (Run manually after eyeballing `get_pending_handoffs` — any handoff you genuinely dictated can stay `operator`.)
12. Re-run `uv run python -m bridge_db --status` and confirm the pending-handoff trust breakdown reflects the relabel.

**Rollback at any point:** set `BRIDGE_DB_AUTH_MODE=off` in the affected client(s) — restores byte-for-byte legacy behavior including sync label preservation. No DB migration to unwind.

**Threat-model note:** the TTY gate on enrollment/promotion ceremonies is a
speed bump for non-interactive agent processes, not a security boundary — any
process that can allocate a PTY can pass it. The real Stage-1 win is that
impersonation now requires an explicit, auditable act (stealing another
client's token) instead of a one-string parameter. OS-level isolation is a
later-stage decision.
