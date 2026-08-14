# Stage-1 Channel Auth — Rollout Status

> Current per-client state of `BRIDGE_DB_AUTH_MODE`, captured 2026-06-28.
> **Supersedes** any earlier "all five wired at warn, safe to flip enforce" reading.
> The phase *procedure* lives in [OPERATOR-CHECKLIST.md](OPERATOR-CHECKLIST.md) (Stage 1
> Rollout). This file records where the rollout *actually* stands and the gap to enforce.

## TL;DR

**Do NOT flip `BRIDGE_DB_AUTH_MODE=enforce` yet.** Only 3 of 5 clients are wired to
authenticate. Flipping enforce now fail-closes the two unwired daemons' writes and breaks
cross-client `create_handoff`.

## Per-client state

Evidence: `~/.local/share/bridge-db/audit.jsonl`, `auth.bind` events grouped by principal
(`detail=principal=<name>`).

| Client | Wired? | auth.bind | writes (all-time) | Config location | Effective mode |
|---|---|---|---|---|---|
| cc | yes | 338 | 7,468 | `~/.claude.json` MCP `env` | warn |
| codex | yes | 3,400 | 637 | `~/.codex/bin/bridge-db-mcp` wrapper | warn (wrapper-sourced) |
| claude_ai | yes | 15 | 25 | `claude_desktop_config.json` | warn |
| notion_os | **no** | 2 | 163 | none under `~/Projects/Notion/src` | off (legacy) |
| personal_ops | **no** | 0 | 880 | `bridge-db.ts` passthrough; daemon env unset | off (legacy) |

All five principals **are enrolled** (`--list-principals`), so Phase A is complete. Phase B
(plumb token + mode into the spawn env) was completed only for cc, codex, claude_ai.

## Why the audit log looks safe but isn't

`notion_os` (2 binds / 165 writes) and `personal_ops` (0 binds / 880 writes) write as `off`:
they never present a token, so they **cannot** generate `auth.mismatch`. A clean mismatch
log therefore means "three clients authenticate cleanly + two never try" — not "all five
are safe to enforce." A prior readiness pass that counted `0 violations / 13,002 entries`
mis-read this as green.

## Architectural blocker: create_handoff role-vs-channel

`create_handoff` is `claude_ai`-only by role rule (`tools/handoffs.py`, `if caller !=
"claude_ai"`). Channel auth binds the connection to the *spawning* token. So any cc- or
codex-initiated handoff must pass `caller="claude_ai"` to satisfy the role rule, which
mismatches the bound principal: allowed + audited under `warn`, **rejected under
`enforce`**. The CC `project-pipeline` skill
(`~/.claude/skills/project-pipeline/SKILL.md`) hardcodes exactly this call, so it would
break the moment enforce is on. Before enforce, decide the intended semantics:

- **(a)** only a `claude_ai`-bound connection may create handoffs — then cc/codex consumers
  must stop dispatching directly (route through the real claude_ai client), or
- **(b)** relax `create_handoff` so any *authenticated* principal may create a handoff
  destined for claude_ai pickup.

Today the behavior is **(a)** by default.

## Recommendation

Hermes is modeled separately from the five historical write-capable callers.
Its authenticated principal has zero write scopes; enrollment proves client
identity without granting activity, cost, handoff, snapshot, disposition, or
export mutations. Its shown-once token remains an operator-run TTY ceremony.

**Hold at `warn`.** The `source_trust` operator→agent clamp — the actual impersonation
defense — is already active in `warn` (confirmed firing 2026-06-28: `requested=operator
stored=agent`). Enforce's only *added* value is rejecting wrong-caller writes, of which the
log shows zero genuine instances (the 2026-06-28 09:50 codex→claude_ai pair was a
deliberate test, project `__READ_ONLY_MISTAKE_TEST_DO_NOT_USE__`).

## Turnkey Phase B for the two unwired daemons

Operator-run: raw principal tokens are shown once at `--enroll` and only hashes persist, so
agents cannot do this.

1. Re-enroll to capture fresh tokens (the stored hash rotates):
   ```bash
   cd ~/Projects/bridge-db
   uv run python -m bridge_db --enroll personal_ops   # capture token once
   uv run python -m bridge_db --enroll notion_os      # capture token once
   ```
2. **personal_ops** — `bridge-db.ts` already passes `BRIDGE_DB_PRINCIPAL_TOKEN` and
   `BRIDGE_DB_AUTH_MODE` through from the daemon's own env. Set both in the daemon launch
   env (`~/Library/LaunchAgents/com.d.personal-ops.plist` `EnvironmentVariables`, or the
   daemon's env file), then restart the daemon:
   ```
   BRIDGE_DB_PRINCIPAL_TOKEN=<personal_ops token>
   BRIDGE_DB_AUTH_MODE=warn
   ```
3. **notion_os** — its bridge-db spawn carries no auth env. Add both vars wherever it spawns
   bridge-db (locate via `rg -n "bridge_db|bridge-db" ~/Projects/Notion/src`), mirroring the
   personal-ops passthrough pattern.
4. Burn in ≥ a few days at `warn`; watch `audit_tail(tool="auth.bind")` for the two new
   principals and `audit_tail(tool="auth.mismatch")` for stragglers.
5. Resolve the `create_handoff` semantics above so the project-pipeline dispatch path is not
   rejected.
6. Only then flip `enforce` per OPERATOR-CHECKLIST.md step 10, with the wrong-caller
   rejection test.

## Rollback

`BRIDGE_DB_AUTH_MODE=off` on any client restores byte-for-byte legacy behavior. No DB
migration to unwind.
