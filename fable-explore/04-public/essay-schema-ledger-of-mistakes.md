# A schema is a ledger of your mistakes

*Draft for saagarpatel.dev. Third in the bridge-db series, after the simulation-testing piece and "Losing loudly."*

My agents' coordination database migrates forward only. Version 1 to version 2 to version 13, each step applied in order, each committing independently so a crash mid-ladder leaves the database at the last complete rung. There is no down migration. There never will be; the data a migration reshapes can't be unreshaped, so a down path is mostly a comforting fiction.

Forward-only has a side effect I didn't appreciate until I reread the whole ladder in one sitting: you can never delete a rung. Every migration stays in the source forever, because some database somewhere might still be three versions back. Which means the migration ladder is an autobiography with no delete key. Every version is a confession of something version-minus-one got wrong, written at the moment I understood the mistake, preserved verbatim.

Read in order, mine says more about how the system actually evolved than any document I wrote on purpose.

## The ladder, read as a life

**v2: the guest list was hardcoded.** The schema's CHECK constraints enumerated exactly which systems could write, and then the fleet grew by two. SQLite can't alter a CHECK constraint, so v2 is the full rename-copy-drop dance: rename the table, recreate it with a wider guest list, copy everything across, drop the original. A twenty-line ceremony to change one tuple. First lesson, learned early: anything you hardcode about who participates will be wrong within months.

**v3: memory arrived.** A full-text index mirroring every content table, so any agent could ask "have I seen this before." The migration is two lines of DDL and a post-hook that rebuilds the index from source tables. The interesting part came later. Hold that thought for v11.

**v4: claims started requiring proof.** A receipts table for shipped work, because "I synced that downstream" is a sentence any agent can emit and no one can audit. From v4 on, done means a receipt with a downstream reference in it.

**v5 and v6: the same mistake, twice, one version apart.** Agents logged the same project under different names, so v5 added a canonical identity key to the activity log, resolved on write against an external registry. Then a handoff dispatched under one name failed to clear under a sibling alias, and v6 added the identical column to the handoff table. I want to claim I saw the general problem the first time. The ladder says otherwise: the lesson arrived table by table, and the schema recorded my learning speed exactly.

**v7: provenance became a column.** Every instruction-bearing table gained a trust label: operator-asserted, agent-authored, or ingested from outside. This is the schema absorbing a truth about coordinating AI agents, which is that where a row came from governs what it's allowed to make happen. The migration includes a backfill judgment call, written straight into the comment: pre-existing sections and handoffs were operator-authored history and got labeled operator; old activity kept the conservative agent default. That's a decision about the past, made once, recorded where the next reader will trip over it.

**v8: two ways to be done.** Receipts prove a shipped event reached its downstream. But some events legitimately owe no sync, and before v8 they just nagged forever or got waved through. Dispositions are the second terminal state: an explicit, typed reason why no receipt will ever come. The difference between "done with proof" and "excused with a signature" turned out to deserve its own table.

**v9: money.** Per-session cost attribution. Every long-running system eventually admits it has a budget.

**v10: the day I stopped trusting agents not to race.** Three things in one migration: an integer version on every context section for compare-and-swap, a durable table of write-conflict receipts, and recorded state for what the file export was based on. One migration, one realization. Five agents sharing rows will eventually write over each other, and the day you accept that, you need optimistic concurrency, evidence for every refusal, and a way to tell a stale file import from a fresh one, all at once.

**v11: the confession.** My favorite rung, because its DDL is literally a comment. At some point I changed what went into the search index so that tags became searchable, and I shipped it without bumping the schema version. New rows got indexed the new way. Every existing database kept its old index rows, silently, and searches for lifecycle tags missed all of history. v11 exists only to atone: no schema change, just a post-hook that re-indexes what the unversioned change orphaned. The migration text is one line long and it is the most instructive rung on the ladder. Version your data transformations like schema, because that's what they are.

**v12: keeping guesses out of the actuals.** Cost rows are measured facts. Then I wanted heuristic classification on top: what kind of session was this, which model dominated, how confident is the guess. v12 puts all of that in a sidecar table with a confidence column, keyed to the actuals but never mixed into them. There's a small data-modeling ethics in that separation. Facts and inferences can share a join key; they shouldn't share a table.

**v13: a column ordered by a machine.** A deterministic simulator found that one agent could clear a handoff another agent was actively holding, because the schema never recorded who claimed it. The fix needed memory, and memory in a schema means a column: `claimed_by`, written at pickup, checked at clear. Twelve versions of this schema came from me noticing things. The thirteenth came from a test harness proving a race I hadn't imagined. I'd call that the ladder's trajectory in one sentence.

## What the ladder knows

Put the thirteen versions side by side and the arc is legible: widen who participates, add memory, then spend version after version on the same deepening theme, which is that claims need evidence. Receipts, identity, provenance, dispositions, conflict receipts, claimants. A coordination system for AI agents turns out to be mostly an evidence system, and I learned that one migration at a time.

Two guardrails frame the whole thing. Each rung commits independently, so a crash mid-migration strands you at a real version instead of between versions. And a database newer than the running code refuses to open at all, because a v13 binary guessing at v14 data is how autobiographies get rewritten by someone who wasn't there.

Try reading your own ladder start to finish sometime. Not the schema as it is; the steps that made it. The current schema shows what you believe now. The migrations show what it cost to believe it, which mistakes you made twice before generalizing, and which fixes you shipped without versioning and had to confess to later. Mine has all three, on the permanent record, which is exactly where they belong.
