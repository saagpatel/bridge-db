# bridge-db Operator Checklist

Use this checklist when verifying that bridge-db is ready for local use and that the documented Claude.ai registration path matches the current environment.

## Local Verification

Run these commands from the repo root:

```bash
uv run pytest
uv run pyright
uv run ruff check
uv run python -m bridge_db --doctor
uv run python -m bridge_db --status
```

Expected result
- Tests pass.
- Type checking passes.
- Lint passes.
- Doctor reports the DB file, schema, bridge file, and audit log as healthy.
- Status prints a compact operator-facing summary of bridge health, counts, and latest signals.
- The `health` MCP tool should report `ok=True` only when the DB, schema, and fallback bridge file are all present.

## Claude.ai Registration Check

bridge-db is documented for Claude Desktop registration through `claude_desktop_config.json`.

Checklist
1. Confirm the Claude Desktop config file exists.
2. Confirm the config contains an `mcpServers` block.
3. Confirm there is a `bridge-db` entry with:
   - `command = "uv"`
   - `args = ["run", "--directory", "/Users/d/Projects/bridge-db", "python", "-m", "bridge_db"]`
4. Restart Claude Desktop after editing the config.
5. Verify Claude.ai can see bridge-db tools.
6. Run one read-only tool first, preferably `health`.

## Current Verified Local State

Verified on 2026-04-15.

- `uv run python -m bridge_db --doctor` passes locally.
- Claude Desktop config exists at `/Users/d/Library/Application Support/Claude/claude_desktop_config.json`.
- That config now contains an `mcpServers.bridge-db` registration pointing at this repo.
- The config file parses as valid JSON after the change.
- Claude.ai read access is confirmed with a successful `mcp__bridge_db__health()` call after restart.
- Claude.ai direct write behavior has been proven through bridge-db MCP tools.
- Startup sync plus export has been verified end to end.
- Latest local repo verification is green: `136` tests, `ruff` clean, `pyright` clean.

Conclusion
- The documented registration path is still the correct target path.
- The local environment is now configured for Claude Desktop to launch `bridge-db`.
- The main cleanup and hardening cycle is complete.
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

## Failure Clues

- If Claude.ai cannot see the tools, check whether the `mcpServers` block was added to the correct config file.
- Restart Claude Desktop after config changes before assuming registration failed.
- If the server launches but tools fail, run `uv run python -m bridge_db --doctor` locally again.
- If `health()` reports `ok: false`, check whether the bridge markdown file is missing or stale before assuming the DB is broken.
- If writes succeed but the markdown file looks stale, run `export_bridge_markdown` and re-check the bridge file timestamp.