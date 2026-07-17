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
| Recall telemetry | `recall` | `recall_query_log.jsonl` plus immutable segments | Preserve all pending policy; new records contain no raw query | Locked, fsync'd, atomic rename at configured byte boundary | Telemetry failure does not break read-only recall; failure is logged to stderr | `recall_stats` scans a bounded horizon across active + segments | `health.evidence_lifecycle.recall`, including bounded legacy raw-query inventory | Legacy-query compaction, segment deletion, and external-copy cleanup require approval |
| Audit events | `log_audit` callers | `audit.jsonl` plus immutable segments | Preserve all pending policy | Locked, fsync'd, atomic rename at configured byte boundary | Primary failure writes independent minimized failure receipt and continues degraded; dual failure raises | `audit_tail` scans a bounded horizon across active + segments | `health/status/dogfood` expose bytes, segments, and degradation | Segment deletion or rewrite requires approval |
| Audit failure receipts | `log_audit` fallback | `audit_failures.jsonl` plus immutable segments | Preserve until an operator-defined acknowledgement/reconciliation policy exists | Same lossless rotation | Failure to persist this receipt raises; no silent success | Receipt contains event digest and non-sensitive attribution, not original detail | Any receipt makes storage health non-green | Acknowledge/archive/delete policy requires approval |
| Migration backups | Destructive schema migration hook | Verified `.bak`, `.sha256`, and `.meta.json` siblings | Operator acknowledgement required | One immutable backup per migration label | Missing/bad manifest, digest, SQLite integrity, or schema evidence blocks destructive migration/reuse | Online SQLite backup includes committed WAL state; digest + integrity + schema readback verified | `health.evidence_lifecycle.migration_backups` inventories every sibling | Backup/manifest/metadata deletion or external archival requires approval |

## Retention boundary

Rotation is not deletion. It bounds the active append target and keeps readers
within explicit byte horizons, while total retained bytes can still grow until
the operator approves a retention or archival policy. Health reports
`retention_policy="preserve_all_pending_approval"` and
`destructive_cleanup="approval_required"` so bounded reads cannot be mistaken
for bounded historical storage.

## Operator policy workflow

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
