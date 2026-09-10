"""Tests for the wall-clock seam (DST Phase 0)."""

from datetime import UTC, datetime, timedelta

from bridge_db import clock


def test_default_now_is_real_utc() -> None:
    before = datetime.now(UTC)
    observed = clock.now()
    after = datetime.now(UTC)
    assert observed.tzinfo is not None
    assert before <= observed <= after


def test_installed_provider_wins_and_reset_restores() -> None:
    frozen = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    clock.install(lambda: frozen)
    try:
        assert clock.now() == frozen
        assert clock.now() == frozen  # stable across calls
    finally:
        clock.reset()
    assert abs(clock.now() - datetime.now(UTC)) < timedelta(seconds=5)
