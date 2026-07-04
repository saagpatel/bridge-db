# AGENTS.md - bridge-db MCP Tools

## Review guidelines

Treat every MCP tool as an API contract. Review exact input fields, output
keys, error shapes, caller ownership checks, pagination, and mutation semantics
for silent drift. A renamed field, broadened default, or swallowed conflict can
break downstream agents even when tests still pass.

Mutation tools must preserve explicit target selection, dry-run or preview
semantics where present, idempotency, and auditability. Pay special attention
to shipped-event disposition, handoff clearing, context updates, snapshot
writes, markdown export/sync, and any path that can mark work processed.

Health/status tools must not flatten partial failures into ready state. If DB
access, FTS consistency, schema version, export freshness, or audit-log writes
are unknown or degraded, review comments should keep that uncertainty visible
instead of accepting optimistic status text.
