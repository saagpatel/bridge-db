"""Wall-clock seam (DST Phase 0, R3 §1.2).

Every Python-side wall-clock read in bridge_db goes through ``now()`` so a
deterministic-simulation harness can install its own time provider. The
production default is the real UTC clock; with no provider installed this
module changes nothing at runtime.

SQLite-side ``strftime('%Y-%m-%dT%H:%M:%SZ','now')`` defaults are the other
half of the clock problem and are deliberately NOT covered here — that seam
is Phase 1 (per-connection ``strftime`` override probe).
"""

from collections.abc import Callable
from datetime import UTC, datetime

_provider: Callable[[], datetime] | None = None


def now() -> datetime:
    """Return the current UTC-aware time from the installed provider."""
    if _provider is not None:
        return _provider()
    return datetime.now(UTC)


def install(provider: Callable[[], datetime]) -> None:
    """Install a time provider (simulation/test use only)."""
    global _provider
    _provider = provider


def reset() -> None:
    """Restore the real UTC clock."""
    global _provider
    _provider = None
