# bridge-db — Portfolio Disposition

**Status:** Release Frozen (operator-tool, MCP-server-distributed) —
**Python SQLite-backed MCP server** with 23 tools for shared state
coordination across **Claude.ai + Claude Code + Codex** on
`origin/main`. Operator-declared "**steady maintenance**" (per
README: "Project is in steady maintenance. Scope is pinned to
cross-system *state* coordination plus lexical `recall` plus
observability; it is not a knowledge store"). **Fifth
operator-tool / dogfood cluster member**; introduces new
sub-shape: **MCP-server-distributed**. 146 tests passing.
ruff + pyright clean. Phases 0-6 all shipped.

> Disposition uses strict `origin/main` verification.
> **Operator-tool cluster reaches 5 with 5 distinct sub-shapes.**
> **bridge-db is the operator's core AI-system coordination
> infrastructure** — load-bearing for Claude.ai/CC/Codex
> three-way state sharing.

---

## Verification posture

Only `origin` (`saagpatel/bridge-db`). Clean migration state.

`origin/main`:

- Tip: `91c38a0` Merge pull request #22 from saagpatel/codex/bridge-db-operator-checklist-refresh
- Recent maintenance cadence:
  - `91c38a0` Merge: operator checklist refresh
  - `f8717cc` docs: refresh bridge-db operator checklist
  - `78095b2` Merge: maintenance state refresh
  - `b615615` docs: refresh bridge-db maintenance state
  - `0ee0ecf` chore(deps): refresh bridge-db lockfile
  - `766b5cd` docs: add post-sync review checklist
- **No `pyproject.toml` for PyPI publishing** declared — local
  `uv tool install` or `uv sync` workflow
- Repo tree includes:
  - `src/` (Python source for 23 MCP tools)
  - `tests/` (146 tests)
  - `OPERATOR-CHECKLIST.md` + `POST-SYNC-REVIEW.md` + `ROADMAP.md`
    + `PHASE-3-DECISION.md` (operator-authored governance)
  - `integration-spec.md` + `codex-migration.md`
  - `bridge-db-semantic-memory-IMPLEMENTATION-PLAN-v2{,.1}.md`
    (closed semantic memory arc)
  - `eval-set-handoff-package.md`
  - `semantic_quality_set.json`
  - `uv.lock` (uv-managed)
- Default branch: `main`

---

## Current state in one paragraph

bridge-db is a **Python SQLite-backed MCP server** that provides
**23 MCP tools** for shared state coordination across the
operator's three primary AI systems: **Claude.ai** (via Claude
Desktop's direct MCP support), **Claude Code** (via CC skills +
MCP stdio), and **Codex** (via MCP stdio). State lives in
`~/.local/share/bridge-db/bridge.db` (SQLite WAL mode +
`PRAGMA busy_timeout=5000` for safe concurrent writes). The 23
tools cover: activity logging + recent activity queries +
shipped-event sync with downstream proof receipts + state
sections (career / speaking / research / capabilities) + system
snapshots + handoffs + cost tracking + **FTS5 lexical `recall`**
+ observability (`audit_tail`, `recall_stats`, `health`, `status`)
+ markdown export for file-based clients. Phases 0-6 all
shipped (Phase 6 observability shipped 2026-04-17). 146 tests
passing. ruff + pyright clean. **Operator-declared steady
maintenance** with scope pinned to cross-system state coordination
+ lexical recall + observability.

For full detail see `README.md` + `ROADMAP.md` + `OPERATOR-CHECKLIST.md`
on `origin/main`.

---

## Why "Release Frozen (operator-tool, MCP-server)" — fifth cluster member, new sub-shape

The operator-tool / dogfood cluster gains a fifth member with a
new sub-shape:

| Member | Sub-shape | Distribution |
|---|---|---|
| GithubRepoAuditor (R11) | pure-internal, PyPI-published | `pip install github-repo-auditor` |
| AIWorkFlow (R17.2) | multi-surface with client portal | Vercel + service host + local CLI |
| NetworkMapper (R17.6) | single-user local-audit clone-and-run | clone + pip + sudo run |
| JSMTicketAnalyticsExport (R18.3) | scheduled CLI with launchd | clone + pip + launchd plist |
| **bridge-db** | **MCP-server-distributed** | **MCP stdio spawned per client** |

This is **deep operator infrastructure** — the MCP-server-
distributed sub-shape is fundamentally different from prior
operator-tool patterns because:

- **No human CLI invocation** — the operator never types
  `bridge-db <command>` interactively. The process is spawned by
  MCP clients (Claude Desktop / Claude Code / Codex) via stdio.
- **No standalone GUI** — observability is via the MCP tools
  themselves (`audit_tail`, `recall_stats`, `health`, `status`).
- **Cross-AI-system state coordination** — the operator runs
  multiple AI systems (Claude.ai, CC, Codex) and bridge-db is
  what lets them share context.
- **Three-way bridge** is genuinely novel — most MCP servers
  serve one client; bridge-db serves three (Claude.ai +
  Claude Code + Codex) with **fallback markdown file path**
  for file-based clients.

Future operator-tool MCP servers batch in this sub-shape.

Release Frozen because:
- Operator explicitly declares "steady maintenance" in README
- Phase 6 (observability) is the **final layer** per operator
- Semantic memory arc was explicitly closed (vector/embedding
  phases dropped after operator decided FTS5 recall was
  sufficient)
- Scope explicitly pinned: "**not a knowledge store**" —
  operator has decided what bridge-db is for and isn't expanding
- 146 tests passing + ruff + pyright clean — no in-flight feature
  work
