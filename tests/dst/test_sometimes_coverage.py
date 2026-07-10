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
- legacy_blind_write_accepted   → cas-pingpong warn @ LOST_UPDATE_SEED
- stale_cas_rejection           → cas-pingpong warn @ STALE_CAS_SEED (CAS loses)
- missing_cas_rejection         → cas-pingpong enforce @ LOST_UPDATE_SEED
- wal_truncated                 → wal-starvation control @ PINNED_SEED

Known-unreachable by the current corpus (Phase 2+ scenario debt, NOT
asserted here — listing them keeps the gap loud instead of silent):
- clear_refused_foreign_claim — needs the clear-time boundary scenario
  (R4 WC-2 / the INV-3 clear race); no corpus scenario drives
  clear_handoff yet.
- attribution_divergence — needs AUTH_MODE=warn with a channel-bound
  principal diverging from the claimed caller (R4 RC-10); the DST suite
  runs auth off.
"""

from pathlib import Path

from bridge_db.invariants import sometimes_counts
from dst.test_cas_pingpong import LOST_UPDATE_SEED, run_cas_pingpong
from dst.test_claim_race import RACING_SEED, run_claim_race
from dst.test_receipt_crash import CRASHING_SEED, run_receipt_crash
from dst.test_wal_starvation import PINNED_SEED, run_wal_starvation

# cas-pingpong warn arm where the CAS writer loses (blind write commits
# first, CAS token goes stale). Re-pinned 0 → 1 with the Phase-3 INV-5
# fix: moving receipts inside _upsert_section changed the rejection
# path's op count, shifting the RNG alignment (declared window change;
# this gate is what caught the old seed going dead).
STALE_CAS_SEED = 1

EXPECTED_LABELS = frozenset(
    {
        "raced_claim_receipt_written",
        "fault_fired_in_receipt_window",
        "stale_claim_receipt_written",
        "legacy_blind_write_accepted",
        "stale_cas_rejection",
        "missing_cas_rejection",
        "wal_truncated",
    }
)


async def test_every_corpus_reachable_label_fires(tmp_path: Path) -> None:
    await run_claim_race(tmp_path / "claim", RACING_SEED)
    await run_receipt_crash(tmp_path / "crash", CRASHING_SEED)
    await run_cas_pingpong(tmp_path / "warn", LOST_UPDATE_SEED, mode="warn")
    await run_cas_pingpong(tmp_path / "stale", STALE_CAS_SEED, mode="warn")
    await run_cas_pingpong(tmp_path / "enforce", LOST_UPDATE_SEED, mode="enforce")
    await run_wal_starvation(tmp_path / "wal", PINNED_SEED, leak=False)

    counts = sometimes_counts()
    dead = sorted(label for label in EXPECTED_LABELS if not counts.get(label))
    assert not dead, (
        f"sometimes() labels at 0 across the coverage corpus: {dead} — "
        "the code paths they mark were never reached; the runs above are "
        "vacuous there (R3 tier-1: a label at 0/N fails the build)"
    )
