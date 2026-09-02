# External Writer Audit

Last checked: 2026-05-30

## Purpose

This note records the current audit of code outside bridge-db that can read from
or write to the live bridge database. The goal is to prevent future recall drift
from direct SQLite writes that bypass bridge-db's FTS-safe MCP or CLI paths.

## Current Finding

No remaining executable direct writer to bridge-db's `activity_log` was found
outside bridge-db itself.

The fixed bypass was Claude Code's SessionEnd hook:

- Old behavior: direct `sqlite3` insert into `activity_log`.
- Required behavior: the owner-private SessionEnd client invokes the stable immutable launcher with the live `cc` binding and `--log-session-boundary <project>`; the CLI revalidates that principal before writing.
- Expected proof: newest `CC session ended` rows have matching `content_index`
  rows and `uv run python -m bridge_db --status` reports `fts_missing=0`.

## Audited Surfaces

- `~/.claude/hooks`
- `~/.claude/skills`
- `~/.codex/automations`
- `~/Projects`
- `~/.local/share/personal-ops`

## Notable Non-Issues

- `~/.claude/hooks/bridge-db-recall-warmup.sh` reads
  `activity_log` with `sqlite3` for SessionStart context. It does not write.
- `~/Projects/Notion/src/notion/bridge-db-sync.ts` reads bridge-db state
  and confirms shipped sync through the bridge-db MCP client. It is not an ad
  hoc SQLite writer.
- `~/.local/share/personal-ops/app/src/bridge-db.ts` uses bridge-db as
  an MCP subprocess. It is not a direct DB writer.
- `notification-hub` has its own unrelated `content_index` table; that is not
  bridge-db's FTS table.
- Direct SQL in bridge-db tests, migrations, seed code, and internal helpers is
  expected. Tests that intentionally insert unindexed rows are drift regression
  coverage.

## Audit Commands

```bash
rg -n "INSERT INTO activity_log|INSERT INTO content_index|DELETE FROM activity_log|UPDATE activity_log" \
  ~/.claude/hooks ~/.claude/skills ~/Projects ~/.codex/automations \
  -S -g '*.sh' -g '*.py' -g '*.js' -g '*.mjs' -g '*.ts' -g '*.tsx' -g '*.md' \
  -g '!**/.venv/**' -g '!**/node_modules/**' -g '!**/.git/**' -g '!**/backups/**' \
  -g '!**/.codex/worktrees/**'

rg -n "bridge.db|BRIDGE_DB_DEFAULT_PATH|sqlite3" \
  ~/.claude/hooks ~/Projects/Notion/src ~/.local/share/personal-ops \
  -S -g '*.sh' -g '*.py' -g '*.js' -g '*.mjs' -g '*.ts' \
  -g '!**/.venv/**' -g '!**/node_modules/**' -g '!**/.git/**'
```

## Maintenance Rule

Any future external bridge-db writer should use one of these paths:

- MCP tools exposed by bridge-db.
- A bridge-db CLI command that shares the same internal write helpers.

Do not add direct `sqlite3` writes to `activity_log`, `content_index`,
`context_sections`, `system_snapshots`, or `pending_handoffs` outside bridge-db.
