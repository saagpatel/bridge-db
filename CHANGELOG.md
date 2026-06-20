# Changelog

All notable changes to bridge-db are documented here.

## [Unreleased] — 2026-06-20

### Added — CAS and provenance hardening

- **Schema v10**: context sections now carry integer `version` tokens;
  `context_section_export_state` records the version/hash rendered into the
  fallback markdown file; `write_conflicts` stores durable receipts for stale
  section writes, stale markdown imports, and raced handoff claims.
- **Tool contracts**: `get_section` / `get_all_sections` return `version` and
  `content_sha256`; `update_section` accepts `if_match_version` and returns
  conflict receipts on stale writes; `get_write_conflicts` exposes the receipt
  ledger.
- **Fallback sync**: `sync_from_file` now rejects changed file sections when the
  DB no longer matches the last exported base instead of clobbering fresher DB
  state.
- **Read provenance**: `get_shipped_events` and lifecycle aggregates from
  `get_activity_signal` now carry instruction-boundary metadata.

## [Unreleased] — 2026-06-11

### Added — `source_trust` governance control

Provenance labelling (`operator` | `agent` | `ingested`) on the four instruction-bearing tables,
closing the cross-provider handoff-laundering path (a non-`operator` handoff executed by Codex with
`danger-full-access`).

- **Schema v7**: additive `source_trust` column on `pending_handoffs`, `activity_log`,
  `context_sections`, `system_snapshots`. Conservative backfill — `context_sections` /
  `pending_handoffs` history → `operator`; `activity_log` / `system_snapshots` history keeps the
  `agent` default.
- **Writers**: `create_handoff`, `log_activity`, `update_section`, `save_snapshot` accept an optional
  `source_trust` (default `agent`); `update_section` preserves an existing label on content-only
  updates. `create_handoff` records the chosen trust in the audit log.
- **Pickup gate**: `pick_up_handoff` gates the `pending → active` transition — a non-`operator`
  handoff requires `confirm=True` for `cc` and is refused for `codex` (confirm cannot bypass). Each
  decision (`allowed` / `confirmation_required` / `refused`) is audited.
- **Surfacing**: `get_pending_handoffs` and `recall` hits carry `source_trust`; `status` adds
  `pending_handoffs_by_trust` and `health` a per-table `source_trust_breakdown`.
- The label is DB-only and is never serialized into the markdown export.
