"""Database schema, migrations, and connection setup."""

import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, NamedTuple, cast

import aiosqlite

from bridge_db import clock, config

logger = logging.getLogger("bridge_db.db")

# Schema version — increment when adding migrations
SCHEMA_VERSION = 23
SNAPSHOT_REFUSAL_EXTENSION_SCHEMA = "BridgeSnapshotRefusalSchemaV1"
OWNER_DELEGATION_EXTENSION_SCHEMA = "BridgeOwnerDelegationSchemaV1"

# A migration post-hook runs after its DDL, before the version bump+commit
# (e.g. FTS repopulation). Its return value is ignored.
_PostHook = Callable[[aiosqlite.Connection], Awaitable[object]]

# Full DDL for current schema (initial create on a fresh DB)
_SCHEMA_DDL = f"""
CREATE TABLE IF NOT EXISTS context_sections (
    section_name TEXT PRIMARY KEY,
    owner TEXT NOT NULL CHECK(owner IN ('claude_ai', 'cc', 'codex')),
    content TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    source_trust TEXT NOT NULL DEFAULT 'agent' CHECK(source_trust IN ('operator', 'agent', 'ingested')),
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1)
);

CREATE TRIGGER IF NOT EXISTS trg_context_total_bytes_insert
BEFORE INSERT ON context_sections
WHEN (
    SELECT COALESCE(SUM(length(CAST(content AS BLOB))), 0)
    FROM context_sections
) + length(CAST(NEW.content AS BLOB)) > {config.CONTEXT_TOTAL_MAX_BYTES}
BEGIN
    SELECT RAISE(ABORT, 'context.total_utf8_bytes_exceeded');
END;

CREATE TRIGGER IF NOT EXISTS trg_context_total_bytes_update
BEFORE UPDATE OF content ON context_sections
WHEN (
    SELECT COALESCE(SUM(length(CAST(content AS BLOB))), 0)
    FROM context_sections
    WHERE section_name != OLD.section_name
) + length(CAST(NEW.content AS BLOB)) > {config.CONTEXT_TOTAL_MAX_BYTES}
AND (
    SELECT COALESCE(SUM(length(CAST(content AS BLOB))), 0)
    FROM context_sections
    WHERE section_name != OLD.section_name
) + length(CAST(NEW.content AS BLOB)) > (
    SELECT COALESCE(SUM(length(CAST(content AS BLOB))), 0)
    FROM context_sections
)
BEGIN
    SELECT RAISE(ABORT, 'context.total_utf8_bytes_exceeded');
END;

CREATE TABLE IF NOT EXISTS context_section_export_state (
    section_name TEXT PRIMARY KEY,
    exported_version INTEGER NOT NULL,
    exported_content_sha256 TEXT NOT NULL,
    exported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY(section_name) REFERENCES context_sections(section_name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bridge_file_export_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    exported_content_sha256 TEXT NOT NULL,
    exported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS bridge_projection_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reason TEXT NOT NULL,
    target_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'completed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    error_category TEXT,
    projected_content_sha256 TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS bridge_export_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    principal TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK(trigger IN ('manual', 'shipped_disposition', 'codex_seed', 'projection_retry')),
    projection_job_id INTEGER,
    previous_content_sha256 TEXT,
    exported_content_sha256 TEXT NOT NULL,
    exported_context_sections INTEGER NOT NULL CHECK(exported_context_sections >= 0),
    byte_count INTEGER NOT NULL CHECK(byte_count >= 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS bridge_import_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    principal TEXT NOT NULL,
    section_name TEXT NOT NULL CHECK(section_name IN ('career', 'speaking', 'research', 'capabilities')),
    previous_version INTEGER,
    imported_version INTEGER NOT NULL CHECK(imported_version >= 1),
    previous_content_sha256 TEXT,
    imported_content_sha256 TEXT NOT NULL,
    imported_source_trust TEXT NOT NULL CHECK(imported_source_trust = 'ingested'),
    fallback_path_sha256 TEXT NOT NULL,
    fallback_file_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS handoff_lifecycle_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('cleared')),
    principal TEXT NOT NULL,
    claimed_caller TEXT NOT NULL,
    requested_project_name TEXT NOT NULL,
    canonical_key TEXT,
    match_basis TEXT NOT NULL CHECK(match_basis IN ('exact', 'canonical_alias')),
    previous_status TEXT NOT NULL,
    previous_claimant TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS handoff_cancellation_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id INTEGER NOT NULL,
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    previous_status TEXT NOT NULL CHECK(previous_status IN ('pending', 'active')),
    previous_claimant TEXT,
    reviewed_row_sha256 TEXT NOT NULL,
    cancelled_by TEXT NOT NULL CHECK(cancelled_by = 'operator-cli'),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS handoff_trust_quarantine (
    handoff_id INTEGER PRIMARY KEY,
    row_json TEXT NOT NULL,
    row_sha256 TEXT NOT NULL,
    previous_source_trust TEXT NOT NULL CHECK(previous_source_trust = 'operator'),
    quarantined_by TEXT NOT NULL CHECK(quarantined_by = 'operator-cli'),
    quarantined_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    restored_at TEXT
);

CREATE TABLE IF NOT EXISTS handoff_session_capabilities (
    handoff_id INTEGER PRIMARY KEY REFERENCES pending_handoffs(id),
    session_id TEXT NOT NULL UNIQUE,
    token_sha256 TEXT NOT NULL UNIQUE CHECK(length(token_sha256) = 64),
    claimed_caller TEXT NOT NULL CHECK(claimed_caller IN ('cc', 'codex')),
    allowed_transition TEXT NOT NULL CHECK(allowed_transition = 'clear'),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    recovered_at TEXT,
    CHECK(consumed_at IS NULL OR recovered_at IS NULL)
);

CREATE INDEX IF NOT EXISTS idx_handoff_capability_expiry
    ON handoff_session_capabilities(expires_at)
    WHERE consumed_at IS NULL AND recovered_at IS NULL;

CREATE TABLE IF NOT EXISTS handoff_orphan_recovery_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id INTEGER NOT NULL UNIQUE,
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    recovery_basis TEXT NOT NULL
        CHECK(recovery_basis IN ('legacy_without_capability', 'expired_capability')),
    previous_status TEXT NOT NULL CHECK(previous_status = 'active'),
    previous_claimant TEXT NOT NULL,
    claim_session_id TEXT,
    capability_expires_at TEXT,
    reviewed_row_sha256 TEXT NOT NULL,
    recovered_by TEXT NOT NULL CHECK(recovered_by = 'operator-cli'),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS write_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    surface TEXT NOT NULL CHECK(surface IN ('context_section', 'markdown_sync', 'handoff')),
    target_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    attempted_by TEXT,
    principal TEXT,
    stale_version INTEGER,
    current_version INTEGER,
    stale_updated_at TEXT,
    current_updated_at TEXT,
    attempted_source_trust TEXT,
    current_source_trust TEXT,
    attempted_content_sha256 TEXT,
    current_content_sha256 TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'acknowledged', 'resolved', 'ignored')),
    detail_json TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    identity_hash TEXT,
    occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK(occurrence_count >= 1),
    last_seen_at TEXT,
    aggregation_state TEXT NOT NULL DEFAULT 'legacy'
        CHECK(aggregation_state IN ('legacy', 'exact_identity', 'capacity_overflow'))
);

CREATE INDEX IF NOT EXISTS idx_write_conflicts_status_created
    ON write_conflicts(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_write_conflicts_surface_target
    ON write_conflicts(surface, target_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_write_conflicts_identity
    ON write_conflicts(identity_hash) WHERE identity_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL CHECK(source IN ('cc', 'codex', 'claude_ai', 'notion_os', 'personal_ops')),
    timestamp TEXT NOT NULL,
    project_name TEXT NOT NULL,
    summary TEXT NOT NULL,
    branch TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    canonical_key TEXT,
    source_trust TEXT NOT NULL DEFAULT 'agent' CHECK(source_trust IN ('operator', 'agent', 'ingested')),
    sync_disposition TEXT CHECK(sync_disposition IS NULL OR sync_disposition IN ('synced', 'unsynced_by_policy', 'no_durable_target', 'superseded_without_receipt', 'declined_mapping')),
    sync_disposition_by TEXT CHECK(sync_disposition_by IS NULL OR sync_disposition_by IN ('cc', 'codex', 'claude_ai', 'notion_os', 'personal_ops')),
    synced_at TEXT,
    sync_downstream_system TEXT,
    sync_downstream_ref TEXT,
    sync_policy_ref TEXT,
    sync_reason TEXT,
    sync_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_activity_source ON activity_log(source);
CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON activity_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_activity_created_id ON activity_log(created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS system_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    system TEXT NOT NULL CHECK(system IN ('cc', 'codex')),
    snapshot_date TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    source_trust TEXT NOT NULL DEFAULT 'agent' CHECK(source_trust IN ('operator', 'agent', 'ingested'))
);

CREATE INDEX IF NOT EXISTS idx_snapshot_system ON system_snapshots(system, created_at DESC);

CREATE TABLE IF NOT EXISTS snapshot_refusals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller TEXT NOT NULL CHECK(caller IN ('cc', 'codex')),
    system TEXT NOT NULL CHECK(system IN ('cc', 'codex')),
    snapshot_family TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    reason_code TEXT NOT NULL CHECK(reason_code = 'snapshot.retention_would_prune'),
    retained_count INTEGER NOT NULL CHECK(retained_count >= 0),
    retention_limit INTEGER NOT NULL CHECK(retention_limit >= 1),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    acknowledgement_state TEXT CHECK(acknowledgement_state IS NULL OR acknowledgement_state IN ('preserve_history', 'retry_after_owner_action', 'superseded')),
    acknowledged_by TEXT CHECK(acknowledged_by IS NULL OR acknowledged_by IN ('cc', 'codex')),
    next_state TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    acknowledged_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_snapshot_refusals_owner_state
    ON snapshot_refusals(caller, acknowledgement_state, created_at DESC);

CREATE TABLE IF NOT EXISTS owner_delegations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT NOT NULL
        CHECK(resource_type IN ('activity_disposition', 'snapshot_refusal')),
    resource_id INTEGER NOT NULL CHECK(resource_id >= 1),
    original_owner TEXT NOT NULL
        CHECK(original_owner IN ('cc', 'codex', 'claude_ai', 'notion_os', 'personal_ops')),
    delegated_to TEXT NOT NULL
        CHECK(delegated_to IN ('cc', 'codex', 'claude_ai', 'notion_os', 'personal_ops')),
    resource_sha256 TEXT NOT NULL CHECK(length(resource_sha256) = 64),
    authorization_reason TEXT NOT NULL CHECK(length(trim(authorization_reason)) > 0),
    authorization_ref TEXT NOT NULL CHECK(length(trim(authorization_ref)) > 0),
    delegated_by TEXT NOT NULL CHECK(delegated_by = 'operator-cli'),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(resource_type, resource_id)
);

CREATE TABLE IF NOT EXISTS owner_delegation_consumptions (
    delegation_id INTEGER PRIMARY KEY REFERENCES owner_delegations(id),
    actor TEXT NOT NULL
        CHECK(actor IN ('cc', 'codex', 'claude_ai', 'notion_os', 'personal_ops')),
    action TEXT NOT NULL CHECK(length(trim(action)) > 0),
    result_sha256 TEXT NOT NULL CHECK(length(result_sha256) = 64),
    consumed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_owner_delegations_target
    ON owner_delegations(delegated_to, resource_type, resource_id);

CREATE TABLE IF NOT EXISTS pending_handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL,
    project_path TEXT,
    roadmap_file TEXT,
    phase TEXT,
    dispatched_from TEXT NOT NULL DEFAULT 'claude_ai',
    dispatched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    picked_up_at TEXT,
    cleared_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'active', 'cleared')),
    canonical_key TEXT,
    source_trust TEXT NOT NULL DEFAULT 'agent' CHECK(source_trust IN ('operator', 'agent', 'ingested')),
    claimed_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_handoff_status ON pending_handoffs(status);

CREATE TABLE IF NOT EXISTS cost_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    system TEXT NOT NULL CHECK(system IN ('cc', 'codex', 'notion_os', 'personal_ops')),
    month TEXT NOT NULL,
    amount REAL NOT NULL,
    notes TEXT,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(system, month)
);

-- Shipped-event sync state (receipts + policy dispositions) lives on the
-- activity row itself in the sync_* columns above (schema v14). The former
-- shipped_sync_receipts and shipped_event_dispositions child tables were
-- collapsed away — see _MIGRATION_V13_TO_V14.

CREATE TABLE IF NOT EXISTS session_costs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL UNIQUE,
    project_name    TEXT,
    started_at      TEXT    NOT NULL,
    cost_usd        REAL    NOT NULL,
    model_breakdown TEXT,
    source          TEXT    NOT NULL DEFAULT 'cc',
    recorded_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_sc_project ON session_costs(project_name);
CREATE INDEX IF NOT EXISTS idx_sc_started ON session_costs(started_at DESC);

CREATE TABLE IF NOT EXISTS session_classification (
    session_id     TEXT PRIMARY KEY REFERENCES session_costs(session_id),
    role           TEXT,
    task_class     TEXT,
    routing_basis  TEXT,
    dominant_model TEXT,
    confidence     REAL,
    method         TEXT NOT NULL,
    classified_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_scl_routing ON session_classification(routing_basis);

CREATE VIRTUAL TABLE IF NOT EXISTS content_index USING fts5(
    source_type UNINDEXED,
    source_id UNINDEXED,
    text,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
"""

