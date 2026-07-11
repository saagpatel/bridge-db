# A thousand tiny catastrophes for a five-agent SQLite file

*Draft for saagarpatel.dev. Companion piece: the [Interleaving Explorer](interleaving-explorer.html), a runnable model of the bug described below.*

My AI agents coordinate through one SQLite file. Claude Code logs what it shipped, Codex picks up handoffs, Claude.ai updates long-lived context, and a couple of local services read along. Five writers, one database, one laptop. For months the test suite was green, and the system worked, and I had no idea whether it was correct.

Those are different things. The tests exercised each tool one call at a time. The failures I actually feared were the ones that need two agents, an unlucky ordering, and a crash in a specific hundred-millisecond window. You can't write a unit test for "the process dies between these two commits." You can wait for it to happen in production, once, unreproducibly, at 2am. That's the traditional approach.

The database companies solved this years ago. FoundationDB and TigerBeetle run their real code inside a simulator that owns everything nondeterministic: the clock, the scheduler, the network, the crashes. Every run is a pure function of a random seed. Find a bug at seed 4217, and seed 4217 reproduces it forever, byte for byte. The technique has a reputation for being heavy machinery, the kind of thing you build when you have a storage engine and a team.

I wanted to know what it costs at my scale. The answer turned out to be about four hundred lines.

## Closing the seams

Determinism isn't something you add. It's something you stop leaking. The leaks in a Python-plus-SQLite server turn out to be enumerable, and each one closes with a small, boring piece of code.

**Time, Python side.** Every wall-clock read goes through one function, `clock.now()`, which a test can replace with a logical clock that only advances when told to. Standard seam, ten lines.

**Time, SQL side.** This one surprised me. Half my timestamps never touch Python at all; they come from column defaults like `strftime('%Y-%m-%dT%H:%M:%SZ','now')` evaluated inside SQLite. So the simulator registers its own `strftime` on the connection, and SQLite happily calls the override instead of the builtin. The simulated clock now reaches all the way down into the schema. If any code path calls strftime in a shape the model doesn't cover, it raises loudly rather than silently falling back to real time.

**Scheduling.** The production server uses an async SQLite driver with a worker thread, which is a source of ordering the harness can't control. The simulator swaps in a facade with the same interface, executing synchronously, and parks every writer at every database operation. A seeded random number generator decides who proceeds. All concurrency in a run becomes an explicit, replayable list of grants.

**Faults.** Crashes and errors are injected at named points, keyed on statement fingerprints, with probabilities drawn from the same seeded generator. A "crash" discards the connection's open transaction, exactly as if the OS had killed the process mid-call. The precision matters: one bug below only exists in a two-statement window, and the injector can land a crash inside it on purpose.

Hash the trace of every run and you get the property that makes all of this worthwhile: same seed, same code, same bytes, every time.

## What it found

I want to be precise here, because "simulation testing found bugs" is the kind of sentence anyone can type. These are the dated scars.

**The default it flipped.** Context sections support compare-and-swap: read the row, get a version, write back conditioned on that version. But blind writes without the version token were still accepted for compatibility, in a mode called warn. The simulator ran two writers, one careful and one blind, across thirty seeds. On seventeen of the thirty, both writers were told their writes succeeded and the careful writer's committed work was silently destroyed. Seventeen of thirty is not an edge case; it's a coin flip that eats your data. On 2026-07-10 the production default flipped to enforce, and the config file cites the evidence seed in a comment. The explorer linked above lets you run the same experiment in your browser.

**The receipt that died with the process.** When two agents race to claim the same handoff, the loser is supposed to get a durable conflict receipt, a row that says "you lost, here's what happened." The original code rolled back the failed claim, then wrote the receipt in a second transaction. The simulator found a seed where a crash lands between those two operations. The claim loss is real, the receipt is gone, and the forensic record has a hole exactly where the accident was. The fix stages the receipt inside the same transaction the failed claim already holds open, so one commit makes the loss and its evidence durable together. Then a second seed found that two concurrent crash-recovery retries could double-write the receipt, which is how the insert became an atomic insert-if-absent. Three rounds of pin, fix, re-pin, all recorded in the seed corpus. The full anatomy of that bug is the centerpiece of the receipts piece that accompanies this one.

**The window nobody writes through yet.** A handoff's trust label is checked when an agent picks it up. The check read the label, decided, then wrote. Nothing in the current system mutates a trust label mid-flight, so the gap between read and write is unexploitable today. The simulator flagged it anyway, and the claim now re-verifies the label in the same UPDATE that takes the claim. Armor for a door nobody has walked through, bought for one line of SQL.

There's also a starvation scenario where a leaked transaction quietly blocks WAL checkpointing forever, and a race on clearing handoffs that ended with a new column in the schema, but this essay has to end eventually.

## The seed corpus

Every bug-finding seed gets committed to a text file and replayed by CI forever. The comments in that file are my favorite artifact of the whole project. They read like incident reports for incidents that never had to happen: which invariant went red, how many seeds out of thirty reach the failure, which fix round re-pinned the seed. A regression suite that is also a history of your own near-misses.

## The part where I confess

While writing this essay I grepped the codebase to double-check the claim that every wall-clock read goes through the seam. The seam's own docstring says exactly that. It's wrong. Two `date.today()` calls sit outside it, one in the activity logger and one in the markdown exporter, and both read the real clock even under simulation. Also `date.today()` returns local time in a system where everything else speaks UTC, which is its own small lie waiting for a stroke after midnight.

No current scenario trips either call, which is why the corpus never caught them. The fix is two lines. But it's the honest punchline for a piece like this: the seam inventory is never finished, the docstring is a claim rather than a proof, and the only durable defense is a check that greps for leaks mechanically. That check is going in with the fix.

## Do you need this?

You don't have a database company. Neither do I. Here is what I'd actually claim: if you have more than one writer and any state you'd be sad to lose, the interleavings exist whether or not you can see them. At five agents I hit lost updates, vanished receipts, and a starvation mode. The machinery to make those reproducible instead of anecdotal was a clock with a setter, a scheduler with a seed, and a few afternoons. The heavy version guards petabytes. The light version fits in a test directory, and it changed four production behaviors in its first two weeks.

There's an honest objection here, and I published it myself: my conflict table is empty. Months of production, zero documented races. Both facts are true, and they don't collide. The table records incidence; the sweep measures exposure. Seventeen of thirty orderings was the exposure, sitting quietly under an empty table, and the default flipped while the table still had nothing in it. That's the version of lucky you get to keep.

The bugs were always there. The simulator just gave them names.
