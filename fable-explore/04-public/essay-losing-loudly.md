# Losing loudly

*Draft for saagarpatel.dev. Sibling to the earlier piece on why the conflict table stays empty: that one explains the silence, this one explains the machinery that makes the silence trustworthy.*

There's a moment in every multi-writer system where you have to reject somebody. Two agents raced, one won, and now you're holding the loser's write. The question that shapes the whole design is what you do with it.

The common answer is an error. Return `conflict: true`, let the caller retry, move on. And an error is fine for the caller, who is standing right there. But the caller is an AI agent that will be garbage-collected in ninety seconds. The party who actually needs to know is me, three days later, wondering why a context section doesn't say what I remember it saying. An error message evaporates with the session that received it. What I need is a receipt.

So the rule in my coordination server is simple to state: no write loses quietly. Every refused write, every displaced write, every raced claim files a durable row in a `write_conflicts` table, with enough forensic detail to reconstruct what happened after everyone involved is gone.

## What a receipt records

A receipt is not a log line. Log lines describe intentions; receipts record outcomes, and they're structured, queryable, and permanent. Each one carries the surface (context section, markdown sync, handoff), the operation, who attempted it, the version they thought they were writing against, the version that was actually there, and a SHA-256 of both the attempted content and the content that was current at the time. The hashes are the part I'd defend hardest. Names and versions tell you a conflict happened. Hashes tell you exactly which bytes were at stake.

Five situations mint one:

1. A compare-and-swap write whose version token is stale. Someone else got there first; the receipt names both versions.
2. A write that should have carried a version token and didn't. Rejected in the current default mode, receipted either way.
3. A blind overwrite that was *accepted*, back when the permissive mode allowed it. This is the one I find philosophically interesting. The system let the write through, legally, by configuration. It still recorded a receipt carrying the hash of the content it destroyed. Permitted is not the same as anonymous.
4. A handoff claim that lost a race. Two agents both saw a pending handoff and grabbed for it; the guarded UPDATE picks one winner, and the loser's refusal is receipted.
5. A stale file import. The markdown fallback file tried to overwrite database state that had moved on since the file was exported. The export records its base version and hash precisely so this comparison has something to compare against.

## The two-transaction bug

The design sounds tidy. The implementation had a hole, and the hole is the most instructive part.

The first version of the raced-claim path did the obvious thing: roll back the failed claim, then write the receipt in a fresh transaction. Two steps. Which means there's a window between them, and a process that dies in that window has lost the claim *and* the evidence of losing it. The forensic record fails exactly and only when things go wrong, which is the one time it matters. My deterministic simulator found a seed that lands a crash in that window on demand.

The fix inverts the shape. When the claim's UPDATE matches zero rows, that zero-row UPDATE has already opened a transaction. The receipt INSERT joins it. One commit makes the refusal and its receipt durable atomically; a crash before the commit loses both, which is fine, because a claim that never happened needs no receipt. The invariant is that the loss and its evidence share a fate.

Then a second seed found the mirror image: a crashed loser retries, and two concurrent retries could file the receipt twice. So the retry path writes through an insert-if-absent whose existence check lives inside the INSERT statement itself, atomic at statement level. Concurrent retries converge on exactly one row. Getting a table of regrets to be crash-safe and idempotent took three rounds of simulation, fix, and re-pin. Receipts are cheap. Correct receipts are engineering.

## Errors are for callers, receipts are for operators

The distinction I keep coming back to: an error answers "what should this caller do right now," and a receipt answers "what should the operator believe later." Different audiences, different lifetimes, different storage. Once you see the split, you notice how many systems only serve the first audience, and how much operational archaeology consists of trying to reconstruct receipts from logs that were never designed to be them.

There's a quieter benefit too. Because every loss is receipted, an empty conflict table becomes a meaningful statement instead of a shrug. Silence from a system that provably speaks up is evidence. Silence from a system that might just be swallowing failures is noise. I wrote a whole separate piece on why my conflict table stays empty; the honest version of that claim rests entirely on the machinery described here.

## At five agents

None of this required distributed systems. No consensus, no replication, no vector clocks. One SQLite file, WAL mode, a handful of agents on one machine. The races are real anyway. Optimistic concurrency still has losers, processes still die between statements, retries still double-fire. The scale changed the size of the answer, a receipts table instead of a Raft cluster, but it didn't change the questions.

If you run anything with two or more writers, ask it the question this design keeps asking: when you lose someone's write, who finds out, and when, and what evidence do they get? "The caller got an error" is an answer about the next ninety seconds. Build for the operator three days out.