# Migration from v3 → v4: add shipped-event sync receipts.
_MIGRATION_V3_TO_V4 = """
CREATE TABLE IF NOT EXISTS shipped_sync_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id INTEGER NOT NULL UNIQUE,
    downstream_system TEXT NOT NULL,
    downstream_ref TEXT NOT NULL,
    synced_by TEXT NOT NULL CHECK(synced_by IN ('cc', 'codex', 'claude_ai', 'notion_os', 'personal_ops')),
    synced_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    notes TEXT,
    FOREIGN KEY(activity_id) REFERENCES activity_log(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_shipped_sync_downstream
    ON shipped_sync_receipts(downstream_system, downstream_ref);
"""

# Migration from v2 → v3: add content_index FTS5 virtual table.
# Rows are populated by repopulate_content_index() after the DDL runs.
_MIGRATION_V2_TO_V3 = """
CREATE VIRTUAL TABLE IF NOT EXISTS content_index USING fts5(
    source_type UNINDEXED,
    source_id UNINDEXED,
    text,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
"""

# Migration from v1 → v2: expand CHECK constraints on activity_log and cost_records.
# SQLite cannot ALTER COLUMN check constraints; must rename+recreate.
# Also ensures all other v2 tables exist (IF NOT EXISTS is a no-op on real v1
# DBs that already had them; defensive for reconstructed-from-minimal v1 DBs).
_MIGRATION_V1_TO_V2 = """
ALTER TABLE activity_log RENAME TO activity_log_v1;

CREATE TABLE activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL CHECK(source IN ('cc', 'codex', 'claude_ai', 'notion_os', 'personal_ops')),
    timestamp TEXT NOT NULL,
    project_name TEXT NOT NULL,
    summary TEXT NOT NULL,
    branch TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

INSERT INTO activity_log SELECT * FROM activity_log_v1;
DROP TABLE activity_log_v1;

CREATE INDEX IF NOT EXISTS idx_activity_source ON activity_log(source);
CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON activity_log(timestamp DESC);

ALTER TABLE cost_records RENAME TO cost_records_v1;

CREATE TABLE cost_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    system TEXT NOT NULL CHECK(system IN ('cc', 'codex', 'notion_os', 'personal_ops')),
    month TEXT NOT NULL,
    amount REAL NOT NULL,
    notes TEXT,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(system, month)
);

INSERT INTO cost_records SELECT * FROM cost_records_v1;
DROP TABLE cost_records_v1;

CREATE TABLE IF NOT EXISTS context_sections (
    section_name TEXT PRIMARY KEY,
    owner TEXT NOT NULL CHECK(owner IN ('claude_ai', 'cc', 'codex')),
    content TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS system_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    system TEXT NOT NULL CHECK(system IN ('cc', 'codex')),
    snapshot_date TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_snapshot_system ON system_snapshots(system, created_at DESC);

CREATE TABLE IF NOT EXISTS pending_handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL,
    project_path TEXT,
    roadmap_file TEXT,
    phase TEXT,
    dispatched_from TEXT NOT NULL DEFAULT 'claude_ai',
    dispatched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    picked_up_at TEXT,
    cleared_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'active', 'cleared'))
);

CREATE INDEX IF NOT EXISTS idx_handoff_status ON pending_handoffs(status);
"""

# Migration v4 → v5: add nullable canonical_key to activity_log so each entry
# can carry the resolved canonical project key (see project_resolver). Additive
# and FTS-neutral — canonical_key is not part of fts_text_for_activity.
_MIGRATION_V4_TO_V5 = """
ALTER TABLE activity_log ADD COLUMN canonical_key TEXT;
"""

# Migration v5 → v6: extend canonical resolution to the handoff queue by adding
# nullable canonical_key to pending_handoffs (mirrors the v5 activity_log change).
# create_handoff resolves on write; clear_handoff matches on it so a handoff
# dispatched under one project_name still clears when /end passes a sibling alias
# (F1 consumer adoption). Additive and FTS-neutral — canonical_key is not part of
# fts_text_for_handoff.
_MIGRATION_V5_TO_V6 = """
ALTER TABLE pending_handoffs ADD COLUMN canonical_key TEXT;
"""

# Migration v6 → v7: add the source_trust provenance label to the four
# instruction-bearing tables. Additive ADD COLUMN — SQLite permits a column CHECK
# when the constant default satisfies it (it does), so no rename/recreate.
# Provenance-free history cannot prove operator review. Pre-existing context and
# handoff rows therefore become 'ingested'; activity and snapshot history keeps
# the non-privileged 'agent' default. source_trust is not FTS-indexed, so
# content_index is untouched (no repopulate_content_index).
_MIGRATION_V6_TO_V7 = """
ALTER TABLE pending_handoffs ADD COLUMN source_trust TEXT NOT NULL DEFAULT 'agent'
    CHECK(source_trust IN ('operator', 'agent', 'ingested'));
ALTER TABLE activity_log ADD COLUMN source_trust TEXT NOT NULL DEFAULT 'agent'
    CHECK(source_trust IN ('operator', 'agent', 'ingested'));
ALTER TABLE context_sections ADD COLUMN source_trust TEXT NOT NULL DEFAULT 'agent'
    CHECK(source_trust IN ('operator', 'agent', 'ingested'));
ALTER TABLE system_snapshots ADD COLUMN source_trust TEXT NOT NULL DEFAULT 'agent'
    CHECK(source_trust IN ('operator', 'agent', 'ingested'));
UPDATE context_sections SET source_trust = 'ingested';
UPDATE pending_handoffs SET source_trust = 'ingested';
"""


