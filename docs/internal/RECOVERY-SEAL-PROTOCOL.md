# Recovery Seal Protocol

`RecoveryAnchorV1` deliberately becomes stale after any persisted BridgeDB
change. `RecoverySealReceiptV1` is the owned lifecycle protocol for ending an
authorized, completed write batch without weakening that freshness check.

## Authority boundary

Only a current channel credential for `cc` or `codex` with the
`seal_recovery_batch` scope may run the protocol:

```console
python -m bridge_db --seal-recovery-batch <batch-id>
```

The seal owner is derived from `BRIDGE_DB_PRINCIPAL_TOKEN`; it is never accepted
as a CLI claim. The global auth rollout mode does not weaken this check.
Existing v2 grants do not gain the new scope automatically. An operator must
separately re-enroll the intended local principal and update its client secret
before activation.

A seal receipt proves who sealed the complete current database image. It does
not claim that the sealer authored every row, and it grants no authority to
write another principal's section or snapshot. In particular, Codex still
cannot refresh a CC snapshot and CC still cannot refresh a Codex snapshot.

Batch IDs are opaque, caller-supplied lifecycle identifiers matching
`[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}`. Reuse the exact ID only to read back or
resume that exact batch attempt. Use a new ID for a later write batch.
Distinct retained batch directories are bounded to 1024. Exact-ID replay stays
available at capacity; a new distinct ID fails closed with
`recovery_seal_capacity_exceeded` before a new directory is created.

## State machine

Each batch has one private, content-bound attempt and at most one private,
content-bound terminal receipt under
`bridge.db.recovery-seals-v1/<sha256(batch-id)>/`:

1. The sealer takes the inter-process recovery-seal lock.
2. It records one immutable `RecoverySealAttemptV1` containing the bound owner,
   batch ID, and complete live-source semantic fingerprint.
3. It rotates or preservation-reuses the current anchor.
4. While SQLite's writer slot is still held, it rechecks the source fingerprint,
   digest, SQLite integrity, bounded semantic readback, and source-current
   status, then publishes one immutable `RecoverySealReceiptV1`.
5. The writer slot is released only after the success receipt is durable. If
   receipt publication fails after an anchor exchange, the unpublished success
   receipt is removed when possible and the exchange rolls back.

Terminal outcomes are exact:

- `recovery_sealed` with reason `verified_current_anchor` means the receipt,
  anchor digest, integrity check, semantic readback, and live-source fingerprint
  all agree.
- `recovery_unsealed` carries a stable failure reason and never claims recovery
  readiness.

A hard interruption can leave only the immutable attempt. Health reports that
as `recovery_unsealed` with `seal_attempt_incomplete`; reads never repair or
auto-seal it. A retry of the same batch ID may resume only while the source
fingerprint is unchanged. If the source advanced, the retry writes a terminal
`recovery_unsealed` receipt instead of sealing a different batch under the old
identity.

Repeated and concurrent calls for one batch serialize and replay the one
terminal receipt. A sealed terminal is replayed only if it still matches the
current anchor and live source; stale historical success fails closed instead
of being returned as current readiness. A different owner cannot reclaim an
existing batch ID.

## Readiness and preservation

`health` and `status` keep physical anchor readiness and lifecycle proof
separate:

- `current_recovery_ready` / `current_recovery_anchor_ready` continue to mean
  that the current anchor is internally valid and source-current.
- `recovery_lifecycle_ready` additionally requires the latest terminal
  `RecoverySealReceiptV1` to match that exact anchor and live-source
  fingerprint.
- `recovery_seals.state` is `missing`, `verified`, `recovery_unsealed`, `stale`,
  or `invalid`.
- Recovery-seal inventory validates an existing private receipt directory but
  never creates or repairs it. Missing, raced, oversized, or malformed receipt
  state is reported fail-closed through `recovery_seals`, not healed by reads.

Recovery-dependent batch workflows should require
`recovery_lifecycle_ready=true`. A manual create or rotation can still prove the
narrow physical anchor property, but it is not evidence that an authorized
write batch followed this lifecycle protocol.

The protocol never deletes or overwrites attempts, terminal receipts, current
anchors, or superseded anchors. Rotation keeps the prior bundle under its
existing `.superseded-*` path. Cleanup remains separately approval-gated.

## Activation boundary

The repository supplies the protocol; it does not silently attach it to every
writer. A workflow that owns a multi-write operation must call the seal command
exactly once after its final authorized write. The 30-minute checkpoint
LaunchAgent remains WAL-only and is not a recovery sealer. Reads, health checks,
session startup, and ordinary MCP inspection never rotate or seal.

Natural lifecycle readiness is not proven until an authorized owner has a
refreshed scoped grant, the owning workflow invokes the command, and live
`health` readback reports both the current anchor and recovery lifecycle ready.
