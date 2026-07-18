# Durable Evidence Lifecycle

bridge-db keeps local evidence recoverable while bounding active-file behavior.
This contract is intentionally non-destructive: rotation preserves every
segment, and no historical telemetry, audit record, migration backup, manifest,
or recovery artifact is deleted automatically.

## Failure-semantics decision

| Design | Strength | Incompatibility / gap | Decision |
|---|---|---|---|
| Transactional SQLite audit/outbox | Canonical mutation and audit event can commit atomically; projection backlog is queryable. | Requires a schema migration and conversion of every pre-decision, refusal, CLI, and post-commit call site. | Strongest long-term option; deferred as a separately approved architecture migration. |
| Fail-closed JSONL | Simple for decisions made before mutation. | Unsafe as a blanket rule: many current success audits run after SQLite commit, so an append error could report failure after state changed. | Rejected as the bundle-wide default. |
| Durable degraded continuation | Preserves availability when the primary JSONL target alone fails and makes degradation operator-visible. | Needs an independent failure-evidence path; cannot claim success if both paths fail. | Implemented. Primary failure writes a minimized durable receipt; health becomes degraded. If the receipt also fails, `AuditUnavailableError` is raised. |

## Lifecycle matrix

| Evidence | Producer | Storage | Retention | Rotation | Failure behavior | Recovery / readback | Operator visibility | Destructive action |
|---|---|---|---|---|---|---|---|---|
| Recall telemetry | `recall` | `recall_query_log.jsonl` plus immutable segments | Review-only; new records contain no raw query; historical query text may be redacted only from an exact verified archive snapshot | Locked, fsync'd, atomic rename at configured byte boundary | Telemetry failure does not break read-only recall; failure is logged to stderr. Redaction writes a durable prepared receipt before publication; an incomplete transaction degrades health | `recall_stats` scans a bounded horizon across active + segments. Redaction readback proves record count, digest, and zero remaining raw-query fields | `health.evidence_lifecycle.recall` plus `dispositions`, including bounded legacy raw-query and open-transaction inventory | Record/segment deletion, archive deletion, automatic retention, and external-copy cleanup require separate approval |
| Audit events | `log_audit` callers | `audit.jsonl` plus immutable segments | Preserve all pending policy | Locked, fsync'd, atomic rename at configured byte boundary | Primary failure writes independent minimized failure receipt and continues degraded; dual failure raises | `audit_tail` scans a bounded horizon across active + segments | `health/status/dogfood` expose bytes, segments, and degradation | Segment deletion or rewrite requires approval |
| Audit failure receipts | `log_audit` fallback | `audit_failures.jsonl` plus immutable segments | Preserve until an operator-defined acknowledgement/reconciliation policy exists | Same lossless rotation | Failure to persist this receipt raises; no silent success | Receipt contains event digest and non-sensitive attribution, not original detail | Any receipt makes storage health non-green | Acknowledge/archive/delete policy requires approval |
| Current recovery anchor | Explicit `--create-recovery-anchor` command | Private `bridge.db.recovery-anchor-v1/` bundle with `anchor.sqlite` and `manifest.json` | Preserve pending operator approval; existing bundle is never overwritten | One atomic directory publication | Staging or publication failure leaves no partial current bundle; invalid or incomplete published state fails closed | SQLite online backup, SHA-256 and byte binding, schema/integrity checks, and bounded table-count readback against a disposable copy | `health/status/dogfood` expose `current_recovery_anchor` independently from legacy provenance | Replacement or deletion requires separate approval |
| Migration backups | Destructive schema migration hook | Verified `.bak`, `.sha256`, and `.meta.json` siblings; pre-verification backups remain `legacy-unverified` | Review-only; preserve all | One immutable backup per migration label | Missing/bad manifest, digest, SQLite integrity, or schema evidence blocks destructive migration/reuse | Online SQLite backup includes committed WAL state; digest + integrity + schema readback verified. Missing historical provenance is never reconstructed retroactively | `health.evidence_lifecycle.migration_backups` inventories every sibling and its verification state | Backup/manifest/metadata deletion requires separate approval |
| Evidence disposition receipts | Approved lifecycle operation | `evidence_dispositions.jsonl` plus immutable segments | Preserve all pending policy | Same lossless rotation | `prepared` is durable before source publication; `completed` follows readback; pre-publish failure appends `aborted`; post-publish interruption leaves an open `prepared` receipt | Latest state is reconstructed across active + rotated files | Any open or malformed transaction makes storage health non-green | Receipt deletion, reconciliation rewrite, or automatic expiry requires separate approval |

