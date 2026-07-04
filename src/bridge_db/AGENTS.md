# AGENTS.md - bridge-db Source

## Review guidelines

Treat source changes as cross-client state-contract changes. Review MCP tool
schemas, JSON-RPC stdout behavior, caller ownership, canonical key handling,
auth checks, migrations, audit logging, and FTS5 updates as merge-relevant when
they affect what Claude, Codex, Notion, or personal-ops can read or write.

Security-sensitive review should focus on confused-deputy paths: one caller
reading or clearing another caller's state, stale-write conflict masking,
unscoped exports, direct bridge-file writes, and migration fallback that
silently drops records, provenance, or audit rows. Missing freshness,
ownership, or schema evidence is unknown, not healthy.

Do not accept tool output that reports a write, export, sync, or recovery as
successful unless the code preserves the matching audit trail and recovery
path. If a change touches migrations, indexed content, or caller-bound writes,
require tests that cover both the success path and the stale/denied path.