# Migration v7 → v8: add shipped-event policy dispositions. This is deliberately
# separate from shipped_sync_receipts: dispositions explain why an event is not
# receipt-ready, while receipts prove downstream sync.
_MIGRATION_V7_TO_V8 = """
CREATE TABLE IF NOT EXISTS shipped_event_dispositions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id INTEGER NOT NULL UNIQUE,
    disposition_type TEXT NOT NULL CHECK(disposition_type IN (
        'unsynced_by_policy',
        'no_durable_target',
        'superseded_without_receipt',
        'declined_mapping'
    )),
    policy_ref TEXT,
    reason TEXT NOT NULL,
    decided_by TEXT NOT NULL CHECK(decided_by IN ('cc', 'codex', 'claude_ai', 'notion_os', 'personal_ops')),
    decided_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    notes TEXT,
    FOREIGN KEY(activity_id) REFERENCES activity_log(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_shipped_disposition_type
    ON shipped_event_dispositions(disposition_type);
"""


# Migration v8 → v9: add session_costs table for per-project cost attribution.
# Structured cost table — not FTS-indexed, no content_index changes required.
_MIGRATION_V8_TO_V9 = """
CREATE TABLE IF NOT EXISTS session_costs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL UNIQUE,
    project_name    TEXT,
    started_at      TEXT    NOT NULL,
    cost_usd        REAL    NOT NULL,
    model_breakdown TEXT,
    source          TEXT    NOT NULL DEFAULT 'cc',
    recorded_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_sc_project ON session_costs(project_name);
CREATE INDEX IF NOT EXISTS idx_sc_started ON session_costs(started_at DESC);
"""


# Migration v9 → v10: add integer-version CAS for context sections plus durable
# conflict receipts and markdown-export base state. FTS-neutral: no indexed text
# changes.
_MIGRATION_V9_TO_V10 = """
ALTER TABLE context_sections ADD COLUMN version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1);

CREATE TABLE IF NOT EXISTS context_section_export_state (
    section_name TEXT PRIMARY KEY,
    exported_version INTEGER NOT NULL,
    exported_content_sha256 TEXT NOT NULL,
    exported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY(section_name) REFERENCES context_sections(section_name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS write_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    surface TEXT NOT NULL CHECK(surface IN ('context_section', 'markdown_sync', 'handoff')),
    target_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    attempted_by TEXT,
    principal TEXT,
    stale_version INTEGER,
    current_version INTEGER,
    stale_updated_at TEXT,
    current_updated_at TEXT,
    attempted_source_trust TEXT,
    current_source_trust TEXT,
    attempted_content_sha256 TEXT,
    current_content_sha256 TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'acknowledged', 'resolved', 'ignored')),
    detail_json TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_write_conflicts_status_created
    ON write_conflicts(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_write_conflicts_surface_target
    ON write_conflicts(surface, target_key);
"""


# Migration v10 → v11: data-only, no schema change. The B3 change made
# fts_text_for_activity append tags, but it shipped without a version bump, so
# DBs already at v10 keep content_index rows built before tags were indexed and
# tag/lifecycle searches (recall("SHIPPED"), DECISION, ...) miss historical rows.
# The post-hook re-indexes activity rows so their tags become searchable.
_MIGRATION_V10_TO_V11 = (
    "-- v10 → v11: data-only; content_index rebuild runs in the post-hook.\n"
)


# Migration v11 → v12: add heuristic session classification sidecar for
# cost-attribution consumers. session_costs remains the actuals table.
_MIGRATION_V11_TO_V12 = """
CREATE TABLE IF NOT EXISTS session_classification (
    session_id     TEXT PRIMARY KEY REFERENCES session_costs(session_id),
    role           TEXT,
    task_class     TEXT,
    routing_basis  TEXT,
    dominant_model TEXT,
    confidence     REAL,
    method         TEXT NOT NULL,
    classified_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_scl_routing ON session_classification(routing_basis);
"""


# Migration v12 → v13: add claimed_by to pending_handoffs so INV-13's
# handoff-claimant fix has a durable column to record against.
_MIGRATION_V12_TO_V13 = """
ALTER TABLE pending_handoffs ADD COLUMN claimed_by TEXT;
"""


# Migration v13 → v14: collapse the shipped-sync trio into activity-row columns.
# shipped_sync_receipts + shipped_event_dispositions were a normalized subsystem
# for ~36 lifetime rows of a per-row yes/no/why. Shipped events ARE activity
# rows, so their sync disposition now lives on the row in sync_* columns and the
# two FK-CASCADE child tables are dropped after a lossless copy.
#
# The single sync_disposition column is the discriminator: 'synced' means a
# receipt-backed downstream proof (carries downstream_system/ref); the four
# policy values mean a non-receipt policy decision (carries reason/policy_ref).
# synced_at holds the receipt's synced_at or the disposition's decided_at;
# sync_note holds either notes field. The receipt copy runs first and the
# disposition copy skips any row that already has a receipt, so on the
# (structurally near-impossible) both-state row 'synced' wins — the collapse
# cannot represent contradictory receipt+disposition simultaneously, which the
# old two-table model technically could via disposition-then-confirm.
#
# Dropping the child tables also removes the documented BD-INV-1 cascade
# time-bomb outright: a disposition can no longer outlive or be separated from
# its row because it IS the row.
#
# The step DDL is intentionally empty: EVERY v14 change (the guarded ADD COLUMNs,
# the lossless copy, and the DROP of the child tables) runs in the
# _migrate_shipped_state_to_columns post-hook so the whole step is idempotent and
# crash-safe. `executescript` commits each ADD COLUMN at the engine level
# regardless of the ladder's later commit, so an ADD-COLUMN-in-the-DDL approach
# is NOT crash-safe: a kill between the ALTERs and the user_version bump would
# leave the columns durably present at v13 and brick the next boot with a
# duplicate-column error on re-run. Guarding each ADD on the live column set (and
# each copy/DROP on table existence) makes a re-run after any interrupted attempt
# a clean no-op that still converges to v14.
_MIGRATION_V13_TO_V14 = "-- v13 → v14: all changes run in the _migrate_shipped_state_to_columns post-hook.\n"

_MIGRATION_V14_TO_V15 = """
CREATE TABLE IF NOT EXISTS bridge_file_export_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    exported_content_sha256 TEXT NOT NULL,
    exported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""

_MIGRATION_V15_TO_V16 = """
CREATE TABLE IF NOT EXISTS bridge_projection_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reason TEXT NOT NULL,
    target_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'completed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    error_category TEXT,
    projected_content_sha256 TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    completed_at TEXT
);
"""

_MIGRATION_V16_TO_V17 = """
CREATE TABLE IF NOT EXISTS handoff_lifecycle_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('cleared')),
    principal TEXT NOT NULL,
    claimed_caller TEXT NOT NULL,
    requested_project_name TEXT NOT NULL,
    canonical_key TEXT,
    match_basis TEXT NOT NULL CHECK(match_basis IN ('exact', 'canonical_alias')),
    previous_status TEXT NOT NULL,
    previous_claimant TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""

_MIGRATION_V17_TO_V18 = """
CREATE TABLE IF NOT EXISTS bridge_export_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    principal TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK(trigger IN ('manual', 'shipped_disposition', 'codex_seed', 'projection_retry')),
    projection_job_id INTEGER,
    previous_content_sha256 TEXT,
    exported_content_sha256 TEXT NOT NULL,
    exported_context_sections INTEGER NOT NULL CHECK(exported_context_sections >= 0),
    byte_count INTEGER NOT NULL CHECK(byte_count >= 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""

_MIGRATION_V18_TO_V19 = """
CREATE TABLE IF NOT EXISTS bridge_import_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    principal TEXT NOT NULL,
    section_name TEXT NOT NULL CHECK(section_name IN ('career', 'speaking', 'research', 'capabilities')),
    previous_version INTEGER,
    imported_version INTEGER NOT NULL CHECK(imported_version >= 1),
    previous_content_sha256 TEXT,
    imported_content_sha256 TEXT NOT NULL,
    imported_source_trust TEXT NOT NULL CHECK(imported_source_trust = 'ingested'),
    fallback_path_sha256 TEXT NOT NULL,
    fallback_file_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""

_MIGRATION_V19_TO_V20 = """
CREATE INDEX IF NOT EXISTS idx_activity_created_id
ON activity_log(created_at DESC, id DESC);
"""

_MIGRATION_V20_TO_V21 = ""

_MIGRATION_V21_TO_V22 = """
CREATE TABLE IF NOT EXISTS handoff_cancellation_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id INTEGER NOT NULL,
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    previous_status TEXT NOT NULL CHECK(previous_status IN ('pending', 'active')),
    previous_claimant TEXT,
    reviewed_row_sha256 TEXT NOT NULL,
    cancelled_by TEXT NOT NULL CHECK(cancelled_by = 'operator-cli'),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS handoff_trust_quarantine (
    handoff_id INTEGER PRIMARY KEY,
    row_json TEXT NOT NULL,
    row_sha256 TEXT NOT NULL,
    previous_source_trust TEXT NOT NULL CHECK(previous_source_trust = 'operator'),
    quarantined_by TEXT NOT NULL CHECK(quarantined_by = 'operator-cli'),
    quarantined_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    restored_at TEXT
);
"""

