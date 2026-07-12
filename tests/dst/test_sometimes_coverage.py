"""CI tier 1, final stage: the sometimes() coverage report (R3 §2, §4).

sometimes() is the dead-harness defense: a label that never fires means
the code path it marks was never reached — the engine is testing nothing
there and every green above it is vacuous. R3's tier-1 contract is "a
label at 0/N fails the build": this test replays one pinned, cheapest
firing seed per label and fails if any corpus-reachable label stays 0.

Coverage roster (label → cheapest pinned firing run):
- raced_claim_receipt_written   → claim-race @ RACING_SEED
- fault_fired_in_receipt_window → receipt-crash @ CRASHING_SEED
- stale_claim_receipt_written   → receipt-crash @ CRASHING_SEED (recovery arm)
- stale_cas_rejection           → cas-vs-cas @ STALE_CAS_SEED (two honest CAS
  writers race; the loser goes stale, not missing_cas)
- missing_cas_rejection         → blind-vs-cas @ MISSING_CAS_SEED (unconditional
  since the 2026-07-12 canary cut — no longer race-dependent)
- wal_truncated                 → wal-starvation control @ PINNED_SEED
- clear_refused_foreign_claim    → clear-race @ CLEAR_REFUSED_SEED
- clear_refused_race             → clear-race @ CLEAR_REFUSED_SEED (race branch)

Known-unreachable by the current corpus (Phase 2+ scenario debt, NOT
asserted here — listing them keeps the gap loud instead of silent):
- attribution_divergence — needs AUTH_MODE=warn with a channel-bound
  principal diverging from the claimed caller (R4 RC-10); the DST suite
  runs auth off.
"""

from pathlib import Path

from bridge_db.invariants import sometimes_counts
from dst.test_cas_pingpong import (
    MISSING_CAS_SEED,
    STALE_CAS_SEED,
    run_blind_vs_cas,
    run_cas_vs_cas,
)
from dst.test_claim_race import RACING_SEED, run_claim_race
from dst.test_clear_race import CLEAR_REFUSED_SEED, run_clear_race
from dst.test_receipt_crash import CRASHING_SEED, run_receipt_crash
from dst.test_wal_starvation import PINNED_SEED, run_wal_starvation

EXPECTED_LABELS = frozenset(
    {
        "raced_claim_receipt_written",
        "fault_fired_in_receipt_window",
        "stale_claim_receipt_written",
        "stale_cas_rejection",
        "missing_cas_rejection",
        "wal_truncated",
        "clear_refused_foreign_claim",
        "clear_refused_race",
    }
)


async def test_every_corpus_reachable_label_fires(tmp_path: Path) -> None:
    await run_claim_race(tmp_path / "claim", RACING_SEED)
    await run_receipt_crash(tmp_path / "crash", CRASHING_SEED)
    await run_blind_vs_cas(tmp_path / "missing-cas", MISSING_CAS_SEED)
    await run_cas_vs_cas(tmp_path / "stale-cas", STALE_CAS_SEED)
    await run_wal_starvation(tmp_path / "wal", PINNED_SEED, leak=False)
    await run_clear_race(tmp_path / "clear", CLEAR_REFUSED_SEED)

    counts = sometimes_counts()
    dead = sorted(label for label in EXPECTED_LABELS if not counts.get(label))
    assert not dead, (
        f"sometimes() labels at 0 across the coverage corpus: {dead} — "
        "the code paths they mark were never reached; the runs above are "
        "vacuous there (R3 tier-1: a label at 0/N fails the build)"
    )
