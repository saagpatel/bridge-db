# Changelog

All notable changes to bridge-db are documented here.

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
