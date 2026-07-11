"""Clock-seam coverage: default dates flow through clock.now(), and the seam stays sealed.

clock.py's contract is that every Python-side wall-clock read in bridge_db goes
through now(). Two date.today() calls (log_activity's default timestamp,
build_markdown's "Last synced" line) violated it: they leaked real wall-clock
time into DST runs and, being local-time in an otherwise-UTC system, could
disagree with snapshots' UTC date near midnight. These tests pin the fix and a
grep-guard keeps new leaks out.
"""

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from conftest import CaptureMCP, make_ctx

from bridge_db import clock
from bridge_db.tools import activity as activity_mod
from bridge_db.tools.export import build_markdown

FROZEN = datetime(2030, 5, 6, 12, 0, 0, tzinfo=UTC)

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "bridge_db"
# Direct wall-clock reads; every match outside clock.py is a seam leak.
FORBIDDEN = ("date.today()", "datetime.now(", "time.time()")


async def test_log_activity_default_timestamp_uses_clock_seam(
    db: aiosqlite.Connection,
) -> None:
    cap = CaptureMCP()
    activity_mod.register(cap)
    clock.install(lambda: FROZEN)
    try:
        result = await cap.fns["log_activity"](
            caller="cc",
            project_name="SeamProject",
            summary="default-timestamp write",
            ctx=make_ctx(db),
        )
    finally:
        clock.reset()
    assert result["timestamp"] == "2030-05-06"

    cursor = await db.execute(
        "SELECT timestamp FROM activity_log WHERE project_name = 'SeamProject'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["timestamp"] == "2030-05-06"


async def test_build_markdown_last_synced_uses_clock_seam(
    db: aiosqlite.Connection,
) -> None:
    clock.install(lambda: FROZEN)
    try:
        content = await build_markdown(db)
    finally:
        clock.reset()
    assert "Last synced: 2030-05-06" in content


def test_no_wall_clock_reads_outside_clock_module() -> None:
    """Grep-guard: the seam inventory stays complete mechanically, not by docstring."""
    leaks: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if path.name == "clock.py":
            continue
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            if needle in text:
                leaks.append(f"{path.relative_to(SRC_ROOT)}: {needle}")
    assert not leaks, f"wall-clock reads bypassing the clock seam: {leaks}"
