# BridgeDB Immutable Execution Generations

## Contract

`bridge_db.execution_generation` stages a clean, exact reviewed Git commit into
a content-addressed release. Each release binds:

- the full tracked source-tree digest;
- `pyproject.toml` and `uv.lock` dependency digests;
- the MCP/auth/integration contract digest;
- the exact Python interpreter digest;
- an immutable launcher and its digest.

Verification walks the complete release tree. Extra files/directories,
symlinks, special files, owner drift, mode drift, source digest drift, launcher
drift, and external interpreter drift all fail closed. The generation ID is
recomputed from the reviewed SHA prefix and full tracked-source digest. Staging
also rechecks the clean reviewed checkout after copying, so a concurrent source
mutation cannot be published under the reviewed identity.

The release tree is read-only. `current` and `previous` are relative symlinks
under one private execution root. Activation replaces `current` atomically,
records `previous`, writes an exact readback receipt, and leaves an explicit
cooperative drain request for the superseded generation. It never enumerates or
terminates processes. A pending activation journal makes interrupted state
non-green; readback never guesses through it. Under the activation lock, retry
recovers only the exact before map, the exact in-order partial map, or the exact
committed map. It restores a before/partial map before retry, finalizes drain
and receipt actions for a committed map, and refuses any arbitrary pointer map
while retaining the journal for review.

Pending-journal recovery always runs first so an already-committed pointer
transition can finish its drain/receipt work even if recovery evidence became
stale after the commit. Before any new `activate` or `rollback` pointer mutation,
the command then calls the owning recovery lifecycle readers for the configured
BridgeDB path. Both `RecoveryAnchorV1` and the latest `RecoverySealReceiptV1`
must read back `state=verified` and `ready=true`; missing, stale, unsealed, or
invalid evidence fails closed before a pending journal or pointer is written.

## Commands

Run from the exact clean reviewed checkout, with an existing dependency
environment. Staging does not install or hydrate dependencies.

```bash
python -m bridge_db.execution_generation stage \
  --source /absolute/path/to/clean/bridge-db \
  --root /absolute/private/path/to/bridge-db-runtime \
  --reviewed-sha <full-40-character-sha> \
  --python-executable /absolute/path/to/python

python -m bridge_db.execution_generation verify \
  --root /absolute/private/path/to/bridge-db-runtime \
  --generation-id <generation-id>

python -m bridge_db.execution_generation activate \
  --root /absolute/private/path/to/bridge-db-runtime \
  --generation-id <generation-id>

python -m bridge_db.execution_generation readback \
  --root /absolute/private/path/to/bridge-db-runtime

python -m bridge_db.execution_generation rollback \
  --root /absolute/private/path/to/bridge-db-runtime
```

Point each MCP client at
`<execution-root>/current/bin/bridge-db-mcp`. The launcher accepts the normal
`bridge_db` CLI arguments, including `--checkpoint`, and injects the exact
generation manifest into the process. `health` and `status` expose that identity;
a mutable direct checkout, missing manifest, layout mismatch, or digest mismatch
is explicit operating attention rather than verified activation.

The interpreter executable bytes are checked during staging, verification,
activation readback, runtime identity readback, and launcher startup. This does
not make its external environment immutable: installed packages, the standard
library, shared libraries, and OS runtime remain outside the release. The
precise claim is `source_and_interpreter_bound_external_environment_unmanaged`;
`pyproject.toml` and `uv.lock` are bound lockfile evidence, not proof that the
external environment matches them. A managed content-addressed environment is
a separate future activation contract.

## Secure Codex binding

`bridge_db.secure_binding` is the reviewed Codex rotation/binding path. It
accepts secret material only through a descriptor greater than stderr. A
regular-file descriptor must refer to an owner-only file; pipes and sockets are
also accepted. The command rejects secret material in stdio, argv, or ordinary
environment arguments.

```bash
python -m bridge_db.secure_binding \
  --caller codex \
  --secret-fd 3 \
  --principals-path /absolute/private/path/principals.json \
  --binding-path /absolute/private/path/bridge-db.env \
  --auth-mode warn
```

