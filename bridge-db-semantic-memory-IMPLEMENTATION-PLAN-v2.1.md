# bridge-db Semantic Memory Layer — Implementation Plan (v2.1)

> **⛔ CLOSED 2026-04-17 — Path B chosen. Phases 0/1/2 will not be built.**
>
> A post-Phase-−1 dry-run against the live DB through the 20-query eval set
> exposed that **12 of 20 queries miss not because FTS5 is too coarse, but
> because the queried content isn't in `bridge.db` at all.** The missing content
> lives in memory files (`~/.claude/projects/-Users-d/memory/*.md`), git-tracked
> plan docs, or Notion — scopes outside bridge-db's current mission. Vector
> search can't find what isn't indexed, so Phases 0–1 as written would consume
> ~2 weeks to produce near-identical hit rates to the FTS5 layer already shipped.
>
> **Decision:** keep bridge-db's scope as cross-system *state* coordination
> (handoffs, snapshots, activity, four Claude.ai-owned context sections).
> Ship Phase −1 as the final layer. If unified recall across memory / plans /
> Notion becomes a priority later, it's a separate project, not an extension
> of bridge-db.
>
> **Kept as frozen historical record** (do NOT treat as live TODOs):
> this document, the v2 predecessor, `eval-set-handoff-package.md`, and
> `semantic_quality_set.json`. Current project state is in `CLAUDE.md` and
> `ROADMAP.md`.
>
> ---

> **Revision notes (v2.1, 2026-04-17):** Rewritten from v2 after (a) agreeing to the "take all six" set of design changes in discussion, and (b) a full ground-truth pass over the current repo that invalidated several v2 assumptions. v2 is superseded — not amended. The changes are substantial enough that a diff would mislead.

## What changed vs v2

Six design decisions were taken before this rewrite:

1. **Current-model-only default** for search; cross-model merging reserved for migration debug, via reciprocal rank fusion rather than raw-score merging.
2. **sqlite-vec as a Phase 0 go/no-go gate**, not a fallback. chromadb is not a fallback — it would be a rewrite.
3. **aiosqlite + extension loading must be verified in Phase 0**, with an explicit Python-build requirement documented if it works.
4. **Local embeddings via Ollama** as the default (nomic-embed-text-v1.5 or bge-large-en-v1.5). Voyage AI dropped. No API key. No Keychain.
5. **Phase −1 baseline week** — FTS5 + a `recall(query)` tool first. Semantic layer only if the baseline demonstrably misses queries.
6. **SLA relaxed to 1500ms**; LRU query-embedding cache deleted. Local inference makes both moot.

## Ground-truth corrections to v2

After reading every source file, running the interpreter, and inspecting the live DB, these v2 claims are wrong and have been corrected throughout this document:

| v2 claim | Ground truth |
|---|---|
| "16 tools, 65 tests" | **19 tools, 104 tests** |
| "likely 500–2000 rows" to embed | **64 rows total** (4 sections + 54 activity + 4 snapshots + 2 handoffs). Retention caps put the long-run ceiling at **~280 rows** |
| sqlite-vec is a LOW risk | **sqlite-vec is blocked today**: `sqlite3.Connection.enable_load_extension` raises `AttributeError` on the current uv-managed Python 3.12.0 build. It was compiled without `--enable-loadable-sqlite-extensions` |
| Section source_id is `INTEGER` | `context_sections.section_name` is `TEXT PRIMARY KEY` — source_id must be `TEXT` for sections and `INTEGER` for activity/snapshots/handoffs |
| Migration file naming `008_semantic_memory.sql` | Repo has no migrations directory. Schema is inline in [db.py](src/bridge_db/db.py) via `SCHEMA_VERSION` integer + an in-function migration block. Semantic-layer schema becomes `SCHEMA_VERSION = 3` |
| Write paths: `update_section`, `log_activity`, `save_snapshot` | **Also:** `sync_from_file` (bulk upsert of sections), `mark_shipped_processed` (UPDATE activity tags), `create_handoff` / `pick_up_handoff` / `clear_handoff`, `codex_seed.apply_manifest` (direct INSERT bypassing tools), `migration.migrate_from_markdown` (bulk bootstrap INSERT), and the auto-prune DELETE inside `log_activity` and `save_snapshot` |
| Content types: sections, snapshots, activity | **Also: handoffs.** Short free-text rows with high "have I dispatched this before" recall value |
| 41 new tests needed | ~23 new tests with the simpler design (see Section 6) |
| 5 new MCP tools | **2 tools** (`semantic_search`, `reindex`), with an optional third (`recall`) surviving from Phase −1 if it proves useful |

Scale note: 64 current rows × 768-dim float32 = **192 KB** of vector data. A full linear-scan cosine pass is ~50K float ops — sub-millisecond on M4 Pro. sqlite-vec's index is solving a problem this project does not have.