_MIGRATION_V22_TO_V23 = """
CREATE TABLE IF NOT EXISTS handoff_session_capabilities (
    handoff_id INTEGER PRIMARY KEY REFERENCES pending_handoffs(id),
    session_id TEXT NOT NULL UNIQUE,
    token_sha256 TEXT NOT NULL UNIQUE CHECK(length(token_sha256) = 64),
    claimed_caller TEXT NOT NULL CHECK(claimed_caller IN ('cc', 'codex')),
    allowed_transition TEXT NOT NULL CHECK(allowed_transition = 'clear'),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    recovered_at TEXT,
    CHECK(consumed_at IS NULL OR recovered_at IS NULL)
);

CREATE INDEX IF NOT EXISTS idx_handoff_capability_expiry
    ON handoff_session_capabilities(expires_at)
    WHERE consumed_at IS NULL AND recovered_at IS NULL;

CREATE TABLE IF NOT EXISTS handoff_orphan_recovery_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id INTEGER NOT NULL UNIQUE,
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    recovery_basis TEXT NOT NULL
        CHECK(recovery_basis IN ('legacy_without_capability', 'expired_capability')),
    previous_status TEXT NOT NULL CHECK(previous_status = 'active'),
    previous_claimant TEXT NOT NULL,
    claim_session_id TEXT,
    capability_expires_at TEXT,
    reviewed_row_sha256 TEXT NOT NULL,
    recovered_by TEXT NOT NULL CHECK(recovered_by = 'operator-cli'),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""

# BridgeSnapshotRefusalSchemaV1 is deliberately additive over the core schema.
# Existing v23 databases from the exact previous merged generation receive this
# table without advancing user_version, so pointer rollback keeps its core upper
# bound while preserving refusal rows for roll-forward.
_SNAPSHOT_REFUSAL_EXTENSION_DDL = """
CREATE TABLE IF NOT EXISTS snapshot_refusals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller TEXT NOT NULL CHECK(caller IN ('cc', 'codex')),
    system TEXT NOT NULL CHECK(system IN ('cc', 'codex')),
    snapshot_family TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    reason_code TEXT NOT NULL CHECK(reason_code = 'snapshot.retention_would_prune'),
    retained_count INTEGER NOT NULL CHECK(retained_count >= 0),
    retention_limit INTEGER NOT NULL CHECK(retention_limit >= 1),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    acknowledgement_state TEXT CHECK(acknowledgement_state IS NULL OR acknowledgement_state IN ('preserve_history', 'retry_after_owner_action', 'superseded')),
    acknowledged_by TEXT CHECK(acknowledged_by IS NULL OR acknowledged_by IN ('cc', 'codex')),
    next_state TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    acknowledged_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_snapshot_refusals_owner_state
    ON snapshot_refusals(caller, acknowledgement_state, created_at DESC);
"""

# BridgeOwnerDelegationSchemaV1 is additive over core v23. It preserves the
# original resource owner and records operator-approved, exact-resource grants
# plus separate one-time consumption receipts. Previous v23 runtimes ignore and
# preserve these tables; they do not gain delegated write authority.
_OWNER_DELEGATION_EXTENSION_DDL = """
CREATE TABLE IF NOT EXISTS owner_delegations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT NOT NULL
        CHECK(resource_type IN ('activity_disposition', 'snapshot_refusal')),
    resource_id INTEGER NOT NULL CHECK(resource_id >= 1),
    original_owner TEXT NOT NULL
        CHECK(original_owner IN ('cc', 'codex', 'claude_ai', 'notion_os', 'personal_ops')),
    delegated_to TEXT NOT NULL
        CHECK(delegated_to IN ('cc', 'codex', 'claude_ai', 'notion_os', 'personal_ops')),
    resource_sha256 TEXT NOT NULL CHECK(length(resource_sha256) = 64),
    authorization_reason TEXT NOT NULL CHECK(length(trim(authorization_reason)) > 0),
    authorization_ref TEXT NOT NULL CHECK(length(trim(authorization_ref)) > 0),
    delegated_by TEXT NOT NULL CHECK(delegated_by = 'operator-cli'),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(resource_type, resource_id)
);

