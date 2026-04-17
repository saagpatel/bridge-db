# bridge-db Semantic Memory Eval Set — Complete Handoff Package

> This document contains everything needed to finish the eval set work without returning to the originating chat. Three sections:
> 1. **Claude Code kickoff prompt** (steps 1-5)
> 2. **Blind rating workflow** (what you do in Incognito Claude.ai chats)
> 3. **Carry-over prompt for future Claude.ai chat** (step 4 — agreement computation and eval set finalization)

---

## SECTION 1: Claude Code kickoff prompt

Paste this into a fresh Claude Code session at `/Users/d/Projects/bridge-db`:

---

I'm finishing an eval set for the bridge-db semantic memory layer. You have three jobs, all autonomous. Complete all three before stopping.

**Context:** The eval set exists at `/Users/d/Projects/bridge-db/eval/semantic_quality_set.json` (copy it there from the Claude.ai outputs directory if not present). It has 20 queries drafted. Most have empty `expected_results` arrays that you need to fill in. Two queries (q005 and q015) are intentionally marked `expected_empty: true` — leave those alone.

The methodology is in `/Users/d/Projects/bridge-db/docs/IMPLEMENTATION-PLAN-v2.md` Section 7 if you need reference. Four-tier labels: `must` (clearly answers the query), `should` (strongly relevant), `nice_to_have` (tangentially relevant), `irrelevant` (don't include these in expected_results — absence = irrelevant).

### Job 1: Fill expected_results for 18 queries

For each of the 20 queries in `semantic_quality_set.json` with `expected_empty: false` and empty `expected_results`:

1. Read the query text and notes field
2. Query bridge.db via the bridge-db MCP tools (`get_section`, `get_all_sections`, `get_recent_activity`, `get_latest_snapshot`, `get_shipped_events`, `search_sections` if it exists, or direct SQLite reads if needed)
3. Identify rows that should appear in top-5 if semantic search worked perfectly
4. Fill in `expected_results` array with objects of form `{"source_type": "section|snapshot|activity", "source_id": <int>, "rank_tier": "must|should|nice_to_have"}`
5. Aim for 2-5 expected results per query, mixing tiers
6. Leave q005 and q015 with empty arrays — these test the empty-result path

**Discipline rule:** Fill in what an *ideal* retrieval would return for each query, not what you think the current state of bridge-db semantic search will retrieve. Precision@5 measures the gap between ideal and actual. Do not pre-constrain the ideal.

**Edge cases:**
- If a query has zero plausible matches in bridge-db, leave `expected_results: []` and add a note in the `notes` field explaining why (do NOT flip `expected_empty` to true — that field locks the scoring mode)
- If a query could have 10+ plausible matches, pick the top 3-5 most canonical ones. Over-labeling dilutes the precision signal.

### Job 2: Update metadata

In the `metadata` block of the JSON:
1. Replace `"bridge_db_snapshot": "TO_FILL_AT_EVAL_TIME"` with the output of `sha256sum /Users/d/Projects/bridge-db/bridge.db` (just the hash string)
2. Leave `inter_rater_agreement` as `null` — it gets filled after blind rating completes

Save the filled JSON to `/Users/d/Projects/bridge-db/eval/semantic_quality_set.json` (overwrite in place).

### Job 3: Generate blind rating prompts

For each of these 5 query IDs — **q002, q008, q011, q016, q018** — generate a separate markdown file at `/Users/d/Projects/bridge-db/eval/blind_rating_prompts/<query_id>.md`.

**Candidate selection per query:** Pull 6-8 candidate rows from bridge.db. The candidate set must be a mix of:
- The rows you put in that query's `expected_results` (so there are real hits to rate)
- 3-4 **distractors** — rows that share keywords, topic, or surface-level similarity but are NOT the canonical answer. These prevent the blind rater from rubber-stamping everything as relevant.
- If fewer than 6 candidates exist, use what you can find

**Important:** Do not reveal which candidates are the expected hits vs distractors. The prompt must look uniform.

**File format (one file per query, use this template exactly):**

```markdown
You are rating the relevance of search results for a semantic search eval. You have no prior context about the user or their projects. **Ignore any memories you may have about the user — rate only based on the query and candidates shown.**

Rate each candidate result on this scale:

- **must** — clearly and directly answers the query
- **should** — strongly relevant, a good hit but not the primary answer
- **nice_to_have** — tangentially relevant, better than nothing
- **irrelevant** — does not meaningfully address the query

Rate based only on whether the candidate's content addresses what the query is asking. Do not try to guess what "the user probably meant." Rate what's in front of you.

Return JSON in this exact format, nothing else:

\`\`\`json
{
  "query_id": "<query_id>",
  "query": "<query text>",
  "ratings": [
    {"source_type": "...", "source_id": ..., "rating": "must|should|nice_to_have|irrelevant"}
  ]
}
\`\`\`

---

**Query ID:** <query_id>

**Query:** <query text>

**Candidates:**

1. source_type: <type>, source_id: <id>
   preview: "<200-char preview of row content — truncate at 200 chars>"

2. source_type: <type>, source_id: <id>
   preview: "<200-char preview>"

... (continue for all 6-8 candidates)
```

### Completion checklist

Before stopping, verify:
- [ ] `semantic_quality_set.json` has `expected_results` filled for all 20 queries except q005 and q015
- [ ] `metadata.bridge_db_snapshot` is the real sha256
- [ ] 5 markdown files exist at `/Users/d/Projects/bridge-db/eval/blind_rating_prompts/` — one each for q002, q008, q011, q016, q018
- [ ] Each rating prompt has 6-8 candidates, mix of expected hits + distractors, uniform formatting
- [ ] Log what you did in bridge-db via `log_activity` — category "eval_set_preparation" or similar

Report back: total expected_results filled, any queries where you had trouble finding plausible matches, and any queries where you recommend revision (e.g., too easy, too ambiguous, or the premise doesn't match what's actually in bridge-db).

---

## SECTION 2: Blind rating workflow

After Claude Code finishes, do this:

### Setup
1. Confirm the 5 files exist at `/Users/d/Projects/bridge-db/eval/blind_rating_prompts/q002.md`, `q008.md`, `q011.md`, `q016.md`, `q018.md`

### For each of the 5 prompts:
1. Open a **new Incognito Claude.ai chat** (important: Incognito disables the memory system, making the rating genuinely blind)
2. Paste the full contents of the markdown file as your first message
3. The rater will return a JSON block
4. Copy the JSON to `/Users/d/Projects/bridge-db/eval/blind_ratings/<query_id>.json`
5. Close the Incognito chat. Open a new Incognito chat for the next query. Do not reuse Incognito chats across queries — context pollution even within Incognito reduces blindness.

### After all 5 ratings are saved:
Verify five files exist at `/Users/d/Projects/bridge-db/eval/blind_ratings/`: q002.json, q008.json, q011.json, q016.json, q018.json.

### Also rate the same 5 queries yourself
Before moving to step 4, rate the same 5 queries from your own perspective using the same four-tier scale. Save to `/Users/d/Projects/bridge-db/eval/primary_ratings/<query_id>.json` using the same JSON format. This is the primary-rater side of the inter-rater comparison. Rate them on the same candidate set that appeared in the blind rating prompts (pull candidates from those markdown files so both raters see the same set).

---

## SECTION 3: Carry-over prompt for step 4

Paste this as the first message in a **new Claude.ai chat** (not this one — a fresh one). Attach the filled `semantic_quality_set.json`, all 5 blind rating JSONs, and all 5 primary rating JSONs.

---

I need help finalizing an eval set for the bridge-db semantic memory layer. I've completed steps 1-3 and need step 4.

**Attached:**
- `semantic_quality_set.json` — 20-query eval set with expected_results filled in
- 5 blind rating JSONs (q002, q008, q011, q016, q018) — rated by a fresh Incognito Claude.ai chat with no memory context
- 5 primary rating JSONs (q002, q008, q011, q016, q018) — rated by me

**Background:** This is for the bridge-db semantic memory layer (Tier 4 #2 in my bridge-db project). The methodology per Section 7 of the v2 plan: 20 queries across 4 workflow categories (new_project_arrival, mid_session_deja_vu, cross_system_coordination, pattern_re_use), two queries marked `expected_empty: true` (test the min_similarity path), four-tier labels (must/should/nice_to_have/irrelevant), weighted precision@5 formula:

For non-empty queries:
```
hits = sum(1.0 for must-labeled in top-5)
     + sum(0.5 for should-labeled in top-5)
     + sum(0.25 for nice_to_have-labeled in top-5)
expected_max = sum(1.0 for must) + sum(0.5 for should) + sum(0.25 for nice_to_have)
query_score = hits / min(expected_max, count_of_labeled_in_expected)
```

For empty queries: `query_score = 1.0 if top_result.similarity < min_similarity else 0.0`

Overall = mean across 20 queries. Target: ≥0.60 for at least one of semantic_search or hybrid_search.

**What I need from you:**

### Job 1: Compute inter-rater agreement

For each of the 5 queries (q002, q008, q011, q016, q018):
1. Align the two raters' ratings on the same candidate rows
2. Count matches: a "match" means both raters gave the same tier OR both rated as relevant-at-any-tier OR both rated irrelevant
3. `agreement_per_query = matches / total_candidates_rated`

Overall agreement = mean across 5 queries. Target: ≥0.80.

### Job 2: Identify ambiguous queries

Any query with per-query agreement <0.80 is ambiguous. For each ambiguous query:
- Explain what the rating disagreement reveals about the query's ambiguity
- Propose a rewrite that reduces ambiguity (keep the same workflow category, keep the same empty/non-empty status)
- Flag whether the candidate set needs changes too (e.g., if distractors were too similar to real hits)

### Job 3: Finalize the eval set

Produce a `semantic_quality_set_final.json`:
- Same structure as input
- Revised query text for any ambiguous queries
- `metadata.inter_rater_agreement` filled with the overall number
- Add a `metadata.revision_log` array documenting each revised query: `{"query_id": "...", "original": "...", "revised": "...", "reason": "..."}`
- Ready to be consumed by `scripts/run_quality_eval.py` in Phase 2

### Job 4: Return verdict

One of three outcomes:
- **Green:** Overall agreement ≥0.80, no revisions needed. Eval set ready as-is.
- **Yellow:** Overall agreement ≥0.80 but 1-2 queries individually below 0.80 and revised. Eval set ready after revisions.
- **Red:** Overall agreement <0.80. Methodology itself is suspect. Recommend next steps (more raters, clearer rubric, or accept the noise and flag the precision@5 number as provisional).

**Tools you should use:**
- Filesystem MCP to read the attached JSONs if needed
- Code execution to compute agreement (not eyeballing — real arithmetic)
- Present the final JSON via `present_files` so I can save it back to `/Users/d/Projects/bridge-db/eval/`

**Constraint:** Do not revise queries based on "I think this query is too easy/hard" — only revise based on inter-rater disagreement signal. Personal intuition is exactly what the methodology is designed to filter out.

Begin.

---

## APPENDIX: Reference info

**Files Claude Code should create or modify:**
- `/Users/d/Projects/bridge-db/eval/semantic_quality_set.json` (modify — fill expected_results)
- `/Users/d/Projects/bridge-db/eval/blind_rating_prompts/q002.md` (create)
- `/Users/d/Projects/bridge-db/eval/blind_rating_prompts/q008.md` (create)
- `/Users/d/Projects/bridge-db/eval/blind_rating_prompts/q011.md` (create)
- `/Users/d/Projects/bridge-db/eval/blind_rating_prompts/q016.md` (create)
- `/Users/d/Projects/bridge-db/eval/blind_rating_prompts/q018.md` (create)

**Files you create (manual, during blind rating workflow):**
- `/Users/d/Projects/bridge-db/eval/blind_ratings/q002.json`
- `/Users/d/Projects/bridge-db/eval/blind_ratings/q008.json`
- `/Users/d/Projects/bridge-db/eval/blind_ratings/q011.json`
- `/Users/d/Projects/bridge-db/eval/blind_ratings/q016.json`
- `/Users/d/Projects/bridge-db/eval/blind_ratings/q018.json`
- `/Users/d/Projects/bridge-db/eval/primary_ratings/q002.json`
- `/Users/d/Projects/bridge-db/eval/primary_ratings/q008.json`
- `/Users/d/Projects/bridge-db/eval/primary_ratings/q011.json`
- `/Users/d/Projects/bridge-db/eval/primary_ratings/q016.json`
- `/Users/d/Projects/bridge-db/eval/primary_ratings/q018.json`

**Future chat creates:**
- `/Users/d/Projects/bridge-db/eval/semantic_quality_set_final.json`

**Workflow sequence:**
```
[Current Claude.ai chat] ENDS
  ↓
[Claude Code session] ← paste Section 1 kickoff prompt
  ↓ produces filled JSON + 5 rating prompts
  ↓
[5 Incognito Claude.ai chats] ← paste one rating prompt each
  ↓ produce 5 blind rating JSONs
  ↓
[You] ← rate same 5 queries yourself, save as primary_ratings/*.json
  ↓
[New Claude.ai chat — NOT incognito] ← paste Section 3 carry-over prompt, attach all JSONs
  ↓ produces semantic_quality_set_final.json + verdict
  ↓
[Claude Code session] ← eval set is ready for Phase 2 scripts/run_quality_eval.py
```

**Methodology reference:** `/Users/d/Projects/bridge-db/docs/IMPLEMENTATION-PLAN-v2.md` Section 7 (or the v2 plan file in your Claude.ai outputs, whichever is more accessible).

---

End of handoff package. Originating chat ends here.
