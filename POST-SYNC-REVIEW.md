# Post-Sync Review Checklist

Use this checklist after a scheduled Bridge Syncs run or any manual shipped-event
reconciliation. The goal is to prove bridge-db state, export freshness, and
scheduled-output evidence without expanding bridge-db beyond its state-bridge
scope.

## When To Run

- After the weekly `bridge-sync` automation window.
- After a one-shot bridge-sync or shipped-event reconciliation.
- Before retiring a burn-in or post-run heartbeat that exists only to validate
  bridge-sync behavior.

## Bridge-Db Proof

Run from this repo:

```bash
git fetch --prune origin
git status --short --branch
uv run python -m bridge_db --status
uv run python -m bridge_db --dogfood
```

Expected clean signals:

- local `main` is current with `origin/main`
- `Overall: healthy`
- no `Attention:` line in `--status`
- `pending_handoffs=0`
- `unprocessed_shipped=0`
- `processed_shipped_without_receipt=0`
- `fts_missing=0`
- `fts_orphaned=0`
- `wal_warning=False`
- latest shipped-sync audit rows use `confirm_shipped_sync` with downstream
  proof, not only `mark_shipped_processed`
- each unprocessed shipped event has an inspected `notion_sync` state from
  `get_shipped_events(unprocessed_only=True)`. Only `ready` events may proceed
  to Notion update/readback/`confirm_shipped_sync`; `meta_no_target` events may
  be confirmed with `downstream_system=policy` and the configured policy file as
  `downstream_ref`; `unmatched`, `no_notion_target`, and `registry_unavailable`
  are valid pending states that require registry repair instead of fuzzy Notion
  search.
- if a Claude Code session ended since the last review, the newest
  `CC session ended` row has a matching `content_index` row

If FTS drift appears, run:

```bash
uv run python -m bridge_db --rebuild-content-index
uv run python -m bridge_db --status
uv run python -m bridge_db --dogfood
```

Do not repair `content_index` through ad hoc SQL.

For a quick SessionEnd hook spot-check, run:

```bash
sqlite3 ~/.local/share/bridge-db/bridge.db \
  "PRAGMA query_only=ON; SELECT a.id, a.timestamp, a.project_name, CASE WHEN ci.source_id IS NULL THEN 0 ELSE 1 END AS indexed FROM activity_log a LEFT JOIN content_index ci ON ci.source_type='activity' AND ci.source_id=CAST(a.id AS TEXT) WHERE a.source='cc' AND a.summary LIKE 'CC session ended%' ORDER BY a.id DESC LIMIT 3;"
```

Expected result: newest rows show `indexed=1`. If not, run the rebuild command
above and audit the SessionEnd hook path before closing the review.

Then refresh the compatibility mirror through MCP:

```text
mcp__bridge_db__export_bridge_markdown()
```

Expected result:

- `ok=true`
- returned path is the configured bridge markdown path
- returned `bytes` is nonzero
- a follow-up `status` call shows the bridge file is fresh

## Scheduled-Run Proof

Do not call the scheduled run successful from config or inventory evidence
alone. Confirm runtime/output evidence first:

1. The `bridge-sync` runtime row has a new `last_run_at` for the expected
   window.
2. The latest bridge-sync session or output has a clean final report.
3. If the session lacks a clean final report, say that plainly and ground the
   project verdict in live bridge-db `status`, `health`, `get_shipped_events`,
   `--dogfood`, and export evidence.
4. The burn-in heartbeat can be retired only after the scheduled-run review is
   complete and clean.

## Automation Scorecard Proof

Run the all-active output locator as supporting evidence:

```bash
node /Users/d/.codex/scripts/ops/find_latest_automation_outputs.mjs --read-only --all-active
```

Use the locator as evidence support, not as primary proof that an automation
produced useful output. Update the active automation usefulness scorecard only
from real scheduled-output evidence, and keep pre-run or manual checks labeled
as such.

## Dependency Drift Triage

Run:

```bash
uv tree --outdated
```

Classify results before upgrading:

- safe patch refresh: small patch updates with no schema or MCP contract impact
- dedicated PR: runtime or transitive updates that affect MCP transport,
  validation, crypto, or server behavior
- defer: any update that would distract from proving bridge-db/export health

As of the 2026-05-30 maintenance pass, the latest dependency drift was handled
in PR #27. Treat future runtime or MCP-adjacent updates as a dedicated
dependency pass, not as part of shipped-event reconciliation.

## Closeout

Before saying the lane is done, run the routine verifier:

```bash
uv run pytest
uv run pyright
uv run ruff check
uv run python -m bridge_db --doctor
uv run python -m bridge_db --status
uv run python -m bridge_db --dogfood
```

Close the review only when repo checks are green, bridge-db proof is fresh, and
scheduled-output evidence has been classified honestly. A clean dogfood pass must
include no shipped-event drift, no receipt gaps, no FTS index drift, and no WAL warning.