## The "should we even build this" question

At 64 rows (ceiling ~280), FTS5 + "feed matching rows to Claude and ask" covers most of the workflow value. Phase −1 exists specifically to answer whether the semantic layer is worth adding at all, or whether FTS5 + `recall` is the shipping artifact. That decision happens with data, not guesswork. See Section 8.

---

## Section 1: EXEC SUMMARY

### 1a. What we're building

A recall layer over bridge-db's existing SQLite content. Starts as FTS5 keyword search plus a `recall(query)` tool that returns matching rows for an LLM caller to synthesize. **Optionally** extended with a local-embedding vector layer if Phase −1 data shows FTS5 misses workflow queries. Embeddings, if added, are stored as `BLOB` in a plain table (not via sqlite-vec) and queried via Python linear scan — the corpus is too small to justify a vector index. Embeddings are generated locally via Ollama (`nomic-embed-text-v1.5`, 768-dim). No API key, no network dependency, no Keychain integration. Content types covered: **context_sections, activity_log, system_snapshots, pending_handoffs** (cost_records and the shipped-events overlay are excluded — structured/numeric, low recall value).

### 1b. Riskiest parts, updated

The v2 risk list mostly evaporated. What remains:

**Risk 1: Phase −1 reveals the semantic layer isn't worth building (Severity: NEUTRAL).** This is the explicit goal of Phase −1, not a risk. If FTS5 + `recall` handles the workload, we ship that and stop. Outcome-favorable either way.

**Risk 2: Local embedding quality is below threshold on the eval set (Severity: MEDIUM).** `nomic-embed-text-v1.5` benchmarks ~85–90% of `voyage-3-large` on MTEB. At 64 rows the gap may be invisible. If eval misses 0.6 weighted precision@5: (a) try `bge-large-en-v1.5` (1024-dim, often better on short texts), (b) try query expansion via Claude paraphrases, (c) accept that semantic search isn't the right tool and ship FTS5 + `recall`.

**Risk 3: Ollama daemon not running at query time (Severity: LOW).** Easy detection (HTTP 11434 refused → fall back to FTS5). Document the dependency in CLAUDE.md. No data loss, graceful degradation.

**Risk 4: Embedding-to-source drift from missed invalidation paths (Severity: MEDIUM).** The repo has more write paths than v2 named. Solution: rather than hook every write path, run a background **reconciler** on `reindex` that walks source tables, computes content hashes, and re-embeds any row where the hash changed. Idempotent, cheap (64 rows), and catches paths we forget.

