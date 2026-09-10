# bridge-db Steady-State Proof Package

Status: passed.

This package is the operational-sync exemplar for `proof-package.v1`. It proves
that bridge-db health and shipped-event sync receipts were clean at the checked
read-only status point.

Key proof points:

- Overall bridge health was healthy.
- `unprocessed_shipped=0`.
- `processed_shipped_without_receipt=0`.
- `fts_missing=0` and `fts_orphaned=0`.
- Dogfood surfaced recent audit evidence and the latest `confirm_shipped_sync`
  detail.

Future bridge-sync work should refresh this pattern with new status and dogfood
receipts before claiming the sync lane is done.