## Retention boundary

Rotation is not deletion. It bounds the active append target and keeps readers
within explicit byte horizons, while total retained bytes can still grow until
the operator approves a retention or archival policy. Health reports
`retention_policy="preserve_all_pending_approval"` and
`destructive_cleanup="approval_required"` so bounded reads cannot be mistaken
for bounded historical storage.

## Operator policy workflow

Create and verify the current recovery anchor explicitly:

```console
python -m bridge_db --create-recovery-anchor
python -m bridge_db --verify-recovery-anchor
```

The first command is idempotent only in the preservation sense: if a bundle
already exists, it verifies and preserves it instead of overwriting it. Health
reports `current_recovery_anchor.state` as:

- `verified` when the bundle passes recovery verification and its semantic
  fingerprint still matches the live database;
- `stale` when the bundle remains internally valid but the live database has
  changed since it was created. This is a freshness mismatch, not evidence of
  bundle corruption, and current recovery readiness remains false until an
  operator separately approves replacement;
- `missing` when no current bundle exists; or
- `invalid` when the bundle or live-source comparison cannot be verified.

The standalone `--verify-recovery-anchor` command proves bundle integrity
without requiring a readable live database; `health`, `status`, and `dogfood`
add the live-source freshness comparison.
It reports historical migration-backup provenance separately as `verified`,
`readable_but_unknown`, or `mixed_or_unreadable`. A verified current anchor can
establish present recovery readiness, but it does not change any legacy file or
claim creation-time provenance that was never recorded.

`python -m bridge_db.evidence_policy plan` emits a read-only,
content-bound `EvidenceLifecyclePlanV1`. It inventories evidence by path, byte
size, and SHA-256 without returning query or audit contents. The snapshot digest
changes if any artifact changes.

An operator can create a verified copy with:

```console
python -m bridge_db.evidence_policy archive \
  --destination /protected/archive/bridge-db-evidence \
  --expected-snapshot-sha256 <digest-from-plan>
```

The archive is written through a private temporary directory, fsync'd,
atomically published, and read-verified. A stale digest fails closed. Source
evidence is never removed or rewritten, and the manifest explicitly carries
`destructive_authority=false`. Archives can contain historical sensitive
telemetry and require equivalent or stronger access controls than the source.
Retain the plan digest independently and use it for later recovery checks:

```console
python -m bridge_db.evidence_policy verify \
  --archive /protected/archive/bridge-db-evidence \
  --expected-snapshot-sha256 <independently-retained-plan-digest>
```

The adjacent manifest checksum detects corruption; the independently retained
plan digest prevents a replacement manifest and checksum from becoming
self-authenticating.

Plan review can be recorded without granting cleanup authority:

```console
python -m bridge_db.evidence_policy acknowledge \
  --expected-snapshot-sha256 <digest-from-plan> \
  --actor <operator> \
  --reason <review-reason>
```

Acknowledgements are append-only and content-bound. They do not clear audit
degradation, authorize historical-query rewriting, or permit deletion. Numeric
retention horizons and every destructive disposition remain explicit operator
decisions. Actor and reason fields reuse the repository's established 4 KiB and
64 KiB UTF-8 write budgets so an acknowledgement cannot become an unbounded
single record.

## Approved archive-bound raw-query redaction

After an operator explicitly approves archive-and-redact for an exact snapshot,
first run the non-mutating form:

```console
python -m bridge_db.evidence_policy redact-legacy-recall \
  --archive /protected/archive/bridge-db-evidence \
  --expected-snapshot-sha256 <independently-retained-plan-digest> \
  --expected-raw-query-records <approved-count> \
  --actor <operator> \
  --reason <approval-reason> \
  --dry-run
```

Only if that returns `status=would_redact` with the approved count may the same
command use `--apply`. The operation locks the recall family, verifies that
every source file still byte-matches the protected archive, preserves every
JSONL record, derives `query_empty` before removing the legacy `query` field,
and adds `query_text_redacted=true`. It atomically replaces each file, fsyncs
the directory, and reads the result back before recording completion.

This authority is narrow. It does not permit deleting telemetry records,
rotated segments, migration backups, archive generations, disposition receipts,
or recovery evidence; it does not enable automatic retention; and it says
nothing about unknown external copies.