CREATE TABLE IF NOT EXISTS owner_delegation_consumptions (
    delegation_id INTEGER PRIMARY KEY REFERENCES owner_delegations(id),
    actor TEXT NOT NULL
        CHECK(actor IN ('cc', 'codex', 'claude_ai', 'notion_os', 'personal_ops')),
    action TEXT NOT NULL CHECK(length(trim(action)) > 0),
    result_sha256 TEXT NOT NULL CHECK(length(result_sha256) = 64),
    consumed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_owner_delegations_target
    ON owner_delegations(delegated_to, resource_type, resource_id);
"""

# Column definitions for the v14 ADD COLUMN step. Kept character-identical to the
# activity_log block in _SCHEMA_DDL so a fresh install and a migrated DB converge
# (see tests/test_schema_convergence_concurrency.py). NOTE: the synced/policy
# field requirements the old NOT NULL child-table columns enforced (a 'synced'
# row has downstream_system+ref, a policy row has a reason) are NOT expressed as
# CHECK constraints here — SQLite cannot ADD a column with a CHECK that
# references OTHER columns, and a table-level cross-column CHECK would require a
# full activity_log rebuild (the FTS-mirrored core table). record_disposition is
# the sole writer and validates those requirements; the health
# receipt_orphan_count / disposition_orphan_count metrics are the compensating
# detection control (they flag a 'synced' row missing downstream proof or a
# policy row missing its reason, and must always read 0).
_V14_SYNC_COLUMNS: tuple[tuple[str, str], ...] = (
    (
        "sync_disposition",
        "TEXT CHECK(sync_disposition IS NULL OR sync_disposition IN ('synced', 'unsynced_by_policy', 'no_durable_target', 'superseded_without_receipt', 'declined_mapping'))",
    ),
    (
        "sync_disposition_by",
        "TEXT CHECK(sync_disposition_by IS NULL OR sync_disposition_by IN ('cc', 'codex', 'claude_ai', 'notion_os', 'personal_ops'))",
    ),
    ("synced_at", "TEXT"),
    ("sync_downstream_system", "TEXT"),
    ("sync_downstream_ref", "TEXT"),
    ("sync_policy_ref", "TEXT"),
    ("sync_reason", "TEXT"),
    ("sync_note", "TEXT"),
)


async def apply_pragmas(db: aiosqlite.Connection) -> None:
    """Apply all required PRAGMAs. Safe to call on every connection open."""
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA busy_timeout=15000")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA cache_size=-64000")
    await db.commit()


async def checkpoint_wal(db: aiosqlite.Connection) -> dict[str, int]:
    """Force a WAL checkpoint (TRUNCATE) to bound -wal growth (FMEA 1.3).

    Under WAL with many always-open reader connections the passive
    autocheckpoint can starve — there is never a reader-free moment — so the
    -wal grows unbounded (observed at 4.2 MB against a 901 KB main DB). A
    periodic TRUNCATE checkpoint reclaims it. Must be called outside a write
    transaction. Returns the checkpoint row: busy (1 = could not fully complete,
    readers active), log_frames, checkpointed.
    """
    cursor = await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    row = await cursor.fetchone()
    if row is None:
        return {"busy": 1, "log_frames": -1, "checkpointed": -1}
    return {"busy": int(row[0]), "log_frames": int(row[1]), "checkpointed": int(row[2])}


async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    )
    return await cursor.fetchone() is not None


async def _backup_db_file(db: aiosqlite.Connection, label: str) -> None:
    """Write a one-time verified logical backup before a destructive
    migration step, so a botched or interrupted migration is repairable.

    No-op for an in-memory DB (no file path). Idempotent: if the backup already
    exists (e.g. from a prior interrupted attempt) the pristine copy is kept
    rather than overwritten with partially-migrated state. SQLite's online
    backup API includes committed WAL-resident state without requiring a
    successful checkpoint.
    """
    cursor = await db.execute("PRAGMA database_list")
    main_path = next(
        (row[2] for row in await cursor.fetchall() if row[1] == "main" and row[2]),
        None,
    )
    if not main_path:
        return
    backup = Path(f"{main_path}.{label}.bak")
    manifest = Path(f"{backup}.sha256")
    metadata = Path(f"{backup}.meta.json")
    version_row = await (await db.execute("PRAGMA user_version")).fetchone()
    expected_version = int(version_row[0]) if version_row is not None else -1

    def validate(path: Path, digest_path: Path) -> None:
        if not digest_path.exists():
            raise RuntimeError(
                f"migration backup {path} has no verification manifest; "
                "refusing destructive migration"
            )
        expected_digest = digest_path.read_text(encoding="utf-8").strip()
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected_digest != actual_digest:
            raise RuntimeError(
                f"migration backup {path} failed digest verification; "
                "refusing destructive migration"
            )
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as check:
            integrity = check.execute("PRAGMA integrity_check").fetchone()
            backup_version = int(check.execute("PRAGMA user_version").fetchone()[0])
        if (
            integrity is None
            or integrity[0] != "ok"
            or backup_version != expected_version
        ):
            raise RuntimeError(
                f"migration backup {path} failed SQLite/version verification; "
                "refusing destructive migration"
            )

    if backup.exists():
        validate(backup, manifest)
        logger.info("migration backup already present at %s; keeping it", backup)
        return
    await db.commit()
    temporary = Path(f"{backup}.tmp")
    temporary.unlink(missing_ok=True)
    temporary_manifest = Path(f"{manifest}.tmp")
    temporary_manifest.unlink(missing_ok=True)
    temporary_metadata = Path(f"{metadata}.tmp")
    temporary_metadata.unlink(missing_ok=True)
    target = sqlite3.connect(temporary)
    try:
        await db.backup(target)
    finally:
        target.close()
    temporary_manifest.write_text(
        hashlib.sha256(temporary.read_bytes()).hexdigest() + "\n", encoding="utf-8"
    )
    temporary_metadata.write_text(
        json.dumps(
            {
                "schema": "MigrationBackupEvidenceV1",
                "created_at": clock.now().isoformat().replace("+00:00", "Z"),
                "label": label,
                "source_schema_version": expected_version,
                "backup_bytes": temporary.stat().st_size,
                "sha256": temporary_manifest.read_text(encoding="utf-8").strip(),
                "sqlite_integrity": "ok",
                "recovery_readback": "verified",
                "retention_policy": "operator_acknowledgement_required",
                "cleanup": "approval_required",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.chmod(temporary_manifest, 0o600)
    os.chmod(temporary_metadata, 0o600)
    validate(temporary, temporary_manifest)
    os.replace(temporary, backup)
    os.replace(temporary_manifest, manifest)
    os.replace(temporary_metadata, metadata)
    logger.info("migration backup written to %s", backup)


async def _migrate_shipped_state_to_columns(db: aiosqlite.Connection) -> None:
    """v13→v14 post-hook: add the sync_* columns, copy the shipped_sync_receipts +
    shipped_event_dispositions child tables onto them, then drop the child tables.

    Idempotent and crash-safe. Each ADD COLUMN is guarded on the live column set
    (PRAGMA table_info) and each copy/DROP on table existence, so re-running this
    step after an interrupted migration (columns present but user_version still
    13) is a clean no-op that still converges to v14 instead of raising
    duplicate-column. Before touching a DB that still holds legacy child-table
    state, a one-time file backup is taken so the irreversible DROP is repairable.

    A complete v13 DB carries both tables (created at v4 and v8). A receipt maps
    to the 'synced' disposition; a policy row keeps its disposition_type. The
    receipt copy runs first and the disposition copy skips any row that already
    has a receipt, so a (structurally near-impossible) both-state row resolves to
    'synced' — the single column cannot hold contradictory receipt+disposition
    state.
    """
    receipts_exist = await _table_exists(db, "shipped_sync_receipts")
    dispositions_exist = await _table_exists(db, "shipped_event_dispositions")

    # Back up before the destructive collapse when legacy child-table state that
    # the DROP would otherwise make unrecoverable is present.
    if receipts_exist or dispositions_exist:
        await _backup_db_file(db, "pre-v14")

    # Idempotent ADD COLUMN: skip any column already present so a crash between
    # the ALTERs and the user_version bump cannot brick the next boot.
    cursor = await db.execute("PRAGMA table_info(activity_log)")
    existing_columns = {row[1] for row in await cursor.fetchall()}
    for name, decl in _V14_SYNC_COLUMNS:
        if name not in existing_columns:
            await db.execute(
                f"ALTER TABLE activity_log ADD COLUMN {name} {decl}"  # noqa: S608 — name/decl from the closed _V14_SYNC_COLUMNS literal
            )

    if receipts_exist:
        await db.execute(
            """
            UPDATE activity_log SET
                sync_disposition = 'synced',
                sync_downstream_system = (SELECT r.downstream_system FROM shipped_sync_receipts AS r WHERE r.activity_id = activity_log.id),
                sync_downstream_ref = (SELECT r.downstream_ref FROM shipped_sync_receipts AS r WHERE r.activity_id = activity_log.id),
                sync_disposition_by = (SELECT r.synced_by FROM shipped_sync_receipts AS r WHERE r.activity_id = activity_log.id),
                synced_at = (SELECT r.synced_at FROM shipped_sync_receipts AS r WHERE r.activity_id = activity_log.id),
                sync_note = (SELECT r.notes FROM shipped_sync_receipts AS r WHERE r.activity_id = activity_log.id)
            WHERE EXISTS (SELECT 1 FROM shipped_sync_receipts AS r WHERE r.activity_id = activity_log.id)
            """  # noqa: S608 — closed literal SQL, no interpolated values
        )

    if dispositions_exist:
        # Only exclude receipt-backed rows when the receipts table is present;
        # if it is absent there is no receipt to lose the tie to.
        receipt_guard = (
            " AND NOT EXISTS (SELECT 1 FROM shipped_sync_receipts AS r "
            "WHERE r.activity_id = activity_log.id)"
            if receipts_exist
            else ""
        )
        await db.execute(
            f"""
            UPDATE activity_log SET
                sync_disposition = (SELECT d.disposition_type FROM shipped_event_dispositions AS d WHERE d.activity_id = activity_log.id),
                sync_policy_ref = (SELECT d.policy_ref FROM shipped_event_dispositions AS d WHERE d.activity_id = activity_log.id),
                sync_reason = (SELECT d.reason FROM shipped_event_dispositions AS d WHERE d.activity_id = activity_log.id),
                sync_disposition_by = (SELECT d.decided_by FROM shipped_event_dispositions AS d WHERE d.activity_id = activity_log.id),
                synced_at = (SELECT d.decided_at FROM shipped_event_dispositions AS d WHERE d.activity_id = activity_log.id),
                sync_note = (SELECT d.notes FROM shipped_event_dispositions AS d WHERE d.activity_id = activity_log.id)
            WHERE EXISTS (SELECT 1 FROM shipped_event_dispositions AS d WHERE d.activity_id = activity_log.id){receipt_guard}
            """  # noqa: S608 — receipt_guard is a closed literal, not user input
        )

    await db.execute("DROP TABLE IF EXISTS shipped_sync_receipts")
    await db.execute("DROP TABLE IF EXISTS shipped_event_dispositions")


async def _migrate_conflict_aggregation(db: aiosqlite.Connection) -> None:
    """Add v21 aggregation fields without rewriting or deleting legacy receipts."""
    if not await _table_exists(db, "write_conflicts"):
        await db.executescript(
            """
            CREATE TABLE write_conflicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                surface TEXT NOT NULL CHECK(surface IN ('context_section', 'markdown_sync', 'handoff')),
                target_key TEXT NOT NULL,
                operation TEXT NOT NULL,
                attempted_by TEXT,
                principal TEXT,
                stale_version INTEGER,
                current_version INTEGER,
                stale_updated_at TEXT,
                current_updated_at TEXT,
                attempted_source_trust TEXT,
                current_source_trust TEXT,
                attempted_content_sha256 TEXT,
                current_content_sha256 TEXT,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'acknowledged', 'resolved', 'ignored')),
                detail_json TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                identity_hash TEXT,
                occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK(occurrence_count >= 1),
                last_seen_at TEXT,
                aggregation_state TEXT NOT NULL DEFAULT 'legacy'
                    CHECK(aggregation_state IN ('legacy', 'exact_identity', 'capacity_overflow'))
            );
            """
        )
    else:
        cursor = await db.execute("PRAGMA table_info(write_conflicts)")
        columns = {str(row["name"]) for row in await cursor.fetchall()}
        additions = {
            "identity_hash": "TEXT",
            "occurrence_count": (
                "INTEGER NOT NULL DEFAULT 1 CHECK(occurrence_count >= 1)"
            ),
            "last_seen_at": "TEXT",
            "aggregation_state": (
                "TEXT NOT NULL DEFAULT 'legacy' "
                "CHECK(aggregation_state IN "
                "('legacy', 'exact_identity', 'capacity_overflow'))"
            ),
        }
        for name, declaration in additions.items():
            if name not in columns:
                await db.execute(
                    f"ALTER TABLE write_conflicts ADD COLUMN {name} {declaration}"
                )  # noqa: S608 — closed migration literals
    await db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_write_conflicts_status_created
            ON write_conflicts(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_write_conflicts_surface_target
            ON write_conflicts(surface, target_key);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_write_conflicts_identity
            ON write_conflicts(identity_hash) WHERE identity_hash IS NOT NULL;
        """
    )
    if await _table_exists(db, "context_sections"):
        await db.executescript(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_context_total_bytes_insert
        BEFORE INSERT ON context_sections
        WHEN (
            SELECT COALESCE(SUM(length(CAST(content AS BLOB))), 0)
            FROM context_sections
        ) + length(CAST(NEW.content AS BLOB)) > {config.CONTEXT_TOTAL_MAX_BYTES}
        BEGIN
            SELECT RAISE(ABORT, 'context.total_utf8_bytes_exceeded');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_context_total_bytes_update
        BEFORE UPDATE OF content ON context_sections
        WHEN (
            SELECT COALESCE(SUM(length(CAST(content AS BLOB))), 0)
            FROM context_sections
            WHERE section_name != OLD.section_name
        ) + length(CAST(NEW.content AS BLOB)) > {config.CONTEXT_TOTAL_MAX_BYTES}
        AND (
            SELECT COALESCE(SUM(length(CAST(content AS BLOB))), 0)
            FROM context_sections
            WHERE section_name != OLD.section_name
        ) + length(CAST(NEW.content AS BLOB)) > (
            SELECT COALESCE(SUM(length(CAST(content AS BLOB))), 0)
            FROM context_sections
        )
        BEGIN
            SELECT RAISE(ABORT, 'context.total_utf8_bytes_exceeded');
        END;
        """
        )


async def ensure_schema(db: aiosqlite.Connection) -> None:
    """Create tables if not present; run any pending migrations in sequence."""
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    current_version: int = row[0] if row else 0  # type: ignore[index]

    if current_version > SCHEMA_VERSION:
        msg = (
            "Database schema version is newer than this bridge-db build supports "
            f"(db={current_version}, supported={SCHEMA_VERSION})."
        )
        logger.error(msg)
        raise RuntimeError(msg)

    if current_version == 0:
        logger.info("Initializing fresh schema v%d", SCHEMA_VERSION)
        await db.executescript(_SCHEMA_DDL)
        await db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        await db.commit()
        logger.info("Schema v%d initialized", SCHEMA_VERSION)
        return

    # Step-wise migration ladder: (target_version, ddl, post_hook), applied in
    # order. Each step advances user_version by one and commits independently, so a
    # mid-sequence failure leaves the DB at the last fully-migrated version. The
    # post_hook (e.g. FTS repopulation) runs after the DDL, before the version bump.
    # Built here rather than at module scope because repopulate_content_index is
    # defined below and resolves at call time.
    migrations: list[tuple[int, str, _PostHook | None]] = [
        (2, _MIGRATION_V1_TO_V2, None),
        (3, _MIGRATION_V2_TO_V3, repopulate_content_index),
        (4, _MIGRATION_V3_TO_V4, None),
        (5, _MIGRATION_V4_TO_V5, None),
        (6, _MIGRATION_V5_TO_V6, None),
        (7, _MIGRATION_V6_TO_V7, None),
        (8, _MIGRATION_V7_TO_V8, None),
        (9, _MIGRATION_V8_TO_V9, None),
        (10, _MIGRATION_V9_TO_V10, None),
        (11, _MIGRATION_V10_TO_V11, reindex_all_activity_fts),
        (12, _MIGRATION_V11_TO_V12, None),
        (13, _MIGRATION_V12_TO_V13, None),
        (14, _MIGRATION_V13_TO_V14, _migrate_shipped_state_to_columns),
        (15, _MIGRATION_V14_TO_V15, None),
        (16, _MIGRATION_V15_TO_V16, None),
        (17, _MIGRATION_V16_TO_V17, None),
        (18, _MIGRATION_V17_TO_V18, None),
        (19, _MIGRATION_V18_TO_V19, None),
        (20, _MIGRATION_V19_TO_V20, None),
        (21, _MIGRATION_V20_TO_V21, _migrate_conflict_aggregation),
        (22, _MIGRATION_V21_TO_V22, None),
        (23, _MIGRATION_V22_TO_V23, None),
    ]
    for target, ddl, post_hook in migrations:
        if current_version >= target:
            continue
        logger.info("Migrating schema v%d → v%d", current_version, target)
        await db.executescript(ddl)
        if post_hook is not None:
            await post_hook(db)
        current_version = target
        await db.execute(f"PRAGMA user_version = {current_version}")
        await db.commit()
        logger.info("Schema migrated to v%d", target)

    if current_version != SCHEMA_VERSION:
        raise RuntimeError(
            f"Migration ladder ended at v{current_version}, expected v{SCHEMA_VERSION}"
        )
    # Snapshot refusals are an additive v1 extension over the v23 core schema.
    # Exact previous merged generation d7272d4 safely ignores and preserves it.
    if not await _table_exists(db, "snapshot_refusals"):
        await db.executescript(_SNAPSHOT_REFUSAL_EXTENSION_DDL)
        await db.commit()
    if not await _table_exists(db, "owner_delegations"):
        await db.executescript(_OWNER_DELEGATION_EXTENSION_DDL)
        await db.commit()
    logger.debug("Schema at v%d", current_version)


async def open_db(db_path: Path) -> aiosqlite.Connection:
    """Open a connection, apply pragmas and schema. Caller must close."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await apply_pragmas(db)
    await ensure_schema(db)
    return db


def get_db(ctx: Any) -> aiosqlite.Connection:
    """Extract the typed DB connection from a FastMCP tool context.

    The MCP SDK types lifespan_context as Unknown; this cast surfaces the real type.
    """
    return cast(aiosqlite.Connection, ctx.request_context.lifespan_context.db)


def content_sha256(content: str) -> str:
    """Return a stable hash for conflict receipts and export-base comparisons."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _bounded_conflict_detail(detail: dict[str, Any] | None) -> str:
    encoded = json.dumps(detail or {}, sort_keys=True, separators=(",", ":"))
    size = len(encoded.encode("utf-8"))
    if size <= config.WRITE_CONFLICT_DETAIL_MAX_BYTES:
        return encoded
    return json.dumps(
        {
            "detail_truncated": True,
            "original_utf8_bytes": size,
            "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _write_conflict_identity(**fields: Any) -> str:
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _upsert_write_conflict(
    db: aiosqlite.Connection,
    *,
    values: tuple[Any, ...],
    identity_hash: str,
    aggregation_state: str,
    enforce_identity_quota: bool,
) -> int | None:
    quota_sql = (
        """
        WHERE EXISTS (
            SELECT 1 FROM write_conflicts WHERE identity_hash = ?
        ) OR (
            SELECT COUNT(*) FROM write_conflicts WHERE identity_hash IS NOT NULL
        ) < ?
        """
        if enforce_identity_quota
        else ""
    )
    quota_params: tuple[Any, ...] = (
        (identity_hash, config.WRITE_CONFLICT_MAX_IDENTITIES)
        if enforce_identity_quota
        else ()
    )
    cursor = await db.execute(
        f"""
        INSERT INTO write_conflicts (
            surface, target_key, operation, attempted_by, principal,
            stale_version, current_version, stale_updated_at, current_updated_at,
            attempted_source_trust, current_source_trust,
            attempted_content_sha256, current_content_sha256,
            reason, detail_json, identity_hash, occurrence_count, last_seen_at,
            aggregation_state
        )
        SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1,
               strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), ?
        {quota_sql}
        ON CONFLICT(identity_hash) WHERE identity_hash IS NOT NULL DO UPDATE SET
            occurrence_count = write_conflicts.occurrence_count + 1,
            last_seen_at = excluded.last_seen_at,
            detail_json = excluded.detail_json
        RETURNING id
        """,  # noqa: S608 — quota_sql is a closed literal
        (*values, identity_hash, aggregation_state, *quota_params),
    )
    row = await cursor.fetchone()
    return int(row["id"]) if row is not None else None


async def record_write_conflict(
    db: aiosqlite.Connection,
    *,
    surface: str,
    target_key: str,
    operation: str,
    reason: str,
    attempted_by: str | None = None,
    principal: str | None = None,
    stale_version: int | None = None,
    current_version: int | None = None,
    stale_updated_at: str | None = None,
    current_updated_at: str | None = None,
    attempted_source_trust: str | None = None,
    current_source_trust: str | None = None,
    attempted_content_sha256: str | None = None,
    current_content_sha256: str | None = None,
    detail: dict[str, Any] | None = None,
) -> int:
    """Stage or aggregate a durable write-conflict receipt."""
    identity_fields = {
        "surface": surface,
        "target_key": target_key,
        "operation": operation,
        "attempted_by": attempted_by,
        "principal": principal,
        "stale_version": stale_version,
        "current_version": current_version,
        "stale_updated_at": stale_updated_at,
        "current_updated_at": current_updated_at,
        "attempted_source_trust": attempted_source_trust,
        "current_source_trust": current_source_trust,
        "attempted_content_sha256": attempted_content_sha256,
        "current_content_sha256": current_content_sha256,
        "reason": reason,
    }
    values = (
        surface,
        target_key,
        operation,
        attempted_by,
        principal,
        stale_version,
        current_version,
        stale_updated_at,
        current_updated_at,
        attempted_source_trust,
        current_source_trust,
        attempted_content_sha256,
        current_content_sha256,
        reason,
        _bounded_conflict_detail(detail),
    )
    identity_hash = _write_conflict_identity(**identity_fields)
    receipt_id = await _upsert_write_conflict(
        db,
        values=values,
        identity_hash=identity_hash,
        aggregation_state="exact_identity",
        enforce_identity_quota=True,
    )
    if receipt_id is not None:
        return receipt_id

    overflow_hash = _write_conflict_identity(
        aggregation_state="capacity_overflow", surface=surface
    )
    overflow_values = (
        surface,
        "__capacity_overflow__",
        "aggregate_write_conflict",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        "distinct_identity_quota_exceeded",
        _bounded_conflict_detail(
            {
                "detail_truncated": True,
                "latest_identity_hash": identity_hash,
                "identity_quota": config.WRITE_CONFLICT_MAX_IDENTITIES,
            }
        ),
    )
    overflow_id = await _upsert_write_conflict(
        db,
        values=overflow_values,
        identity_hash=overflow_hash,
        aggregation_state="capacity_overflow",
        enforce_identity_quota=False,
    )
    if overflow_id is None:
        raise RuntimeError("write_conflicts overflow aggregation failed")
    return overflow_id


async def find_write_conflict(
    db: aiosqlite.Connection,
    *,
    surface: str,
    target_key: str,
    operation: str,
    attempted_by: str | None,
    reason: str,
) -> int | None:
    """Return the id of an existing receipt with this identity, if any.

    The dedupe key for idempotent receipt writes: a retrying caller (the
    normal recovery path after a crashed attempt) converges to exactly one
    receipt instead of growing the ledger on every retry.
    """
    cursor = await db.execute(
        """
        SELECT id FROM write_conflicts
        WHERE surface = ? AND target_key = ? AND operation = ?
          AND attempted_by IS ? AND reason = ?
        ORDER BY id LIMIT 1
        """,
        (surface, target_key, operation, attempted_by, reason),
    )
    row = await cursor.fetchone()
    return int(row["id"]) if row is not None else None


async def record_write_conflict_once(
    db: aiosqlite.Connection,
    *,
    surface: str,
    target_key: str,
    operation: str,
    attempted_by: str | None,
    reason: str,
    principal: str | None = None,
    current_source_trust: str | None = None,
    detail: dict[str, Any] | None = None,
) -> int:
    """Stage or aggregate a retry conflict using the full evidence identity."""
    return await record_write_conflict(
        db,
        surface=surface,
        target_key=target_key,
        operation=operation,
        attempted_by=attempted_by,
        principal=principal,
        reason=reason,
        current_source_trust=current_source_trust,
        detail=detail,
    )


@asynccontextmanager
async def rollback_on_error(db: aiosqlite.Connection) -> AsyncGenerator[None, None]:
    """Roll back the connection's open transaction if the block raises.

    INV-10 armor for staged-write blocks on the shared connection: an
    exception mid-staging (receipt insert, follow-on write) must not leave
    an open transaction behind for an unrelated later caller's commit to
    flush silently. The exception always re-raises.
    """
    try:
        yield
    except Exception:
        try:
            await db.rollback()
        except Exception:
            logger.exception("rollback after staging failure itself failed")
        raise


# ── FTS5 content index helpers ───────────────────────────────────────────────
# Callers are responsible for committing; these helpers only stage writes so
# they can be composed with other writes in the same tool transaction.


def fts_text_for_section(section_name: str, content: str) -> str:
    """Indexable text for a context_sections row."""
    return f"{section_name}\n{content}"


def fts_text_for_activity(
    project_name: str, summary: str, branch: str | None, tags: list[str] | None = None
) -> str:
    """Indexable text for an activity_log row.

    Tags are included so lifecycle markers (SHIPPED, RESEARCH, DECISION, ...) are
    recall-able. Any path that mutates a row's tags MUST re-index via
    reindex_activity_fts to keep content_index in sync.
    """
    parts = [project_name, summary]
    if branch:
        parts.append(branch)
    if tags:
        parts.extend(tags)
    return "\n".join(parts)


def _activity_tags_from_json(raw_tags: str | None) -> list[str]:
    if not raw_tags:
        return []
    parsed = json.loads(raw_tags)
    if not isinstance(parsed, list):
        return []
    return [str(tag) for tag in cast(list[Any], parsed)]


async def reindex_activity_fts(db: aiosqlite.Connection, activity_id: int) -> None:
    """Refresh the content_index row for an activity after its tags or text change."""
    cursor = await db.execute(
        "SELECT project_name, summary, branch, tags FROM activity_log WHERE id = ?",
        (activity_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return
    tags = _activity_tags_from_json(row["tags"])
    await upsert_fts_entry(
        db,
        "activity",
        str(activity_id),
        fts_text_for_activity(row["project_name"], row["summary"], row["branch"], tags),
    )


def fts_text_for_snapshot(data: str) -> str:
    """Indexable text for a system_snapshots row. `data` is the JSON-encoded payload."""
    return data


def fts_text_for_handoff(
    project_name: str,
    project_path: str | None,
    roadmap_file: str | None,
    phase: str | None,
) -> str:
    """Indexable text for a pending_handoffs row."""
    parts = [project_name]
    for p in (project_path, roadmap_file, phase):
        if p:
            parts.append(p)
    return "\n".join(parts)


async def upsert_fts_entry(
    db: aiosqlite.Connection, source_type: str, source_id: str, text: str
) -> None:
    """Delete + insert the content_index row for a given source key."""
    await db.execute(
        "DELETE FROM content_index WHERE source_type = ? AND source_id = ?",
        (source_type, source_id),
    )
    await db.execute(
        "INSERT INTO content_index (source_type, source_id, text) VALUES (?, ?, ?)",
        (source_type, source_id, text),
    )


async def delete_fts_entry(
    db: aiosqlite.Connection, source_type: str, source_id: str
) -> None:
    """Delete the content_index row for a given source key."""
    await db.execute(
        "DELETE FROM content_index WHERE source_type = ? AND source_id = ?",
        (source_type, source_id),
    )


async def gc_fts_orphans(db: aiosqlite.Connection, source_type: str) -> int:
    """Drop content_index rows whose source row no longer exists.

    Used after auto-prune deletes in activity_log and system_snapshots.
    Returns the number of orphan rows removed.
    """
    source_pk = {
        "section": ("context_sections", "section_name"),
        "activity": ("activity_log", "id"),
        "snapshot": ("system_snapshots", "id"),
        "handoff": ("pending_handoffs", "id"),
    }
    if source_type not in source_pk:
        raise ValueError(f"Unknown source_type for GC: {source_type}")
    table, pk = source_pk[source_type]
    cursor = await db.execute(
        f"""
        DELETE FROM content_index
        WHERE source_type = ?
          AND source_id NOT IN (SELECT CAST({pk} AS TEXT) FROM {table})
        """,  # noqa: S608 — table/pk come from a closed literal map
        (source_type,),
    )
    return cursor.rowcount or 0


class InsertActivityResult(NamedTuple):
    """Result of insert_activity_row: new row id + rows removed by retention."""

    activity_id: int
    pruned_rows: list[tuple[int, str]]  # (id, raw tags JSON) of pruned rows


class ActivityProtectedQuotaExceeded(RuntimeError):
    """A protected activity insert would exceed its per-source hard quota."""


def protected_tags_predicate(column: str = "tags") -> tuple[str, list[str]]:
    """SQL fragment matching rows whose tags include a retention-protected tag.

    Case-insensitive by BD-INV-1: a lowercase 'ledger' from any writer must
    still protect the row (every other tag matcher in this codebase is
    exact-case; this one deliberately is not).
    """
    tags = sorted(config.LEDGER_PROTECTED_TAGS)
    placeholders = ", ".join("?" for _ in tags)
    sql = f"EXISTS (SELECT 1 FROM json_each({column}) WHERE upper(value) IN ({placeholders}))"
    return sql, [t.upper() for t in tags]


def canonicalize_protected_tags(tags: list[str] | None) -> list[str] | None:
    """Fold any casing of a retention-protected lifecycle tag to its registered
    uppercase form (``config.LEDGER_PROTECTED_TAGS``); leave free-form tags as-is.

    Retention matches these case-insensitively (see ``protected_tags_predicate``),
    but the shipped-sync feed, health nags, and ``record_disposition`` all match
    exact-case ``'SHIPPED'``/``'LEDGER'``. Canonicalizing on write keeps every
    reader in agreement, so a lowercase ``shipped`` can never become a row that is
    retention-protected yet silently never synced and never nagged.
    """
    if not tags:
        return tags
    protected = {t.upper() for t in config.LEDGER_PROTECTED_TAGS}
    return [tag.upper() if tag.upper() in protected else tag for tag in tags]


async def insert_activity_row(
    db: aiosqlite.Connection,
    *,
    source: str,
    timestamp: str,
    project_name: str,
    summary: str,
    branch: str | None = None,
    tags: list[str] | None = None,
    retention_limit: int | None = None,
    protected_quota: int | None = None,
    canonical_key: str | None = None,
    source_trust: str = "agent",
) -> InsertActivityResult:
    """Insert an activity row, keep the FTS mirror in sync, and apply protected-aware retention."""
    # Canonicalize retention-protected lifecycle tags at this single write choke
    # point so every reader agrees on one form. Retention matches SHIPPED/LEDGER
    # case-insensitively, but the shipped-sync feed, health nags, and
    # record_disposition match exact-case; a stored lowercase 'shipped' would be
    # retention-protected yet never synced and never nagged. Reassigning `tags`
    # here also feeds the canonical form to the FTS mirror below.
    tags = canonicalize_protected_tags(tags)
    raw_tags = json.dumps(tags or [])
    is_protected = bool(
        {tag.upper() for tag in tags or []} & config.LEDGER_PROTECTED_TAGS
    )
    if is_protected and protected_quota is not None:
        protected_sql, protected_params = protected_tags_predicate()
        cursor = await db.execute(
            f"""
            INSERT INTO activity_log
                (source, timestamp, project_name, summary, branch, tags, canonical_key, source_trust)
            SELECT ?, ?, ?, ?, ?, ?, ?, ?
            WHERE (
                SELECT COUNT(*) FROM activity_log
                WHERE source = ? AND {protected_sql}
            ) < ?
            RETURNING id
            """,  # noqa: S608 — predicate assembled from a closed literal set
            (
                source,
                timestamp,
                project_name,
                summary,
                branch,
                raw_tags,
                canonical_key,
                source_trust,
                source,
                *protected_params,
                protected_quota,
            ),
        )
        inserted = await cursor.fetchone()
        if inserted is None:
            raise ActivityProtectedQuotaExceeded(source)
        activity_id = int(inserted["id"])
    else:
        cursor = await db.execute(
            """
            INSERT INTO activity_log
                (source, timestamp, project_name, summary, branch, tags, canonical_key, source_trust)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source,
                timestamp,
                project_name,
                summary,
                branch,
                raw_tags,
                canonical_key,
                source_trust,
            ),
        )
        activity_id = cursor.lastrowid
        if activity_id is None:
            raise RuntimeError("activity_log insert did not return an id")

    await upsert_fts_entry(
        db,
        "activity",
        str(activity_id),
        fts_text_for_activity(project_name, summary, branch, tags),
    )

    pruned_rows: list[tuple[int, str]] = []
    if retention_limit is not None:
        protected_sql, protected_params = protected_tags_predicate()
        cursor = await db.execute(
            f"""
            DELETE FROM activity_log
            WHERE source = ? AND id NOT IN (
                SELECT id FROM activity_log WHERE source = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
            )
            AND NOT {protected_sql}
            RETURNING id, tags
            """,  # noqa: S608 — predicate assembled from a closed literal set
            (source, source, retention_limit, *protected_params),
        )
        deleted = await cursor.fetchall()
        if deleted:
            pruned_rows = [(row["id"], row["tags"]) for row in deleted]
            await gc_fts_orphans(db, "activity")

    return InsertActivityResult(activity_id=int(activity_id), pruned_rows=pruned_rows)


