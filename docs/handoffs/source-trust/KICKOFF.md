# KICKOFF — bridge-db source_trust, Phase 0 (Opus 4.8, xHigh)

> Paste this into Claude Code with effort set to **xHigh**. It is tuned for 4.8: explicit scope,
> checkbox DoD, migration acceptance criteria — no "think step by step" scaffolding (redundant at
> this tier). Run Phases 1–3 with the same structure after Phase 0 lands.

---

## Scope
Implement **Phase 0 only** (Schema + type foundation) of the `source_trust` governance control in
`~/Projects/bridge-db`. Do not start Phases 1–3. This is a schema migration on the live
canonical SQLite DB — treat it as high-stakes: additive, idempotent, reversible-by-omission.

## Read before planning
- `docs/handoffs/source-trust/CLAUDE.md` and `docs/handoffs/source-trust/IMPLEMENTATION-ROADMAP.md`
- `src/bridge_db/db.py` — the `SCHEMA_VERSION` ladder, `_SCHEMA_DDL`, the `_MIGRATION_Vx_TO_Vy` runner and how it gates on PRAGMA `user_version`
- `src/bridge_db/models.py` — the `Literal` type pattern (`CallerID`)
- `tests/test_migration.py` and `tests/test_db.py` — the existing fixture + assertion style

## Build (Phase 0)
1. Add `SourceTrust = Literal["operator","agent","ingested"]` to `models.py`.
2. Bump `SCHEMA_VERSION` 6 → 7; add `source_trust TEXT NOT NULL DEFAULT 'agent' CHECK(source_trust IN ('operator','agent','ingested'))` to all four instruction-bearing tables in `_SCHEMA_DDL` (`pending_handoffs`, `activity_log`, `context_sections`, `system_snapshots`).
3. Add `_MIGRATION_V6_TO_V7`: additive `ALTER TABLE ... ADD COLUMN` per table, then conservative backfill — `context_sections` & `pending_handoffs` → `'operator'`; `activity_log` & `system_snapshots` keep the `'agent'` default. Wire it into the version runner, guarded on `user_version < 7`; bump to 7 after.

## Constraints (hard)
- Additive `ALTER` only — no table rename/recreate, no change to existing CHECK constraints.
- Migration idempotent (re-run is a no-op via the version gate).
- No new dependencies. `source_trust` is DB-only — never serialized into the markdown export.
- No tool-behavior change in Phase 0 (writer params and the pickup gate are Phases 1–2).
- FTS `content_index` is untouched (the label is UNINDEXED metadata) — do not call `repopulate_content_index`.

## Definition of Done
- [ ] `models.py` exports `SourceTrust`; type check passes.
- [ ] Fresh DB: `PRAGMA user_version` == 7; all four tables have `source_trust` defaulting to `'agent'`.
- [ ] Populated v6 fixture migrates to v7 with no data loss; backfill values exactly match the rule above.
- [ ] Migration re-run is a no-op (idempotent).
- [ ] `uv run pytest tests/test_db.py tests/test_migration.py -q` → green; full suite still green.
- [ ] `/ultrareview` run at phase end; every finding addressed before commit.

## Process
Start in **plan mode**. Present your Phase 0 plan first — the exact migration SQL, the
`SourceTrust` type, and the `test_migration` fixture/assertions — and wait for my approval before
writing any code. Use the parallel-dispatch proposal in the roadmap (type + DDL + fixture are
independent).
