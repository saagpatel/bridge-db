# fable-explore/ — bridge-db deep-dive session index

Working folder for the 2026-07-10 exploration session (Fable). Discovery → proposals →
public material for saagarpatel.dev. Ground rules: public-safe = architecture and
reasoning only, never bridge contents; coordination/memory/data-model side only;
code changes are drafts for review, live DB untouched.

## Contents

1. **[01-findings-system-teardown.md](01-findings-system-teardown.md)** — full-read
   findings: the four-plane view of bridge-db, eight load-bearing design moves,
   lifecycle map, 7 rough edges, 7 public-material angles. *Complete.*
2. **[02-explainer-research.md](02-explainer-research.md)** — web research synthesis:
   12 explainer techniques, 5 novel formats ranked, own-system teardown craft,
   saturation map. Strategy line: the subject IS the medium's strongest technique.
   *Complete.*
3. **[03-proposals.md](03-proposals.md)** — six improvement proposals as reviewable
   drafts with diff sketches + test plans, ranked. Headliner P1: two `date.today()`
   calls bypass the DST clock seam (verified; also a local-vs-UTC inconsistency).
   P2: health is blind to `write_conflicts`. P3: active handoff claims unreadable.
   P4–P6 smaller. *Complete, awaiting review — no src/ changes made.*

## Public material drafts (04-public/)

4. **[interleaving-explorer.html](04-public/interleaving-explorer.html)** — the
   flagship: self-contained, zero-dependency interactive explainer of the CAS
   lost-update race. Seeded scheduler, step/scrub/play, warn-vs-enforce on identical
   schedules, all-six-interleavings map, 30-seed sweep, receipts minted live.
   Model verified by script (exactly 2 dangerous schedules in warn; 0 losses in
   enforce across all seeds; never trace-free) + headless Chrome render check.
   Open directly: `open fable-explore/04-public/interleaving-explorer.html`
5. **[essay-dst-on-a-small-system.md](04-public/essay-dst-on-a-small-system.md)** —
   "A thousand tiny catastrophes for a five-agent SQLite file." The DST story with
   dated scars (the 17/30 sweep that flipped enforce, the receipt crash window, the
   trust TOCTOU) and an honest confession: the clock-seam leak found while writing it.
   Companion to the explorer.
6. **[essay-losing-loudly.md](04-public/essay-losing-loudly.md)** — receipts as a
   design pattern: five receipt sites, the two-transaction bug, errors-are-for-callers
   vs receipts-are-for-operators. Sibling of the existing empty-conflict-table piece.
7. **[bridge-db-four-planes.svg](04-public/bridge-db-four-planes.svg)** — architecture
   diagram: five callers → tool surface → four planes (coordination / memory /
   accountability / verification) → one SQLite file. Structure only, no data.

## Angles deliberately left on the shelf (from 01, if wanted later)

- "A schema is a ledger of your mistakes" (migration ladder v1→v13 as autobiography)
- "sometimes(): asserting your guards actually fire" (reachability counters)
- "The claim is not the SELECT" (largely absorbed by the explorer + DST essay)

## Publication notes

- Both essays passed a zero-em-dash check; explorer prose likewise (JS comments keep
  code idiom).
- Site integration: explorer palette is CSS-variable-scoped at the top of the file for
  easy reskin; SVG uses the same paper palette.
- The DST essay's confession section depends on proposal P1 landing (or being openly
  "in review") before publication, so the essay's claims and the repo agree.