_FTS_SOURCE_TABLES = {
    "section": ("context_sections", "section_name"),
    "activity": ("activity_log", "id"),
    "snapshot": ("system_snapshots", "id"),
    "handoff": ("pending_handoffs", "id"),
}


async def _expected_fts_texts(
    db: aiosqlite.Connection, source_type: str
) -> dict[str, str | None]:
    """Recompute canonical FTS text for one closed source-table class."""
    if source_type == "section":
        cursor = await db.execute(
            "SELECT section_name, content FROM context_sections"
        )
        return {
            str(row["section_name"]): fts_text_for_section(
                row["section_name"], row["content"]
            )
            for row in await cursor.fetchall()
        }
    if source_type == "activity":
        cursor = await db.execute(
            "SELECT id, project_name, summary, branch, tags FROM activity_log"
        )
        expected: dict[str, str | None] = {}
        for row in await cursor.fetchall():
            try:
                tags = _activity_tags_from_json(row["tags"])
            except (json.JSONDecodeError, TypeError):
                expected[str(row["id"])] = None
                continue
            expected[str(row["id"])] = fts_text_for_activity(
                row["project_name"], row["summary"], row["branch"], tags
            )
        return expected
    if source_type == "snapshot":
        cursor = await db.execute("SELECT id, data FROM system_snapshots")
        return {
            str(row["id"]): fts_text_for_snapshot(row["data"])
            for row in await cursor.fetchall()
        }
    if source_type == "handoff":
        cursor = await db.execute(
            "SELECT id, project_name, project_path, roadmap_file, phase "
            "FROM pending_handoffs"
        )
        return {
            str(row["id"]): fts_text_for_handoff(
                row["project_name"],
                row["project_path"],
                row["roadmap_file"],
                row["phase"],
            )
            for row in await cursor.fetchall()
        }
    raise ValueError(f"unsupported FTS source type: {source_type}")


