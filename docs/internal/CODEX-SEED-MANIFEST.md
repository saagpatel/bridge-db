# Codex Seed Manifest Fingerprints

The private `python -m bridge_db.codex_seed` entrypoint recognizes two
fingerprint versions. Verification is versioned and unknown versions fail
closed.

| Manifest form | Compatibility state | Signed content | Apply behavior |
| --- | --- | --- | --- |
| no `fingerprint_version` | `legacy_implicit_v1` | `snapshot_payload` | accepted only before 2026-08-18T00:00:00Z |
| `"fingerprint_version": "snapshot-v1"` | `legacy_explicit_v1` | `snapshot_payload` | accepted only before 2026-08-18T00:00:00Z |
| `"fingerprint_version": "manifest-v2"` | `current_v2` | version, snapshot date, snapshot payload, and baseline activity | accepted |
| any other value | none | none | rejected before the database opens |

Every dry-run, apply, and conflict response includes
`fingerprint_compatibility` with the accepted version, state, covered fields,
and whether an upgrade is required. Omission therefore has an explicit legacy
meaning instead of silently selecting an algorithm.

New manifests should use `manifest-v2` and compute `fingerprint` with
`bridge_db.codex_seed.fingerprint_manifest_v2`. The digest uses stable,
key-sorted compact JSON over:

```json
{
  "fingerprint_version": "manifest-v2",
  "snapshot_date": "<date>",
  "snapshot_payload": {},
  "baseline_activity": {}
}
```

Legacy v1 responses include `sunset_at`. At and after
`2026-08-18T00:00:00Z`, validation rejects v1 before the database opens.
Unknown versions always fail closed. Regenerate existing private manifests as
`manifest-v2` before the cutoff.

## Snapshot admission

Apply uses the repository's shared snapshot admission service. It never issues
its own snapshot deletion. If the target Codex family is full, apply returns
`ok=false`, `snapshot_write="refused_capacity"`, the durable `refusal_id`, and
the required `next_state`; the paired baseline activity stays unwritten. The
Codex owner must handle and acknowledge that exact refusal before retrying.