Both parent directories and any existing targets must be owner-only and free of
symlinks. Canonically ordered locks cover both target parents, including when
the registry and binding live in different directories; lock symlinks and
insecure lock files are refused. The registry and binding file are staged at
mode `0600`, replaced per file, fsynced, and read back as the same grant
generation. A caught replacement or readback failure restores the exact
in-memory originals. This is not a crash-atomic two-file transaction: an abrupt
process crash between the replacements can leave a split pair, which the
standalone verifier reports non-green and which requires an exact retry or
recovery. The receipt contains caller, generation, target paths, and readback
state only: it never contains the secret or its digest.

Activation and credential binding are separate governed effects. Stage and
verify the reviewed generation first, activate it through the normal operating
change lane, then rotate/bind the exact Codex principal and verify a fresh spawn.
Do not treat source availability as installed or live uptake.

## Client launcher convergence

`bridge_db.client_rebinding` changes only the exact `mcpServers.bridge-db`
legacy invocation found in the Claude Code and Claude Desktop JSON configs:
`uv run --directory /Users/d/Projects/bridge-db python -m bridge_db`. It replaces
that command/argument pair with
`/Users/d/.local/state/bridge-db/current/bin/bridge-db-mcp` and an empty argument
list. The existing two-key environment mapping is value-equivalent before and
after readback, but no environment value is returned. Unexpected JSON shape,
launcher arguments, permissions, owners, symlinks, or concurrent changes fail
closed.

```bash
python -m bridge_db.client_rebinding rebind \
  --client claude-code \
  --config-path /Users/d/.claude.json \
  --backup-root /Users/d/.local/state/bridge-db/client-config-backups

python -m bridge_db.client_rebinding rebind \
  --client claude-desktop \
  --config-path "/Users/d/Library/Application Support/Claude/claude_desktop_config.json" \
  --backup-root /Users/d/.local/state/bridge-db/client-config-backups
```

Each change first preserves the exact original bytes in a private `0700` backup
root as a non-overwritten `0400` digest-named file. Replacement is per-file
atomic, mode `0600`, and exact-readback verified. The `restore` subcommand
requires the backup plus the expected current-config digest, preventing a stale
rollback from overwriting a concurrent edit.

The source-owned `config/bridge-db-mcp-immutable` is the reviewed Codex wrapper
input. Its installed target is `/Users/d/.codex/bin/bridge-db-mcp-immutable`.
It parses (never sources) `BRIDGE_DB_PRINCIPAL_TOKEN`,
`BRIDGE_DB_AUTH_MODE`, and the optional reviewed
`BRIDGE_DB_TRANSPORT_MODE=direct|shared` key from owner-only mode-`0600`
`/Users/d/.codex/secrets/bridge-db.env`, or from one explicit absolute
`BRIDGE_DB_ENV_FILE`, then execs the stable launcher. Unknown or duplicate keys
fail closed without printing values. The source-owned
`config/com.saagar.bridge-db-checkpoint.plist` preserves the 30-minute
run-with-receipt contract through the reviewed operator-script pointer
`/Users/d/.local/state/operator-scripts/current/run-with-receipt.sh`, while
invoking the same stable launcher with `--checkpoint`. These are reviewed
install inputs only; source presence is not proof of live installation or
client reconnect.

## MCP tenancy lifecycle

Each stdio server owns a private lease containing owner, bound principal,
generation, creation and request times, lifecycle reason, exact process-start
identity, PID ancestry, active-request count, and current RSS. Every tool call
is bracketed by request accounting. Representative Codex, Claude, and Personal
Ops replay rows derive per-owner process-count, lifetime, RSS, and idle-review
budgets; pooling is deliberately not introduced without live evidence that
lifecycle cleanup is insufficient.

The 2026-08-05 live selection snapshot observed approximately 30 concurrent
stdio server pairs: the ChatGPT app-server parent (PID 15780 at observation
time) owned the majority, Claude owned about nine, and Personal Ops and Hermes
owned one each. Observed lifetimes ranged from roughly one minute to 22 hours,
and Python RSS from roughly 4 MiB to 53 MiB. That baseline falsifies an
observability-only approach and selects client-close instrumentation plus
per-owner leases and cooperative generation drain; it does not justify pooling
or cross-process killing. The aggregate snapshot is not exact per-owner replay
evidence, so an exact replay-derived policy remains an activation prerequisite.
Same-generation budget excess remains an owner-review decision and does not
claim to force every tenant below budget. Activation plus controlled client
reload/reconnect is required before these leases can close legacy direct-path
processes.