- Recent commits are docs/checklist/lockfile refresh, not
  features

---

## Cluster taxonomy update

| Cluster | Count | Sub-shapes |
|---|---|---|
| **Operator-tool / dogfood** | **5** | PyPI-published (GHA) / multi-surface-with-portal (AIWF) / local-audit-clone-and-run (NetMapper) / scheduled-CLI-with-launchd (JSM) / **MCP-server-distributed (bridge-db)** |
| (others unchanged) | | |

Operator-tool cluster reaches 5 with 5 distinct sub-shapes —
**most-internally-diverse cluster in portfolio by a wide
margin**. The operator's tooling ecosystem covers PyPI / Vercel /
service host / sudo CLI / launchd / MCP server — five distinct
distribution models for operator-internal infrastructure.

---

## Unblock trigger (operator)

This is operator-core infrastructure — no public "ship"
trigger. Operational concerns:

1. **MCP client compatibility maintenance** — Anthropic's MCP
   spec evolves; verify Claude Desktop / Claude Code / Codex
   bridge-db integration after MCP major version updates.
2. **SQLite WAL mode monitoring** — `wal_size_bytes` +
   `wal_warning` surface via `health` tool catch WAL growth;
   verify alert thresholds.
3. **`shipped_sync_receipts` audit** — `processed_shipped_without_receipt`
   soft drift signal catches older paths. Periodically clean.
4. **`recall` corpus freshness** — FTS5 lexical recall is only
   as good as what's logged via `log_activity` / state sections.
5. **Dependency refresh cadence** — `uv tree --outdated` pass
   per the operator's checklist; PR #20 was the last refresh.
6. **Markdown bridge file regeneration** — `export_bridge_markdown`
   keeps `~/.claude/projects/-Users-d/memory/claude_ai_context.md`
   in sync; used by file-based MCP clients.
7. **Schema migration discipline** — bridge.db schema changes
   risk breaking older MCP clients in flight. Phase 6
   future-schema-rejection hardening reduces but doesn't
   eliminate.

No new feature work expected unless operator opens explicit v2
scope packet (which Phase 6 closure decision indicates is
unlikely).

---

## Portfolio operating system instructions

| Aspect | Posture |
|---|---|
| Portfolio status | `Release Frozen (operator-tool, MCP-server, steady maintenance)` |
| Audience | **Operator self** (three-way bridge: Claude.ai / Claude Code / Codex) |
| Distribution | **MCP stdio** spawned per client (no PyPI, no daemon) |
| Review cadence | **Steady maintenance** (operator-declared) |
| Resurface conditions | (a) MCP spec breaking change, (b) Claude Desktop / CC / Codex bridge-db integration breaks, (c) WAL size warning fires, (d) explicit v2 scope packet (unlikely per Phase 6 closure) |
| Co-batch with | Operator-tool cluster — **now 5 repos with 5 sub-shapes** |
| Sub-shape | **MCP-server-distributed** (first in portfolio; deep operator infrastructure) |
| Special concern | **Cross-AI-system state coordination is load-bearing.** If bridge-db breaks, the operator loses three-way context sharing. Single point of failure for AI tooling. |
| Special concern | **MCP spec stability.** Anthropic's MCP evolves; pin and monitor. |
| Special concern | **SQLite WAL size monitoring** via `health` tool. |
| Special concern | **Scope discipline**: "not a knowledge store." Resist requests to add knowledge-store features; refer to dedicated tools (engraph, etc.). |
| Special concern | **`POST-SYNC-REVIEW.md`** workflow after Bridge Sync runs is operator-canonical. |

---

## Reactivation procedure

1. Verify branch tracking.
2. No stash needed (working tree was clean).
3. **Re-read `OPERATOR-CHECKLIST.md` + `POST-SYNC-REVIEW.md`** —
   operator-authored governance.
4. Verify all three MCP client integrations still functional
   (Claude.ai via Desktop, CC via skills, Codex via stdio).
5. Run `uv sync && pytest` — expect 146 tests passing.
6. Run `ruff check` + `pyright` — expect clean.
7. Run `--doctor`, `--status`, `--dogfood` checks — expect
   healthy.
8. Verify `~/.local/share/bridge-db/bridge.db` WAL size is
   reasonable (not growing unbounded).

---

## Last known reference

| Field | Value |
|---|---|
| `origin/main` tip | `91c38a0` Merge pull request #22 (operator checklist refresh) |
| Default branch | `main` |
| Build system | Python 3.11+ + uv + SQLite (WAL mode) + MCP stdio |
| Distribution | **MCP stdio** spawned per client |
| Audience | **Operator self** (Claude.ai + Claude Code + Codex three-way bridge) |
| Test count | **146 tests passing** |
| Tools | **23 MCP tools** (activity, recent activity, shipped events, sections, snapshots, handoffs, cost, recall, audit_tail, recall_stats, health, status, export_bridge_markdown, sync_from_file) |
| Phases shipped | 0-6 (Phase 6 observability shipped 2026-04-17; Phase −1 semantic memory arc closed) |
| Data lifecycle | `~/.local/share/bridge-db/bridge.db` (SQLite WAL) + markdown export fallback |
| Operator state | **Steady maintenance** (declared in README) |
| Migration state | No `legacy-origin` remote |
| Distinguishing feature | **Fifth operator-tool cluster member; introduces MCP-server-distributed sub-shape (first in portfolio).** Cross-AI-system state coordination across Claude.ai / Claude Code / Codex. Most-internally-diverse cluster in portfolio at 5 sub-shapes. **Load-bearing for operator's AI tooling ecosystem.** |
