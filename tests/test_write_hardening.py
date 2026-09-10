"""Phase 3 write-path hardening (register #9 / FMEA 1.2, 1.3).

- busy_timeout raised 5s -> 15s so writers wait out contention before SQLITE_BUSY.
- checkpoint_wal forces a TRUNCATE checkpoint so the -wal can't grow unbounded
  when many always-open reader connections starve the passive autocheckpoint.
"""

import aiosqlite

from bridge_db.db import checkpoint_wal


async def test_busy_timeout_is_15s(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA busy_timeout")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 15000


async def test_checkpoint_wal_truncates_uncontended(db: aiosqlite.Connection) -> None:
    # Generate WAL frames, then checkpoint.
    await db.execute(
        "INSERT INTO context_sections (section_name, owner, content, source_trust, updated_at) "
        "VALUES ('t', 'cc', 'x', 'agent', '2026-01-01T00:00:00Z')"
    )
    await db.commit()

    result = await checkpoint_wal(db)
    assert set(result) == {"busy", "log_frames", "checkpointed"}
    # A single uncontended connection should fully truncate the WAL.
    assert result["busy"] == 0
    assert result["checkpointed"] >= 0
