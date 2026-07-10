"""Replay the pinned regression-seed corpus (R3 §2).

Every seed in regress_seeds.txt replays forever: a seed that found (or
pinned) a behavior once must keep reproducing it on every build, or the
build has broken determinism or lost the window (gate G6)."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from dst.test_claim_race import run_claim_race

_SCENARIOS: dict[str, Callable[[Path, int], Awaitable[dict[str, Any]]]] = {
    "claim-race": run_claim_race,
}

_SEEDS_FILE = Path(__file__).parent / "regress_seeds.txt"


def _corpus() -> list[tuple[str, int]]:
    entries: list[tuple[str, int]] = []
    for line in _SEEDS_FILE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        scenario, seed = stripped.split()
        entries.append((scenario, int(seed)))
    return entries


@pytest.mark.parametrize(("scenario", "seed"), _corpus())
async def test_regress_seed_replays(scenario: str, seed: int, tmp_path: Path) -> None:
    runner = _SCENARIOS[scenario]
    await runner(tmp_path, seed)  # per-seed oracle asserts inside
