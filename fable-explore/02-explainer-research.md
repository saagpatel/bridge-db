# Explainer craft research (subagent synthesis, 2026-07-10)

Verbatim report from the research pass over the technical-explainer landscape.
Feeds format choices in 04-public/.

## (a) Mechanical techniques that make explainers great

1. **Direct manipulation over playback.** Ciechanowski: you *drag* the planet to change its orbit, not press play — the input→behavior link is felt, not told. (ciechanow.ski)
2. **Custom HTML elements as a reusable engine.** Sam Rose's newer posts compile TypeScript Web Components encapsulating sliders/scrubbers/replay buttons — one widget vocabulary reused across a post. (github.com/samwho/visualisations)
3. **Bottom-up progressive disclosure.** Rose starts from the simplest example and layers up; complexity accretes, reader never faces the full system cold.
4. **Seeded randomness + exact replay.** TigerBeetle's VOPR reproduces any failure from (seed, commit hash). As a reader device: a "replay this exact failure" button is shareable and deterministic.
5. **Time compression as the hook.** "3.3s sim = 39 min real" — express the system's leverage as a compression ratio.
6. **Simulator-as-game.** TigerBeetle put a distributed DB in the browser and let readers inject the faults.
7. **State-space made visible.** Code left, generated interleaving/state graph right.
8. **Sensory microfeedback.** Josh Comeau's subtle slider "click" — the page feels alive.
9. **Vanilla / near-zero deps.** Self-contained pages load instantly, age well, stay hackable-by-view-source.
10. **Assertions as narrative spine.** Surface an invariant, then show the sim try to violate it — built-in tension.
11. **Thematic cross-linking.** A small interlinked "workshop" compounds more than one-offs.
12. **Live-debugging as content.** matklad's IronBeetle streams — the scar, filmed.

## (b) Novel format opportunities (ranked by distinctiveness)

1. **Explorable failure reproduction.** Embed the *actual* DST scenario in-page: reader picks a seed, watches agents race a CAS write, sees the conflict receipt minted, hits replay for the identical interleaving. Almost nobody ships a runnable reproduction of their own concurrency bug.
2. **Interleaving explorer / scrubber.** Drag/step through two agents' ops; watch which interleavings preserve the invariant and which trip it. Polished single-file version for optimistic concurrency is open space.
3. **Single-file HTML system simulator.** "No build, view-source is the tutorial" — fits the SQLite-minimalism thesis.
4. **The "N=5 bites" register.** "Five agents, one SQLite file, and CAP-flavored problems still find you." Nearly empty niche.
5. **Receipt-driven narrative.** Reader *causes* a conflict, then inspects the durable receipt — evidence over assertion, mirroring the system's own honesty design.

## (c) Craft principles for own-system teardowns

- **Show the scars, dated.** (fly.io "We Were Wrong About GPUs" register.) The DST gap-seeds, the warn→enforce flip, the enforce-readiness hold ARE the credibility.
- **Declare your bias.** "I built this, here's what I'd do differently."
- **Earn the machinery you skip.** "You do NOT need Raft at five agents — but optimistic concurrency, retention races, and stale reads still bite." Show the bite, then the minimal idea.
- **Invariant-first, not feature-first.** Lead with the property promised, then dramatize the system defending it.
- **Finite polish, ship, revisit.** A post is not a software project.
- **Beware the template smell.** Let the strongest posts be the interactive ones.

## (d) Saturation map

**Overdone:** "SQLite is enough"/Litestream genre (fully templated, now bleeding into
AI-workflow writing); "year of multi-agent systems" thought-leadership listicles;
DST *concept* explainers (Antithesis/TigerBeetle/WarpStream covered the definitional
layer).

**Open whitespace:**
- DST on a tiny personal system (every DST demo is a database company at scale).
- Interactive/runnable DST in-browser (almost all DST writing is prose).
- Personal-infra multi-agent coordination as candid narrative.
- Optimistic concurrency / CAS as an explorable (explained everywhere, rarely manipulable).
- Write-conflict receipts / honesty-as-mechanism as a reader-facing evidence device.

**One-line strategy:** Don't write another "SQLite is enough" or "what is DST" — build
the *runnable* version. The subject (DST, CAS, conflict receipts) IS the medium's
strongest technique, at the underexplored N=5 personal scale, told with the scars showing.

Sources: ciechanow.ski; samwho.dev; tigerbeetle.com/blog (VOPR, liveness sim, browser DB);
antithesis.com/docs; fly.io (wrong-about-gpu, all-in-on-sqlite-litestream);
writethatblog.substack.com; simonwillison.net/tags/explorables; wyounas.github.io;
obeli.sk.
