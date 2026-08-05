# BridgeDB Immutable Execution Generations

## Contract

`bridge_db.execution_generation` stages a clean, exact reviewed Git commit into
a content-addressed release. Each release binds:

- the full tracked source-tree digest;
- `pyproject.toml` and `uv.lock` dependency digests;
- the MCP/auth/integration contract digest;
- the exact Python interpreter digest;
- an immutable launcher and its digest.

The release tree is read-only. `current` and `previous` are relative symlinks
under one private execution root. Activation replaces `current` atomically,
records `previous`, writes an exact readback receipt, and leaves an explicit
cooperative drain request for the superseded generation. It never enumerates or
terminates processes. A pending activation journal makes interrupted state
non-green; readback never guesses through it.

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
`<execution-root>/current/bin/bridge-db-mcp`. The launcher injects the exact
generation manifest into the process. `health` and `status` expose that identity;
a mutable direct checkout, missing manifest, layout mismatch, or digest mismatch
is explicit operating attention rather than verified activation.

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
symlinks. The registry and binding file are staged at mode `0600`, replaced per
file, fsynced, and read back as the same grant generation. A partial replacement
is restored from the exact in-memory originals. The receipt contains caller,
generation, target paths, and readback state only: it never contains the secret
or its digest.

Activation and credential binding are separate governed effects. Stage and
verify the reviewed generation first, activate it through the normal operating
change lane, then rotate/bind the exact Codex principal and verify a fresh spawn.
Do not treat source availability as installed or live uptake.

## Rollback and drain

- `rollback` atomically activates `previous` and makes the displaced generation
  the new rollback target.
- Activation receipts are append-only under `receipts/` and bind requested and
  read-back generation identities.
- Drain requests are declarative and cooperative. The tenancy lifecycle owns
  active-request/owner/lease decisions; no generation command kills a process.
- Release deletion is outside this contract. Retained generations remain the
  rollback evidence until a separate exact cleanup approval exists.