`python -m bridge_db.tenancy status --root <private-tenancy-root>` inventories
leases. `plan --policy <replay-policy.json> --current-generation <id>` produces
a content-bound no-kill plan. `apply --plan <plan.json>` permits one actionable
lease; a plan with multiple actions requires `--lease-id <exact-id>`. Missing
or PID-reused processes are rechecked and their exact lease is retired even if
the crashed process left a stale active-request count. Live same-identity
requests and changed/unknown ancestry are never drained.

Obsolete live generations receive an exact cooperative marker. A server polls
its own marker, refuses new requests, waits for its current request count to
reach zero, then cancels its own server task so lifespan cleanup closes the DB
and moves the lease to append-only history. No lifecycle command enumerates or
signals another process. Exact-target application and deterministic retirement
receipts prevent an apply operation from partially mutating multiple leases.

### Shared runtime upgrade

The direct stdio runtime remains the default and rollback. When the reviewed
binding selects `BRIDGE_DB_TRANSPORT_MODE=shared`, a no-argument wrapper launch
registers the shell relay's PID/start identity, then ensures one broker for the
exact credential and complete non-secret launch contract: normalized auth mode,
generation/manifest, interpreter/runtime source, database, bridge, principal,
audit/evidence, registry, tenancy, logging, and idle-policy inputs. A private
random selector key derives the group name, so neither the credential nor a
reusable credential digest appears in argv, output, socket names, or receipts.
Each relay also receives a distinct random capability through an owner-only
mode-`0400` curl header file. Every broker request validates its lease and
capability hash plus the relay's current PID/start identity, then strips the
header before MCP dispatch; lifecycle scans retain the same identity gate.
The capability value never enters argv, logs, socket names, leases, history, or
broker receipts. The broker binds only an owner-only Unix socket, uses stateful
Streamable HTTP behind the stdio relay, and serializes tool calls so independent
MCP session connections cannot interleave database work.

Client lease history is preserved. Missing or PID-reused relay records are
retired after identity readback; unknown process state keeps the broker alive.
The broker exits cooperatively after 300 seconds with no live relay references,
and startup fails closed after 10 seconds. It has no process-signaling primitive
and does not close existing direct clients. CLI arguments always use the direct
launcher so checkpoint and recovery operations do not depend on the relay.
Returning the binding to `direct` is the exact rollback for future spawns.

`BridgeMcpTenancyInventoryV2` reports live process identity separately from the
on-disk lease population. `active_count`, owners, generations, request count,
oldest age, and RSS describe live identity-matched processes; `lease_count`,
stale/unknown counts, lease owner/generation maps, and
`lease_last_observed_rss_total_bytes` preserve the evidence needed to review
stale records without presenting them as live memory pressure.
`BridgeSharedRuntimeInventoryV1`, available in `health`, `status`, and
`python -m bridge_db --shared-runtime-status`, separately reports broker/socket
reachability, live/stale/unknown relay identities, and capability-file counts
without returning group selectors or capability values.
`BridgeSharedRuntimeReadinessV1` is the narrower health/status gate: it derives
the current group without creating state or exposing its selector, then requires
the exact broker PID/start identity, receipt, and socket. Aggregate activity in
another group therefore cannot render selected shared transport green.

## Database rollback compatibility

Snapshot refusals are an additive `BridgeSnapshotRefusalSchemaV1` extension over
core SQLite `user_version=23`; they do not advance the core version. The exact
previous merged generation (`d7272d489873faa5ed84c81734636ffc8cecb095`) uses
the same v23 upper-bound check, can open/read/write its existing tables, and
ignores the additive refusal table. Tests preserve refusal rows across a
previous-runtime-style open and core write. Rollback therefore preserves the
database, but the old generation cannot create or acknowledge refusals; that
feature is degraded until roll-forward.

Both activation and pointer rollback are code-gated on a current verified
recovery anchor/seal and the normal activation approval. A stale, missing,
invalid, or explicitly unsealed lifecycle is non-green; source compatibility
tests do not authorize activation.

## Rollback and drain

- `rollback` atomically activates `previous` and makes the displaced generation
  the new rollback target.
- Activation receipts are append-only under `receipts/` and bind requested and
  read-back generation identities.
- Drain requests are declarative and cooperative. The tenancy lifecycle owns
  active-request/owner/lease decisions; the old server observes its own marker,
  refuses new work, and exits through lifespan cleanup only after it is idle.
- Release deletion is outside this contract. Retained generations remain the
  rollback evidence until a separate exact cleanup approval exists.
