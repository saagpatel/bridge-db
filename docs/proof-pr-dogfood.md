# proof-pr Dogfood

This repo is a low-risk dogfood target for `proof-pr` because bridge-db already
has proof-package habits, rollback notes, and a strong local verifier.

For PRs that need a review receipt, install the public CLI tag in a temporary
environment and render the proof block from the generated receipt:

```bash
python3 -m venv /tmp/proof-pr-dogfood-venv
/tmp/proof-pr-dogfood-venv/bin/python -m pip install \
  git+https://github.com/saagpatel/proof-pr.git@v0.2.7
/tmp/proof-pr-dogfood-venv/bin/proof-pr init \
  --cwd . \
  --tier T1 \
  --summary "Short PR summary" \
  --output /tmp/proof-pr-dogfood.json
/tmp/proof-pr-dogfood-venv/bin/proof-pr collect \
  /tmp/proof-pr-dogfood.json \
  --cwd .
/tmp/proof-pr-dogfood-venv/bin/proof-pr render \
  /tmp/proof-pr-dogfood.json
/tmp/proof-pr-dogfood-venv/bin/proof-pr receipt-hygiene \
  /tmp/proof-pr-dogfood.json \
  --explain
```

Use the rendered block in the PR body and keep the JSON receipt as local review
evidence. The receipt is not supply-chain provenance; release/build tiers should
link separate attestations or artifact digests when those become relevant.

`receipt-hygiene --explain` is the author-facing nudge for incomplete receipts.
It keeps hygiene read-only, but adds copyable commands and compact receipt patch
examples for missing evidence such as public git metadata, secrets posture,
permission posture, or rollback specificity.

For bridge-db, keep the risk tier honest:

- `T0`: documentation-only changes with no runtime effect.
- `T1`: narrow code changes covered by focused tests and the routine verifier.
- `T2`: user-visible CLI, schema, or MCP behavior changes.
- `T3`: caller ownership, write paths, audit/recall consistency, or workflow
  changes.
- `T4`: releases, migrations, security-sensitive changes, or irreversible data
  operations.
