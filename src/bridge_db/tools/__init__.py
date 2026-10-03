"""Tool registration: wire all tool modules onto the MCPServer instance."""

from mcp.server.mcpserver import MCPServer


def register_all(mcp: MCPServer) -> None:
    """Register all tool groups. Import order is documentation order."""
    from bridge_db.tools import (
        activity,
        audit,
        conflicts,
        context,
        cost,
        export,
        handoffs,
        health,
        recall,
        snapshots,
    )

    activity.register(mcp)
    handoffs.register(mcp)
    context.register(mcp)
    snapshots.register(mcp)
    cost.register(mcp)
    export.register(mcp)
    health.register(mcp)
    recall.register(mcp)
    audit.register(mcp)
    conflicts.register(mcp)
