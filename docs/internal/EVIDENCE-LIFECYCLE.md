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
| Current recovery anchor | Explicit `--create-recovery-anchor` or separately approved `--rotate-recovery-anchor` command | Private `bridge.db.recovery-anchor-v1/` bundle with `anchor.sqlite` and `manifest.json`; rotations retain the prior bundle under a timestamped `.superseded-*` sibling | Preserve pending operator approval; creation never overwrites and rotation never deletes the prior bundle | One atomic directory publication for creation; one atomic exchange places the new bundle at the current path and the prior bundle at its final superseded path | Staging failure leaves the current bundle untouched; post-exchange verification failure swaps the directories back; invalid or incomplete evidence fails closed | SQLite online backup, SHA-256 and byte binding, schema/integrity checks, bounded table-count readback against a disposable copy, and live-source fingerprint comparison under SQLite's writer slot | `health/status/dogfood` expose `current_recovery_anchor` independently from legacy provenance; rotation CLI returns both current and superseded digests | Rotation requires separate approval; deletion of current or superseded evidence requires another separate approval |
| Recovery batch seals | Scoped `cc` or `codex` `--seal-recovery-batch <batch-id>` after an authorized completed write batch | Private `bridge.db.recovery-seals-v1/<sha256(batch-id)>/` attempt plus at most one terminal `RecoverySealReceiptV1`; distinct retained batches are capped at 1024 | Preserve all pending policy; attempts and receipts are never rewritten or deleted; exact replay remains allowed at capacity | Inter-process seal lock serializes batches; immutable files publish by no-replace link and directory fsync; inventory validates existing directories only | An ordinary failure writes `recovery_unsealed`; a hard interruption leaves an open attempt that health classifies as unsealed; a success-receipt publication failure removes the unpublished terminal when possible and rolls the anchor exchange back | Terminal success binds owner, batch ID, source fingerprint, anchor digest, integrity, semantic readback, and source-current proof while SQLite's writer slot remains held; stale sealed replays fail closed | `health/status` expose receipt counts, latest state, strict `recovery_lifecycle_ready`, stale replay, missing/invalid directory, and capacity-exceeded states without creating or repairing evidence | Scope activation and workflow wiring require separate owner action; receipt or attempt deletion requires separate approval |
| Migration backups | Destructive schema migration hook | Verified `.bak`, `.sha256`, and `.meta.json` siblings; pre-verification backups remain `legacy-unverified`; retained `.bak-wal` and `.bak-shm` companions remain separate evidence | Review-only; preserve all | One immutable backup per migration label | Missing/bad manifest, digest, SQLite integrity, or schema evidence blocks destructive migration/reuse. Retained companions without a live primary are surfaced as warnings without weakening a verified current anchor | Online SQLite backup includes committed WAL state; digest + integrity + schema readback verified. Missing historical provenance is never reconstructed retroactively | `health.evidence_lifecycle.migration_backups` inventories every live primary, retained companion, and distinct missing primary; `status` and `dogfood` summarize companion state | Backup/manifest/metadata/companion deletion requires separate approval |
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

After a separately approved BridgeDB write makes a valid anchor stale, rotate
it without deleting or overwriting recovery evidence:

```console
python -m bridge_db --rotate-recovery-anchor
```

If the current anchor is already verified and current, rotation is
preservation-idempotent and reports `preserved_current`. For a stale but
internally verified anchor, the command stages and verifies a new bundle,
acquires SQLite's writer slot, atomically exchanges the current and staged
directories so the prior bundle lands directly under a collision-resistant
`bridge.db.recovery-anchor-v1.superseded-<timestamp>-<digest>` sibling. If
source state changes, the current evidence changes, the atomic exchange fails,
or post-exchange verification cannot complete, the command fails closed. A
post-exchange verification failure swaps the directories back before removing
only the unpublished candidate. Invalid current evidence is never rotated.

For an owned write workflow, prefer the terminal batch protocol after its last
authorized write:

```console
python -m bridge_db --seal-recovery-batch <batch-id>
```

This command requires a current `cc` or `codex` channel token whose v2 grant
contains `seal_recovery_batch`. It derives the owner from that credential and
does not accept an owner claim. A successful terminal receipt is published
while the SQLite writer guard still blocks later commits. The stricter
`health.evidence_lifecycle.recovery_lifecycle_ready` becomes true only when the
latest success receipt matches the exact current anchor and live source. Stale
success receipts are preserved as evidence but are not replayed as current
success, and read-side inventory never creates the receipt root.
Standalone create/rotate commands still prove physical anchor readiness but do
not retroactively claim that a write batch followed the owned lifecycle.

See [`RECOVERY-SEAL-PROTOCOL.md`](RECOVERY-SEAL-PROTOCOL.md) for interruption,
repeat, concurrency, authorization, and activation semantics.

It reports historical migration-backup provenance separately as `verified`,
`readable_but_unknown`, or `mixed_or_unreadable`. A verified current anchor can
establish present recovery readiness, but it does not change any legacy file or
claim creation-time provenance that was never recorded.

Retained `.bak-wal` and `.bak-shm` companions are content-bound in evidence
plans even when their live `.bak` primary is absent. Health reports companion
and distinct missing-primary counts as preservation warnings; those warnings do
not become proof of deletion, authorize cleanup, or make a current verified
RecoveryAnchorV1 unusable.

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
