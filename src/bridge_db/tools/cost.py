"""Cost tracking tools: record_cost, get_cost_history."""

import logging
import re
from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from bridge_db.db import get_db
from bridge_db.models import (
    COST_SYSTEM_MAP,
    READABLE_SYSTEMS,
    CallerID,
    cost_ownership_error,
    invalid_system_error,
)

logger = logging.getLogger("bridge_db.tools.cost")

_MONTH_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def record_cost(
        caller: Annotated[
            CallerID,
            Field(description="Must be 'cc', 'codex', 'notion_os', or 'personal_ops'"),
        ],
        month: Annotated[str, Field(description="Month in YYYY-MM format, e.g. '2026-04'")],
        amount: Annotated[float, Field(description="Cost in USD", ge=0)],
        notes: Annotated[
            str | None, Field(description="Optional notes about this cost entry")
        ] = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Upsert a monthly cost record. Caller must own the system they are updating."""
        system = COST_SYSTEM_MAP.get(caller)
        if system is None:
            raise ToolError(cost_ownership_error(caller))

        if not _MONTH_RE.match(month):
            raise ToolError(f"Invalid month format '{month}', expected 'YYYY-MM'")

        db = get_db(ctx)
        await db.execute(
            """
            INSERT INTO cost_records (system, month, amount, notes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(system, month) DO UPDATE SET
                amount = excluded.amount,
                notes = excluded.notes,
                recorded_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """,
            (system, month, amount, notes),
        )
        await db.commit()
        logger.info("cost recorded: system=%s month=%s amount=%.2f", system, month, amount)
        return {"ok": True, "system": system, "month": month, "amount": amount}

    @mcp.tool()
    async def get_cost_history(
        system: Annotated[
            str | None, Field(description="Filter by 'cc' or 'codex'. Omit for all.")
        ] = None,
        limit: Annotated[int, Field(description="Max records to return", ge=1, le=120)] = 12,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> list[dict[str, Any]]:
        """Return cost records, most recent month first."""
        db = get_db(ctx)

        if system is not None:
            if system not in READABLE_SYSTEMS:
                raise ToolError(invalid_system_error(system))
            cursor = await db.execute(
                "SELECT system, month, amount, notes, recorded_at FROM cost_records "
                "WHERE system = ? ORDER BY month DESC LIMIT ?",
                (system, limit),
            )
        else:
            cursor = await db.execute(
                "SELECT system, month, amount, notes, recorded_at FROM cost_records "
                "ORDER BY month DESC LIMIT ?",
                (limit,),
            )

        rows = await cursor.fetchall()
        return [
            {
                "system": r["system"],
                "month": r["month"],
                "amount": r["amount"],
                "notes": r["notes"],
                "recorded_at": r["recorded_at"],
            }
            for r in rows
        ]