**Risk 5: sqlite-vec Python-build issue comes back to bite us if we later need an index (Severity: LOW).** Documented as a known limitation. If the corpus ever grows past ~10k rows (it won't — retention caps prevent this) we'd revisit the Python build, but this isn't a near-term concern.

### 1c. Shortest path to daily personal use

- **Phase −1 (Week 1):** FTS5 + `recall(query)` tool. Dogfood for a week. Log queries, note misses. **Hard gate:** go/no-go on the semantic layer based on this data.
- **Phase 0 (~1 day, conditional):** If Phase −1 says go — schema migration (SCHEMA_VERSION 2→3), Ollama health check, embedder wrapper, ~10-query smoke test on an ad-hoc corpus.
- **Phase 1 (Week 2, conditional):** Backfill + `semantic_search` + reconciler + eval.
- **Phase 2 (Week 3, conditional):** Hybrid FTS5 + vector fusion only if semantic-alone misses threshold. Otherwise skipped.

**Shortest path to value: End of Week 1** — regardless of which layer lands. The Phase −1 artifact (FTS5 + `recall`) is useful on its own.

---

## Section 2: REVIEW GATE (SPEC LOCK)

### 2a. Goal

Add a recall layer to bridge-db that lets any of the three connected systems (Claude.ai, Claude Code, Codex) retrieve semantically or lexically relevant past content across context_sections, activity_log, system_snapshots, and pending_handoffs. The layer degrades gracefully — if Ollama is unavailable, FTS5 still works; if semantic is skipped entirely, FTS5 still works.

### 2b. Success metrics

1. **Phase −1 ships.** FTS5 + `recall` is usable from Claude Code, Claude.ai, and Codex within 1 week.
2. **Phase −1 → Phase 1 decision is data-driven.** After a dogfooding week, decision is documented in CLAUDE.md with reference to a query log.
3. **If semantic layer ships:** weighted precision@5 ≥ 0.6 on the 20-query eval set for at least one of `semantic_search` / `hybrid_search`.
4. **Latency:** p95 < 1500ms end-to-end on M4 Pro. Cold and warm are no longer distinguished — local inference collapses the two.
5. **No regressions:** all 104 existing tests continue to pass. Existing 19 tools remain unchanged.
6. **Adoption proxy:** at least 5 recall/search invocations per week across the three systems in the first month after the primary layer (whichever it is) ships.

### 2c. Hard constraints

1. **No new datastores.** Everything lives in `~/.local/share/bridge-db/bridge.db`.
2. **Existing 19 tools and 104 tests must continue to pass unchanged.** The recall layer is additive.
3. **No API keys required at runtime.** All embedding happens locally via Ollama.
4. **The recall layer must degrade gracefully.** If Ollama is down, FTS5 path returns. If FTS5 doesn't exist yet (pre Phase −1), existing tools still work.
5. **New tools follow the existing naming convention:** snake_case, action verb, single-module registration.
6. **Schema changes follow the existing pattern.** Increment `SCHEMA_VERSION`, add DDL inline in [db.py](src/bridge_db/db.py), write an in-place migration from the previous version. No new migrations directory.
7. **No embedding of credentials.** The pre-embed scrubber from v2 carries over (see Section 5).
8. **No implicit write-path hooks.** Reconciler-based invalidation, not per-write hooks. Simpler to reason about; catches unseen write paths.

### 2d. Locked decisions

**Decision 1: Recall architecture.** FTS5 is primary. Vector layer is conditional on Phase −1 outcome. Hybrid mode is conditional on Phase 1 outcome. **Rationale:** at 64–280 rows, FTS5 may be sufficient. Each added layer must justify itself.

**Decision 2: Embedding model.** Default `nomic-embed-text-v1.5` via Ollama (768-dim). Fallback `bge-large-en-v1.5` (1024-dim) if the default underperforms on the eval set. **Rationale:** local-first, no API key, no network dependency, no cross-model drift risk.

**Decision 3: Vector storage.** Plain SQLite table, `vector` column as `BLOB` (768 float32 = 3072 bytes). Linear scan in Python via numpy for search. **No sqlite-vec.** **Rationale:** sqlite-vec is blocked on this Python and unnecessary at this scale. Storing as BLOB keeps the "one SQLite file" property that makes bridge-db portable.

**Decision 4: Distance metric.** Cosine similarity, computed in Python. Embeddings pre-normalized at write time, so cosine == dot product. **Rationale:** standard; enables fast `numpy.dot(matrix, query_vec)` over the full corpus.

**Decision 5: Schema version.** `SCHEMA_VERSION = 3`. Migration from v2: add `content_index` (FTS5 virtual table), `embeddings` table, `embedding_reconcile_state` table. **Rationale:** matches the existing in-function migration pattern.

**Decision 6: Source type + source id encoding.** `source_type TEXT CHECK IN ('section', 'activity', 'snapshot', 'handoff')`, `source_id TEXT` (yes, TEXT for all — because sections use `section_name`, while others use stringified integer IDs). **Rationale:** one column type simplifies the schema; the cast cost is negligible.

**Decision 7: Chunking.** **No chunking in v2.1.** All four content types have per-row content that fits within nomic-embed-text's 8192-token context. `system_snapshots` JSON is ~1768 chars avg (well under limit). Revisit only if eval fails on snapshot-type queries specifically. **Rationale:** content fits; chunking adds complexity for no proven gain.

**Decision 8: Invalidation strategy.** **Reconciler, not hooks.** A `reindex()` tool walks every source table, computes content hash per row, and re-embeds any row where the stored hash differs from the live hash. Runs on demand, cheap (64 rows × 3072 bytes × local embed = ~2 seconds). **Rationale:** v2 tried to hook every write path; repo has more write paths than v2 enumerated, and new ones will appear. Reconciler catches everything without requiring discipline at each write site.

**Decision 9: Hybrid search (conditional).** If shipped in Phase 2, use weighted RRF with k=60, top-20 truncation per list (not 50 — the corpus is 64 rows), missing-list contribution = 0. `combined_score(d) = vector_weight × (1/(60 + rank_vec(d))) + (1 − vector_weight) × (1/(60 + rank_fts(d)))`. Default `vector_weight = 0.5`, clamped [0, 1]. **Rationale:** same canonical RRF formula as v2, shrunk truncation to match corpus scale.

**Decision 10: Cross-model migration.** Current-model-only by default. If the model pin is changed, a migration CLI runs blocking re-embed. During the re-embed window, searches still work (just against the old-model corpus until the switchover commits). No cross-model merging in normal operation; RRF-over-models exists as a `--debug-cross-model` flag on `semantic_search` only. **Rationale:** cosine similarity isn't comparable across embedding spaces; merging by raw score is mathematically unsound. Better to block on migration and keep ranking meaningful.

**Decision 11: Pre-embed scrubber.** Ported verbatim from v2 (13 credential patterns, non-blocking, ordered specific-first). **Rationale:** defense in depth. Even though bridge-db shouldn't contain credentials, a regression elsewhere shouldn't leak to an embedding store (even a local one).

---

## Section 3: ARCHITECTURE

### 3a. System diagram

```
                  ┌─────────────────────────────────────┐
                  │   Claude.ai / Claude Code / Codex   │
                  └──────────────────┬──────────────────┘
                                     │ recall / semantic_search / hybrid_search
                                     ↓
                  ┌─────────────────────────────────────┐
                  │   bridge-db MCP server (existing)   │
                  │          FastMCP, stdio             │
                  └──────────────────┬──────────────────┘
                                     │
              ┌──────────────────────┴──────────────────────┐
              │                                             │
         Phase −1 (always)                        Phases 1–2 (conditional)
              │                                             │
┌─────────────┴──────────────┐          ┌───────────────────┴───────────────────┐
│  FTS5 + recall tool        │          │  Local embedder (Ollama)              │
│  - content_index vtable    │          │  - nomic-embed-text-v1.5              │
│  - MATCH queries           │          │  - scrubber                           │
│  - rows returned to caller │          │  - embeddings table (BLOB)            │
│                            │          │  - linear-scan cosine in Python       │
│                            │          │  - reconciler for invalidation        │
└─────────────┬──────────────┘          └───────────────────┬───────────────────┘
              │                                             │
              └──────────────────────┬──────────────────────┘
                                     ↓
                  ┌─────────────────────────────────────┐
                  │   bridge.db (SQLite, existing)      │
                  │   - context_sections (existing)     │
                  │   - activity_log (existing)         │
                  │   - system_snapshots (existing)     │
                  │   - pending_handoffs (existing)     │
                  │   - cost_records (existing)         │
                  │   + content_index (FTS5, new)       │
                  │   + embeddings (BLOB table, new)    │
                  │   + embedding_reconcile_state (new) │
                  └─────────────────────────────────────┘

    Ollama (localhost:11434) ←── embed requests (batched) ──┘
```

### 3b. Tech stack

- Python 3.12+ (existing pyproject constraint).
- aiosqlite (existing).
- numpy — new dependency for vector math. Small, widely available, no drama.
- httpx — new dependency for Ollama HTTP calls. Or use stdlib `urllib` if we want to avoid adding a dep; httpx is nicer and aligns with async style.
- No sqlite-vec. No voyageai. No keyring.
- Ollama (system dependency, already used elsewhere in the user's stack per MCP list).

### 3c. File structure

```
bridge-db/
├── src/bridge_db/
│   ├── db.py                         # MODIFIED — SCHEMA_VERSION 2→3, v2→v3 migration block
│   ├── models.py                     # MODIFIED — add SourceType literal, SourceID validator
│   ├── tools/
│   │   ├── recall.py                 # NEW — Phase −1: recall, fts_search
│   │   └── semantic.py               # NEW — Phase 1–2: semantic_search, reindex, (conditional) hybrid_search
│   └── semantic/                     # NEW module, only populated in Phase 1
│       ├── __init__.py
│       ├── types.py                  # Dataclasses, Literal aliases
│       ├── embedder.py               # Ollama HTTP client, batching, scrubber invocation
│       ├── scrubbing.py              # 13-pattern credential scrubber (ported from v2)
│       ├── store.py                  # embeddings + embedding_reconcile_state CRUD
│       ├── search.py                 # Linear-scan cosine, scope filter, merge
│       ├── hybrid.py                 # (Phase 2) weighted RRF fusion
│       └── reconciler.py             # Walks source tables, compares content_hash, re-embeds drift
├── tests/
│   ├── test_recall.py                # NEW — Phase −1
│   └── semantic/                     # NEW — Phase 1–2
│       ├── test_embedder.py
│       ├── test_scrubbing.py
│       ├── test_store.py
│       ├── test_search.py
│       ├── test_hybrid.py            # Phase 2
│       └── test_reconciler.py
├── eval/
│   └── semantic_quality_set.json     # Existing; filled during Phase 1
├── scripts/
│   └── run_quality_eval.py           # Phase 1
└── bridge-db-semantic-memory-IMPLEMENTATION-PLAN-v2.1.md   # this file
```

### 3d. Data model

Migration v2 → v3, added inline to [db.py](src/bridge_db/db.py):

```sql
-- 1. FTS5 contentless virtual table indexing relevant source columns.
-- Contentless means the FTS index references rowids of the source tables, not duplicated content.
CREATE VIRTUAL TABLE IF NOT EXISTS content_index USING fts5(
    source_type UNINDEXED,        -- 'section' | 'activity' | 'snapshot' | 'handoff'
    source_id UNINDEXED,          -- TEXT; section_name for sections, stringified id otherwise
    text,                         -- the searchable content (scrubbed)
    tokenize = 'porter unicode61 remove_diacritics 2'
);

-- 2. Flat embeddings table. No sqlite-vec. BLOB vector stored pre-normalized.
CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL CHECK(source_type IN ('section','activity','snapshot','handoff')),
    source_id TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    vector BLOB NOT NULL,                         -- float32 little-endian, pre-normalized
    content_hash TEXT NOT NULL,                   -- SHA256 of scrubbed embedded text
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE(source_type, source_id, embedding_model)
);
CREATE INDEX IF NOT EXISTS idx_embeddings_source ON embeddings(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(embedding_model);

-- 3. Reconciler state. One row per source table; last_reconciled_at lets the reconciler
--    short-circuit when there have been no writes since the last pass.
CREATE TABLE IF NOT EXISTS embedding_reconcile_state (
    source_type TEXT PRIMARY KEY CHECK(source_type IN ('section','activity','snapshot','handoff')),
    last_reconciled_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_model TEXT,
    rows_embedded INTEGER NOT NULL DEFAULT 0
);
```

### 3e. Type definitions

```python
# src/bridge_db/semantic/types.py
from dataclasses import dataclass
from typing import Literal

SourceType = Literal["section", "activity", "snapshot", "handoff"]
SearchScope = Literal["all", "section", "activity", "snapshot", "handoff"]

@dataclass
class SearchResult:
    source_type: SourceType
    source_id: str                        # TEXT — section_name or str(id)
    similarity: float                     # cosine in [0, 1]; higher is better
    content_preview: str                  # first 200 chars of source content
    full_content_uri: str                 # bridge-db://<source_type>/<source_id>
    metadata: dict                        # source-type-specific extras

@dataclass
class RecallResult:
    source_type: SourceType
    source_id: str
    snippet: str                          # FTS5 snippet() output with BM25 context
    bm25_score: float                     # lower is better in SQLite's rank
    full_content_uri: str

@dataclass
class HybridSearchResult(SearchResult):
    vector_rank: int | None
    fts_rank: int | None
    combined_score: float

@dataclass
class ReindexReport:
    rechecked: int
    re_embedded: int
    deleted_orphans: int
    duration_ms: int
```

### 3f. Tool contracts

```python
# Phase −1 — always shipped
async def recall(
    query: str,
    limit: int = 10,
    scope: SearchScope = "all",
) -> list[RecallResult]:
    """FTS5 keyword search across bridge-db content. Limit clamped to [1, 50]."""

# Phase 1 — conditional
async def semantic_search(
    query: str,
    scope: SearchScope = "all",
    limit: int = 5,
    min_similarity: float = 0.0,
) -> list[SearchResult]:
    """Vector search via local embedder + linear-scan cosine. Limit clamped [1, 20]."""

async def reindex(
    scope: SearchScope = "all",
    force: bool = False,
) -> ReindexReport:
    """
    Walk source tables, re-embed rows whose content_hash changed, delete orphans.
    force=True re-embeds everything (used on model change).
    """

# Phase 2 — conditional on semantic-alone missing eval threshold
async def hybrid_search(
    query: str,
    scope: SearchScope = "all",
    limit: int = 5,
    vector_weight: float = 0.5,
) -> list[HybridSearchResult]:
    """Weighted RRF fusion of semantic_search and recall (FTS5)."""
```

**Source-row URI scheme:** `bridge-db://<source_type>/<source_id>` — opaque to the client, but bridge-db can expose a resolver later if needed.

### 3g. Ollama interaction

```python
# semantic/embedder.py — sketch
import httpx

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "nomic-embed-text:v1.5"

async def embed(texts: list[str], model: str = DEFAULT_MODEL) -> list[list[float]]:
    scrubbed = [scrubbing.scrub(t) for t in texts]
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Ollama's /api/embeddings is single-input per call in current API.
        # Batch by parallelism, not by single-request batching.
        results = await asyncio.gather(
            *[client.post(f"{OLLAMA_URL}/api/embeddings", json={"model": model, "prompt": t}) for t in scrubbed]
        )
    vectors = [r.json()["embedding"] for r in results]
    return [normalize(v) for v in vectors]
```

**Caveat:** Ollama's HTTP API embeds one text per call. At 64 rows with ~20ms per embed locally, a full backfill is ~1.3s. Parallelism (e.g., 8 concurrent) reduces this to under 200ms. Worth noting but not an optimization target.

### 3h. Dependencies

```bash
# Runtime additions to pyproject.toml:
uv add numpy httpx

# No voyageai, no keyring, no sqlite-vec.

# System dependency (not added by uv):
brew install ollama
ollama pull nomic-embed-text:v1.5
ollama serve   # or set up launchd plist for auto-start
```

---

## Section 4: PHASED IMPLEMENTATION

### Phase −1: FTS5 baseline + dogfood week (Week 1)

**Objectives**
- FTS5 `content_index` virtual table created via a SCHEMA_VERSION 2→3 migration.
- Initial population script walks existing rows and inserts them into `content_index`.
- Tools `recall(query)` and an internal `fts_search` helper exist.
- Recall is logged so the Phase 0 decision is data-driven.
- Existing 19 tools and 104 tests continue to pass.

**Tasks**
1. Bump `SCHEMA_VERSION = 3` in [db.py](src/bridge_db/db.py). Add v2→v3 migration creating only `content_index`. **Acceptance:** fresh DB applies v3 DDL; existing v2 DB runs v2→v3 migration; v2 test suite still passes.
2. Build `repopulate_content_index(db)` that walks sections, activity, snapshots (JSON-dump `data`), handoffs. **Acceptance:** after running against live bridge.db, `SELECT COUNT(*) FROM content_index` returns 64.
3. Hook `content_index` writes into `update_section`, `log_activity`, `save_snapshot`, `create_handoff`, and the delete paths where applicable. **Acceptance:** row inserted via any tool is findable by `MATCH` immediately.
4. Build `src/bridge_db/tools/recall.py` registering `recall(query, limit, scope)` MCP tool. Uses FTS5 `MATCH` with `snippet()` and `bm25()`, joins source tables for previews. **Acceptance:** `recall("bridge-db")` returns results from all four source types.
5. Append each `recall` call to `recall_query_log.jsonl` under the audit log directory. No schema change. **Acceptance:** every call produces one line.
6. Write `tests/test_recall.py`: 5 tests — happy path, empty result, scope filter, limit clamping, populator idempotence. **Acceptance:** suite → 109.
7. Update [CLAUDE.md](CLAUDE.md) with new tool count (19 → 20) and recall note.
8. **Dogfood for 7 calendar days.** Log into `last-session.md` any query where you mentally thought "semantic search would've caught this."

**Phase Verification Checklist**
- [ ] `uv run pytest` → 109 pass. `pyright` + `ruff check` clean.
- [ ] v2→v3 migration tested on a backup of live DB.
- [ ] `recall("handoff")` from Claude Code returns ≥1 handoff row.
- [ ] `recall_query_log.jsonl` has ≥14 entries by end of week.

**Go/No-Go gate for Phase 0:**
- **≤ 2 missed queries** → ship Phase −1 as final. Skip Phases 0–2. Note in CLAUDE.md.
- **3–5 missed queries** → borderline; proceed but treat eval threshold as the real gate.
- **> 5 missed queries** → clear signal; proceed confidently.

### Phase 0: Validation spikes (~1 day, conditional)

1. Install Ollama; pull `nomic-embed-text:v1.5`; verify `/api/embeddings` returns a 768-dim array.
2. Throwaway spike: embed all 64 rows, run 5 test queries, eyeball top-5. **Acceptance:** ≥4 of 5 queries return ≥1 subjectively relevant result.
3. Benchmark linear-scan cosine over 64 × 768. **Acceptance:** < 5ms on M4 Pro.
4. If nomic results feel weak, pull `bge-large-en-v1.5` and re-run. Commit to winner. Update Decision 2.

### Phase 1: Vector layer + reconciler + eval (Week 2, conditional)

**Objectives**
- `embeddings` + `embedding_reconcile_state` added via SCHEMA_VERSION 3→4.
- All rows embedded locally.
- `semantic_search` and `reindex` tools ship.
- Scrubber live.
- Eval weighted precision@5 ≥ 0.6.

**Tasks**
1. Bump `SCHEMA_VERSION = 4`; add v3→v4 migration block. **Acceptance:** round-trips on fresh and existing DBs.
2. `semantic/scrubbing.py` — port 13 patterns from v2 Section 5. **Acceptance:** 6 tests pass.
3. `semantic/embedder.py` — async Ollama client, concurrency 8, L2-normalize, `OllamaUnavailable` on connection error. **Acceptance:** 3 tests pass (happy path, 500 → raises, scrubber invoked pre-embed).
4. `semantic/store.py` — `upsert_embedding`, `delete_embedding`, `list_by_source`, `fetch_all_vectors(model)` → numpy. **Acceptance:** 4 tests pass.
5. `semantic/search.py` — embed query, `numpy.dot(matrix, q)`, scope filter, min_similarity, top-k. **Acceptance:** 4 tests pass.
6. `semantic/reconciler.py` — walk source tables, hash scrubbed content, diff vs `embeddings.content_hash`, re-embed deltas, drop orphans. Update `embedding_reconcile_state`. **Acceptance:** 3 tests pass (initial embed, row changed → re-embed, row deleted → orphan deleted).
7. Register `semantic_search` + `reindex` in `tools/semantic.py`. Total: 22 tools (19 + 1 Phase −1 + 2 Phase 1).
8. Run initial `reindex()`. **Acceptance:** 64 rows embedded; `embedding_reconcile_state` populated.
9. Fill `eval/semantic_quality_set.json` via the existing handoff package. Run blind rating on 5 queries. Revise any with <80% agreement.
10. `scripts/run_quality_eval.py` — run every query, compute weighted precision@5. **Acceptance:** script prints aggregate number.

**Phase Verification Checklist**
- [ ] `uv run pytest` → ~129 pass.
- [ ] `semantic_search("have I done cloudkit before")` returns ≥1 relevant row.
- [ ] `reindex()` on second call reports `re_embedded: 0` (idempotent).
- [ ] Eval weighted precision@5 ≥ 0.6.
- [ ] p95 over 20 queries < 1500ms.

**Go/No-Go gate for Phase 2:** If semantic-only hits ≥0.6, skip Phase 2. If semantic misses but FTS5 is strong, Phase 2 justified. If both < 0.6, investigate before adding complexity.

### Phase 2: Hybrid RRF (Week 3, conditional)

1. `semantic/hybrid.py` — parallel vector + FTS, top-20 per list, Decision 9 RRF. **Acceptance:** 3 tests pass (weight=0.5, 1.0, 0.0).
2. Register `hybrid_search`. Total: 23 tools.
3. Re-run eval; document delta in CLAUDE.md.

**Phase Verification Checklist**
- [ ] Suite → ~132.
- [ ] Hybrid precision@5 ≥ 0.6 (or ≥ semantic-only).
- [ ] Same query in semantic vs hybrid returns visibly different ordering.

### Phase 3: dropped

No rerank, no telemetry, no analytics. Corpus is too small to justify.

---

## Section 5: SECURITY & CREDENTIALS

- **No API keys.** Local embeddings via Ollama eliminate the Voyage key, the Keychain integration, and the `bridge-db-setup` CLI from v2. This whole attack surface is gone.
- **Data boundary.** Embedding requests leave the bridge-db process only to reach `127.0.0.1:11434` (Ollama). They do not leave the machine. Verify Ollama is configured for local-only (default; no remote binding).
- **Encryption at rest.** bridge.db lives on FileVault-encrypted disk. Embeddings inherit that protection; no application-level encryption added.
- **Pre-embed scrubber.** Ported verbatim from v2 Section 5: 13 credential patterns, non-blocking, ordered specific-first, content-hash computed on scrubbed text so the hash is stable across runs. Pattern set:
  - aws_access_key_id / aws_secret_access_key / github_token / github_pat / slack_token / google_api_key / stripe_secret / anthropic_key / openai_key / voyage_key / jwt / pem_private_key / generic_sk_key (last — specific `sk-*` variants win first).
  - Non-blocking: on match, replace with `[REDACTED]`, log WARNING with pattern name + count (never the matched content), proceed.
- **Defense in depth.** bridge-db should not contain credentials. Scrubber exists to catch regressions; if it ever fires on real content, audit the source of the write, not the scrubber.
- **Ollama availability.** If `127.0.0.1:11434` is refused, `semantic_search` returns a structured error indicating fallback to `recall` (FTS5). bridge-db does not attempt to start Ollama.

---

## Section 6: TESTING STRATEGY

Existing suite: **104 tests**. New tests by phase:

| Phase | Module | Tests |
|---|---|---|
| −1 | `tests/test_recall.py` | 5 |
| 1 | `tests/semantic/test_scrubbing.py` | 6 |
| 1 | `tests/semantic/test_embedder.py` | 3 |
| 1 | `tests/semantic/test_store.py` | 4 |
| 1 | `tests/semantic/test_search.py` | 4 |
| 1 | `tests/semantic/test_reconciler.py` | 3 |
| 2 | `tests/semantic/test_hybrid.py` | 3 |

**Total new: 28 tests.** Final suite after Phase 2: **132 tests**.

All tests follow the existing `CaptureMCP` + async fixture pattern in [tests/conftest.py](tests/conftest.py). Real SQLite in tmp_path — no mocks of the DB layer. Ollama is mocked at the HTTP boundary via `httpx.MockTransport` — no real network calls from tests.

**Correctness probes (manual, not in suite):**
- After Phase 1 backfill, pick 5 ad-hoc queries, verify top-1 is reasonable.
- After reconciler run on an unchanged corpus, verify `re_embedded == 0`.
- After editing a section, verify next `reindex()` re-embeds that section and only that section.

---

## Section 7: EVAL METHODOLOGY (delta from v2 Section 7)

**Keeping v2 Section 7 verbatim** for: query sourcing, writing discipline, 4-tier labels, weighted precision@5 formula, inter-rater protocol, schema. The existing `eval/semantic_quality_set.json` and handoff package are valid and should be executed as written.

**Changes from v2:**
1. The handoff package references `docs/IMPLEMENTATION-PLAN-v2.md`. Either copy this v2.1 file to `docs/IMPLEMENTATION-PLAN.md` or update the handoff to reference this file at repo root.
2. Content types expand to include **handoffs**. When Claude Code fills `expected_results`, any query where a handoff row is a plausible match should be labeled accordingly. Update the handoff package's Job 1 context to enumerate all four source types.
3. Threshold stays at 0.6 weighted precision@5 — it was set pre-Voyage-drop but is model-agnostic. If local embedding misses, revisit model choice (Decision 2 fallback), not the threshold.
4. **Temporal queries (q011, q012, q014) — locked:** keep the queries in. Accept that pure semantic will underperform on them; that underperformance is the honest signal that hybrid + a recency filter may be needed. `run_quality_eval.py` must report **aggregate weighted precision@5 AND a break-out of the 3 temporal queries** so their impact on the aggregate is visible, not hidden. The 0.6 gate applies to the aggregate; if the temporals are what's blocking it, that's signal for Phase 2 (hybrid) rather than for removing the queries.
5. q006 (`why did we pick sqlite-vec over chromadb`) — v2.1 flips the rationale. Keep the query; the answer in bridge-db's context sections will reflect the updated decision if and when it's written there.

---

## Section 8: OPEN QUESTIONS & HONEST RISKS

### Resolved decisions (pre-Phase −1)

1. **Eval set timing — locked:** runs as a Phase 1 validation task (Phase 1, Task 9–10), not Phase 2. The handoff package's methodology is unchanged; only the phase it fires in moves.
2. **Dogfood week timing — locked:** 7 calendar days of real workflow use. No synthesized queries. Short week is better than a padded week with fake data.
3. **"Missed query" definition — locked:** a query counts as missed if either (a) you had to mentally rephrase it multiple times to get FTS5 to return the right row, or (b) you knew the content existed but FTS5 returned a wrong row higher than the right one. The definition is written to `last-session.md` on day 1 and not amended retroactively.

### Known weak spots

1. **Temporal queries in the eval set (q011/q012/q014) probe something neither FTS5 nor pure cosine handles well.** Flagged in Section 7 delta. The eval will likely score low on these regardless of embedder choice.
2. **Reconciler trigger — locked:** on-demand via `reindex()`, with a **staleness warning** surfaced in `semantic_search` results when the last reconcile is >24h old (read from `embedding_reconcile_state.last_reconciled_at`). No launchd cron (adds moving parts). No piggyback on `mark_shipped_processed` (brittle coupling). The warning appears in `SearchResult.metadata` as `{"reconcile_stale_hours": <n>}` — callers can display it or ignore it. Bulk write paths (`sync_from_file`, `migration.migrate_from_markdown`, `codex_seed.apply_manifest`) call `reindex()` explicitly before returning.
3. **Ollama as a dependency introduces a "daemon running?" preflight.** bridge-db currently has no external daemon dependencies. Adding one is a real deployment consideration. Mitigation: graceful FTS5 fallback; document in CLAUDE.md.
4. **If the user wipes `~/.local/share/bridge-db/bridge.db`, embeddings are lost.** Acceptable — a full reindex takes ~2 seconds at 64 rows. Note in CLAUDE.md.

### Honest "is this worth it" check

With 64 rows and a ~280 ceiling, the semantic layer is defensible but not obviously necessary. Phase −1 is the acid test. If FTS5 + `recall` catches most queries, ship that and move on. Do not let Phase 1 momentum override the Phase −1 signal. The most expensive outcome is building Phases 1–2 to find that they provide marginal benefit over Phase −1 at the cost of Ollama + numpy + reconciler complexity.

**Default bias: ship less.** If the decision is a coin flip at the Phase −1 gate, skip to shipping Phase −1 alone.

### Red flags in v2 that are NOT yet resolved here

- The existing `eval/semantic_quality_set.json` has queries referencing projects (AssistSupport, Command Center v6, AutoProjectDispatch, bloomberry count 1482) whose content may not exist in the live bridge.db. When Claude Code fills `expected_results`, some queries may legitimately have no plausible matches. Handle per the handoff package instructions: leave `expected_results: []` with a note explaining, do NOT flip `expected_empty` to true.

- The Phase −1 write-path hooks for FTS5 are simpler than Phase 1's reconciler because FTS5 inserts are cheap (no network call). Keep them inline in the tool code; don't over-abstract.

---

**End of plan v2.1.** Supersedes v2. If something in v2 was load-bearing and isn't covered here, flag it before starting Phase −1.