async def collect_fts_index_metrics(db: aiosqlite.Connection) -> dict[str, Any]:
    """Return exact source-vs-FTS identity and content consistency metrics."""
    sources: dict[str, dict[str, int | bool]] = {}
    total_expected = 0
    total_indexed = 0
    total_missing = 0
    total_orphaned = 0
    total_content_mismatched = 0

    for source_type, (table, pk) in _FTS_SOURCE_TABLES.items():
        cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
        expected_row = await cursor.fetchone()
        expected = expected_row[0] if expected_row else 0

        cursor = await db.execute(
            "SELECT COUNT(*) FROM content_index WHERE source_type = ?", (source_type,)
        )
        indexed_row = await cursor.fetchone()
        indexed = indexed_row[0] if indexed_row else 0

        cursor = await db.execute(
            f"""
            SELECT COUNT(*)
            FROM {table} AS source
            WHERE NOT EXISTS (
                SELECT 1 FROM content_index AS idx
                WHERE idx.source_type = ?
                  AND idx.source_id = CAST(source.{pk} AS TEXT)
            )
            """,  # noqa: S608 — table/pk come from a closed literal map
            (source_type,),
        )
        missing_row = await cursor.fetchone()
        missing = missing_row[0] if missing_row else 0

        cursor = await db.execute(
            f"""
            SELECT COUNT(*)
            FROM content_index AS idx
            WHERE idx.source_type = ?
              AND NOT EXISTS (
                  SELECT 1 FROM {table} AS source
                  WHERE CAST(source.{pk} AS TEXT) = idx.source_id
              )
            """,  # noqa: S608 — table/pk come from a closed literal map
            (source_type,),
        )
        orphaned_row = await cursor.fetchone()
        orphaned = orphaned_row[0] if orphaned_row else 0

        expected_texts = await _expected_fts_texts(db, source_type)
        cursor = await db.execute(
            "SELECT source_id, text FROM content_index WHERE source_type = ?",
            (source_type,),
        )
        content_mismatched = sum(
            1
            for row in await cursor.fetchall()
            if row["source_id"] in expected_texts
            and row["text"] != expected_texts[row["source_id"]]
        )

        ok = (
            expected == indexed
            and missing == 0
            and orphaned == 0
            and content_mismatched == 0
        )
        sources[source_type] = {
            "expected": expected,
            "indexed": indexed,
            "missing": missing,
            "orphaned": orphaned,
            "content_mismatched": content_mismatched,
            "ok": ok,
        }

        total_expected += expected
        total_indexed += indexed
        total_missing += missing
        total_orphaned += orphaned
        total_content_mismatched += content_mismatched

    return {
        "ok": all(source["ok"] for source in sources.values()),
        "expected": total_expected,
        "indexed": total_indexed,
        "missing": total_missing,
        "orphaned": total_orphaned,
        "content_mismatched": total_content_mismatched,
        "sources": sources,
    }


