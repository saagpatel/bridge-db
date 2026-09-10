"""R3 §1.2 go/no-go probe, kept as a permanent guard.

The whole SQL-side clock strategy rests on one documented-but-unusual
SQLite behavior: an application-defined function named ``strftime``
overrides the built-in for that connection, INCLUDING when SQLite
evaluates a column DEFAULT expression. If a CPython/SQLite upgrade ever
breaks this, every DST result is untrustworthy — this test is the tripwire
(fallbacks: APSW custom VFS, or bound-parameter defaults; R3 §1.2.2).
"""

import sqlite3

SIM_TIME = "2030-01-02T03:04:05Z"


def _sim_strftime(*_args: object) -> str:
    return SIM_TIME


def test_strftime_override_captures_defaults_and_late_ddl() -> None:
    conn = sqlite3.connect(":memory:")
    conn.create_function("strftime", -1, _sim_strftime)

    direct = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()[0]
    assert direct == SIM_TIME

    conn.execute(
        "CREATE TABLE probe (id INTEGER PRIMARY KEY, "
        "ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')))"
    )
    conn.execute("INSERT INTO probe DEFAULT VALUES")
    assert conn.execute("SELECT ts FROM probe").fetchone()[0] == SIM_TIME


def test_strftime_override_wins_when_registered_after_ddl() -> None:
    # The production schema is created before any sim harness exists; the
    # override must still capture defaults on tables from prior DDL.
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE probe (id INTEGER PRIMARY KEY, "
        "ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')))"
    )
    conn.create_function("strftime", -1, _sim_strftime)
    conn.execute("INSERT INTO probe DEFAULT VALUES")
    assert conn.execute("SELECT ts FROM probe").fetchone()[0] == SIM_TIME
