"""Tool registration: wire all tool modules onto the FastMCP instance."""

from mcp.server.fastmcp import FastMCP


def register_all(mcp: FastMCP) -> None:
    """Register all tool groups. Import order is documentation order."""
    from bridge_db.tools import activity, context, cost, export, handoffs, health, snapshots

    activity.register(mcp)
    handoffs.register(mcp)
    context.register(mcp)
    snapshots.register(mcp)
    cost.register(mcp)
    export.register(mcp)
    health.register(mcp)