async def reindex_all_activity_fts(db: aiosqlite.Connection) -> None:
    """Rebuild content_index activity rows so their tags are searchable (v11 hook).

    The B3 change made fts_text_for_activity append tags; section/snapshot/handoff
    text is unchanged, so only activity rows need re-indexing, not the whole index.
    A DB already at v10 keeps tag-less activity rows until this runs.
    """
    await db.execute("DELETE FROM content_index WHERE source_type = 'activity'")
    cursor = await db.execute(
        "SELECT id, project_name, summary, branch, tags FROM activity_log"
    )
    for row in await cursor.fetchall():
        row_tags = _activity_tags_from_json(row["tags"])
        await db.execute(
            "INSERT INTO content_index (source_type, source_id, text) VALUES (?, ?, ?)",
            (
                "activity",
                str(row["id"]),
                fts_text_for_activity(
                    row["project_name"], row["summary"], row["branch"], row_tags
                ),
            ),
        )


async def repopulate_content_index(db: aiosqlite.Connection) -> dict[str, int]:
    """Rebuild content_index from all source tables. Idempotent — clears first."""
    await db.execute("DELETE FROM content_index")

    counts = {"section": 0, "activity": 0, "snapshot": 0, "handoff": 0}

    cursor = await db.execute("SELECT section_name, content FROM context_sections")
    for row in await cursor.fetchall():
        await db.execute(
            "INSERT INTO content_index (source_type, source_id, text) VALUES (?, ?, ?)",
            (
                "section",
                row["section_name"],
                fts_text_for_section(row["section_name"], row["content"]),
            ),
        )
        counts["section"] += 1

    cursor = await db.execute(
        "SELECT id, project_name, summary, branch, tags FROM activity_log"
    )
    for row in await cursor.fetchall():
        row_tags = _activity_tags_from_json(row["tags"])
        await db.execute(
            "INSERT INTO content_index (source_type, source_id, text) VALUES (?, ?, ?)",
            (
                "activity",
                str(row["id"]),
                fts_text_for_activity(
                    row["project_name"], row["summary"], row["branch"], row_tags
                ),
            ),
        )
        counts["activity"] += 1

    cursor = await db.execute("SELECT id, data FROM system_snapshots")
    for row in await cursor.fetchall():
        await db.execute(
            "INSERT INTO content_index (source_type, source_id, text) VALUES (?, ?, ?)",
            ("snapshot", str(row["id"]), fts_text_for_snapshot(row["data"])),
        )
        counts["snapshot"] += 1

    cursor = await db.execute(
        "SELECT id, project_name, project_path, roadmap_file, phase FROM pending_handoffs"
    )
    for row in await cursor.fetchall():
        await db.execute(
            "INSERT INTO content_index (source_type, source_id, text) VALUES (?, ?, ?)",
            (
                "handoff",
                str(row["id"]),
                fts_text_for_handoff(
                    row["project_name"],
                    row["project_path"],
                    row["roadmap_file"],
                    row["phase"],
                ),
            ),
        )
        counts["handoff"] += 1

    await db.commit()
    logger.info("content_index repopulated: %s", counts)
    return counts
