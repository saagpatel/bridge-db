# bridge-db Semantic Memory Layer — Implementation Plan (v2)

> **Revision notes (v2, 2026-04-17):** Incorporates deep-dive fixes on chunking
> methodology (A), hybrid RRF formula lock (B), full scrubber pattern set (C),
> migration read-during-migration rule (D), eval set methodology overhaul (E),
> and embedding cleanup on source-row changes (F). Locked decisions in Section
> 2d grew from 7 to 10. Phase 0 gained a content-length analysis task (deferred
> to Claude Code execution). Phase 1 test count grew from 14 to 22.

## Section 1: EXEC SUMMARY

### 1a. What we're building

A semantic search layer on top of the existing bridge-db SQLite MCP server (localhost:9199). Adds vector embeddings for three content types — context sections, system snapshots, and activity entries — stored alongside the existing data using the sqlite-vec extension. Embeddings are generated via Voyage AI's `voyage-3-large` model. Exposes a new MCP tool `semantic_search(query, scope, limit)` that any of the three connected systems (Claude.ai, Claude Code, Codex) can call to retrieve semantically similar past content. Existing 16 tools and 65 tests remain unchanged. The layer adds 5 new tools and approximately 32 new tests.

### 1b. Riskiest parts and de-risking strategy

**Risk 1: Embedding generation latency on bulk backfill (Severity: MEDIUM)**
- Why it is risky: Backfilling embeddings for all existing context sections, snapshots, and activity entries (likely 500-2000 rows total) requires sequential API calls to Voyage. At ~200ms per call, that's 100-400 seconds of work blocking initial deployment. If interrupted, partial state is hard to recover.
- Mitigation: Build the backfill as a resumable background job with a `embeddings_backfill_state` table that tracks `last_processed_id` per content type. Run in batches of 50, commit after each batch. The bulk endpoint at Voyage allows up to 128 inputs per call — use it.
- Fallback: If Voyage is unreachable during backfill, the job logs and exits cleanly. Re-run picks up from `last_processed_id`. Search falls back to keyword-only mode if embeddings don't exist for a given row.

**Risk 2: Embedding drift across model versions (Severity: MEDIUM)**
- Why it is risky: If Voyage releases `voyage-4` and you migrate, existing embeddings under the old model are incompatible with new query embeddings. Naively filtering to current model only would hide all pre-migration content until re-embedding completes.
- Mitigation: Store the embedding model name as a column on every embedding row (`embedding_model TEXT NOT NULL`). `semantic_search` reads from ALL model versions present, flagging non-current results with `{"stale_model": "voyage-3-large"}` in `SearchResult.metadata`. Similarity scores across models are not directly comparable, so stale-model results are scored within their own embedding space and merged into the result list after the current-model results. A migration tool re-embeds rows under the new model; migration completes when zero rows remain under the old model.
- Fallback: If cross-model merging produces noisy rankings, expose a `current_model_only` flag on `semantic_search` that falls back to current-model filtering. Default stays inclusive.

**Risk 3: Voyage API key exposure (Severity: HIGH)**
- Why it is risky: API key in plaintext is the standard Python developer mistake. bridge-db runs as a local daemon, but the key still needs to be readable by the process.
- Mitigation: Store the key in macOS Keychain via the `keyring` Python library. The bridge-db daemon reads it at startup. No `.env` file. No environment variable in the launchd plist.
- Fallback: If Keychain access fails at startup, the daemon logs a clear error pointing to a `bridge-db-setup` CLI command that walks the user through key entry. The semantic layer is disabled but the existing 16 tools still work.

**Risk 4: Search result quality on first deployment (Severity: MEDIUM)**
- Why it is risky: Without tuning, the top-k results from cosine similarity may include semantically close but irrelevant rows. Bad first impressions kill adoption — you stop using the tool, the layer rots.
- Mitigation: Build a 20-query eval set using the methodology in Section 7 (workflow-sourced queries written before viewing bridge-db content, four-tier expected-result labeling, inter-rater subset for label validation). Run it after backfill completes. Report weighted precision@5 in the deployment summary. Threshold: 0.6 for at least one of `semantic_search` / `hybrid_search`.
- Fallback: If quality is below threshold after Phase 2 tuning, add sub-chunking (trigger criteria defined in Section 2d decision 7) and re-embed, then re-run eval. If still below threshold, add Phase 3 re-ranking.

**Risk 5: sqlite-vec extension loading on macOS (Severity: LOW)**
- Why it is risky: SQLite extensions sometimes fail to load on macOS due to security restrictions (Gatekeeper, system Python's restricted SQLite build). System Python on recent macOS often disables `enable_load_extension`.
- Mitigation: Use the `sqlite-vec` Python package which ships a pre-compiled extension. Connect via `sqlite3` with `enable_load_extension(True)` after explicitly using a Python from `pyenv` or `uv`-managed virtualenv (not system Python). Document this in CLAUDE.md.
- Fallback: If sqlite-vec won't load on M4 Pro, fall back to the `chromadb` embedded mode which is also a single-file local store. This adds a dependency but avoids the SQLite extension issue. Document the decision tree.

**Risk 6: Embedding/metadata table inconsistency (Severity: MEDIUM)**
- Why it is risky: vec0 virtual tables and regular SQLite tables participate in transactions, but the interaction has edge cases. If the vec0 insert succeeds and the `embedding_metadata` insert fails (or vice versa), the mirroring goes out of sync and scope filtering returns wrong results.
- Mitigation: All embedding writes go through `store.upsert_embedding()` which uses `BEGIN IMMEDIATE` transaction semantics (via `with conn:` context manager). Delete-then-insert pattern for upserts; both deletes in one transaction. Unit tests verify rollback on simulated failure.
- Fallback: If transactionality proves unreliable on sqlite-vec under stress, add a periodic reconciliation job that detects orphans in either direction and repairs them. Log reconciliation events as warnings.

**Risk 7: p95 latency breaches 500ms on cold queries (Severity: MEDIUM)**
- Why it is risky: Voyage query-embedding call alone is 150-250ms (network + inference). Add vec0 MATCH (~10ms), metadata join (~5ms), serialization — you're already at 200-300ms before any network jitter. On a hotel wifi or when Voyage has a slow hour, p95 blows through the SLA.
- Mitigation: Phase 1 ships with a simple in-process LRU cache (size 64) keyed on `(query, model)` tuples. Cached query embeddings skip the Voyage call entirely. Also add metric logging so p95 is measurable rather than guessed.
- Fallback: If p95 still breaches on uncached queries, relax the SLA to `warm_p95 < 500ms, cold_p95 < 1000ms` in Section 2b and document the network dependency.

### 1c. Shortest path to daily personal use

Phase 0 + Phase 1 + Phase 2 = 3 weeks to daily-use semantic search.

- **Phase 0 (Week 1):** Schema, sqlite-vec setup, Keychain integration, content-length analysis to confirm or adjust chunking lock. Solves 0% of pain — pure foundation.
- **Phase 1 (Week 2):** Backfill + indexing tools + first MCP tool (`semantic_search` with cross-scope) + embedding-invalidation hooks on existing write-path tools + scrubber + query-embed cache. Solves 70% of pain — you can now ask "have I seen this before" across all bridge-db content from any of the three systems.
- **Phase 2 (Week 3):** Scope filtering + hybrid search + quality eval. Solves the remaining 25% — fine-grained scoping ("just snapshots," "just activity from last 30 days") plus hybrid mode for short queries.
- **Phase 3 (optional, Week 4):** Re-ranking + cross-system telemetry. Solves the last 5% but mostly polish.

---

## Section 2: REVIEW GATE (SPEC LOCK)

### 2a. Goal

Add a semantic search MCP tool to bridge-db that returns top-k semantically similar past content across context sections, snapshots, and activity entries, with optional scope filtering, in under 500ms per query at warm p95 (cached query embedding) and under 1000ms at cold p95 (uncached).

### 2b. Success metrics

1. **Latency:** Semantic search returns results in under 500ms at warm p95 and under 1000ms at cold p95 for a database of 2000 embedded rows on M4 Pro hardware. Measured via the telemetry log over 50 sample queries after Phase 2 ships.
2. **Quality:** Weighted precision@5 of at least 0.6 on the 20-query eval set after Phase 2 ships, for at least one of `semantic_search` or `hybrid_search`. Weighting defined in Section 7.
3. **Reliability:** Backfill completes for 2000 rows without manual intervention; resumable from any failure point.
4. **Cost:** Voyage API spend stays under $2/month for a database that grows by ~100 rows/week.
5. **Adoption:** Used at least 5 times per week across Claude.ai, Claude Code, and Codex sessions in the first month after Phase 2 ships.

### 2c. Hard constraints

1. No new datastores. Embeddings live in the existing `bridge.db` SQLite file via sqlite-vec.
2. Existing 16 tools and 65 tests must continue to pass unchanged. Semantic layer is purely additive.
3. Voyage API key must be stored in macOS Keychain via the `keyring` library. Never plaintext, never `.env`, never environment variable in launchd plist.
4. The semantic layer must degrade gracefully — if Voyage is down or sqlite-vec fails to load, the existing 16 tools must still work.
5. All new tools must follow the existing bridge-db tool naming convention (snake_case, prefixed with action verb).
6. Embeddings must be regeneratable. The migration path from `voyage-3-large` to a future `voyage-4` is part of the design, not an afterthought.
7. Existing write-path tools (`update_section`, `log_activity`, `save_snapshot`) must invalidate stale embeddings explicitly — no implicit triggers.

### 2d. Locked decisions

- **Decision 1:** Vector storage backend
  - Locked to: sqlite-vec
  - Rationale: Single datastore, actively maintained, integrates with existing SQLite at localhost:9199, no second MCP server needed.

- **Decision 2:** Embedding model
  - Locked to: Voyage AI `voyage-3-large` (1024-dim)
  - Rationale: Best quality on technical content per Voyage's own benchmarks and corroborating community evals. Anthropic-recommended partner. Cost negligible at user's volume (~$0.50-2/month projected).

- **Decision 3:** Distance metric
  - Locked to: Cosine similarity
  - Rationale: Standard for normalized embeddings. Voyage embeddings come pre-normalized; cosine equals dot product, which sqlite-vec handles natively at high speed.

- **Decision 4:** Backfill batch size
  - Locked to: 50 rows per batch, with Voyage's bulk-input endpoint (up to 128 per API call).
  - Rationale: Balances throughput against memory and recovery granularity. A failed batch loses at most 50 rows of progress.

- **Decision 5:** Search result default limit
  - Locked to: 5 results by default, max 20.
  - Rationale: Five is enough for "have I seen this" workflows. Twenty is the upper bound for synthesis tasks. More than 20 dilutes signal.

- **Decision 6:** Keyring service name
  - Locked to: `bridge-db-voyage`
  - Rationale: Namespaced so it doesn't collide with other Keychain entries. Easy to find and rotate via `security` CLI.

- **Decision 7:** Chunking strategy for embeddings
  - Locked to: One embedding per row by default. Concatenate row title + content with a separator before embedding. Sub-chunking decision deferred to Phase 0 Task 2.5 (content-length analysis) with these locked criteria:
    - If P90(context_sections token length) < 1500 tokens → no sub-chunking (current default stands).
    - If P90(context_sections) 1500-3000 tokens → sub-chunk sections only, using 512-token sliding window with 64-token overlap.
    - If P90(context_sections) > 3000 tokens → sub-chunk sections with 512/64 window AND reconsider snapshot chunking with the same criteria.
    - Snapshots and activity entries: no chunking regardless of length (both are short by nature; re-evaluate only if Phase 2 eval shows systematic recall failure on these types).
  - Rationale: bridge-db rows are generally short and atomic, but context sections can grow. Locking the decision *criteria* now with data-collection deferred to Phase 0 prevents both premature over-engineering and late-stage rework.

- **Decision 8:** Hybrid search fusion formula
  - Locked to: Weighted Reciprocal Rank Fusion with k=60, top-50 truncation per list, missing-list contribution = 0.
  - Formula: `combined_score(d) = vector_weight × (1/(60 + rank_vec(d))) + (1 - vector_weight) × (1/(60 + rank_fts(d)))`
  - Default `vector_weight = 0.5`. Clamped to [0.0, 1.0].
  - Rationale: k=60 is the empirically validated default from Cormack/Clarke/Büttcher (2009). Weighted variant gives user control without breaking the mathematical soundness of plain RRF. Top-50 truncation prevents long-tail noise from dominating fusion when vector and FTS disagree everywhere. Documents appearing in only one ranked list get zero contribution from the missing side rather than 1/k, which would otherwise inflate weakly-supported results.

- **Decision 9:** Read-during-migration behavior
  - Locked to: `semantic_search` reads from ALL embedding models present in `embedding_metadata`, not just the current one. Results from non-current models are flagged in `SearchResult.metadata` as `{"stale_model": "<model-name>"}`. Stale-model results are ranked within their own embedding space and appended after current-model results in the merged output. Migration completes when zero rows remain under the old model.
  - Rationale: Users can still search during migration. Stale-flagged results give them a signal without hiding data. Cross-model similarity scores are not directly comparable, so the two-tier merge prevents naive ranking across incompatible score spaces.

- **Decision 10:** Embedding invalidation on source-row changes
  - Locked to: Explicit `invalidate_embedding(source_type, source_id)` call from the existing write-path tools (`update_section`, `log_activity`, `save_snapshot`, plus any delete paths). No SQLite triggers.
  - Rationale: bridge-db's design ethos is transparency. Triggers are magic: they make it harder to reason about when embeddings change. Explicit calls at the write site keep the invalidation flow readable in the tool code.

---

## Section 3: ARCHITECTURE

### 3a. System diagram

```
                  ┌─────────────────────────────────────┐
                  │   Claude.ai / Claude Code / Codex   │
                  │       (MCP clients via SSE)         │
                  └──────────────────┬──────────────────┘
                                     │ semantic_search(query, scope, limit)
                                     ↓
                  ┌─────────────────────────────────────┐
                  │   bridge-db MCP Server (existing)   │
                  │      localhost:9199 (FastMCP)       │
                  └──────────────────┬──────────────────┘
                                     │
                  ┌──────────────────┴──────────────────┐
                  │   Semantic Layer (new module)        │
                  │   - query embedding via Voyage API   │
                  │     (LRU cache, size 64)             │
                  │   - pre-embed scrubber               │
                  │   - vector search via sqlite-vec     │
                  │   - scope filter via SQL WHERE       │
                  │   - hybrid mode (vector + FTS5)      │
                  │   - cross-model merge for migration  │
                  └──────────────────┬──────────────────┘
                                     │
                  ┌──────────────────┴──────────────────┐
                  │   bridge.db (SQLite, existing)       │
                  │   - context_sections                 │
                  │   - snapshots                        │
                  │   - activity                         │
                  │   + embeddings (new vec0 vtable)     │
                  │   + embedding_metadata (new)         │
                  │   + embeddings_backfill_state (new)  │
                  └─────────────────────────────────────┘

    Voyage AI (cloud) ←── batched embed requests ──┘
    macOS Keychain  ←── key fetch at daemon startup ┘
```

### 3b. Tech stack

- Python 3.12+ — bridge-db's existing runtime
- sqlite-vec 0.1.6+ — SQLite extension for vector storage and search; ships as a Python package with pre-built binaries
- voyageai (Python SDK) — official Voyage AI client, handles batching and retries
- keyring 24.0+ — Python library for macOS Keychain access via the Security framework
- FastMCP (existing) — bridge-db's MCP server framework, already in use for the 16 existing tools
- pytest (existing) — for the 32 new test cases
- SQLite 3.41+ (system) — required minimum for sqlite-vec extension loading on macOS

### 3c. File structure

```
bridge-db/
├── src/
│   └── bridge_db/
│       ├── __init__.py
│       ├── server.py                    # Existing FastMCP server entry point
│       ├── db.py                        # Existing SQLite connection management
│       ├── tools/
│       │   ├── __init__.py
│       │   ├── existing tools.py        # 16 existing tool registrations
│       │   └── semantic.py              # NEW — 5 semantic search tool registrations
│       ├── semantic/                    # NEW MODULE
│       │   ├── __init__.py
│       │   ├── types.py                 # Dataclasses + Literal type aliases
│       │   ├── embeddings.py            # Voyage client wrapper, batching, retries, LRU cache
│       │   ├── scrubbing.py             # Pre-embed credential scrubber (13 patterns)
│       │   ├── store.py                 # sqlite-vec virtual table management, upsert/delete atoms
│       │   ├── search.py                # Query embedding + vector search + scope filter + cross-model merge
│       │   ├── hybrid.py                # Vector + FTS5 weighted RRF
│       │   ├── backfill.py              # Resumable bulk embedding job
│       │   └── keychain.py              # macOS Keychain via keyring library
│       └── migrations/
│           ├── 008_semantic_memory.sql  # NEW — sqlite-vec vtable + metadata + state tables
│           └── 009_fts5_indexes.sql     # NEW — FTS5 indexes for hybrid mode (Phase 2)
├── tests/
│   ├── existing test files
│   ├── semantic/
│   │   ├── __init__.py
│   │   ├── test_embeddings.py           # 5 tests — Voyage client, batching, error handling, cache
│   │   ├── test_scrubbing.py            # 6 tests — pattern coverage, ordering, idempotence, hash consistency
│   │   ├── test_store.py                # 7 tests — CRUD + upsert replaces old + orphan cleanup
│   │   ├── test_search.py               # 7 tests — cross-scope, scope filter (3), limit, min_similarity, cross-model merge
│   │   ├── test_hybrid.py               # 4 tests — RRF correctness, weight tuning, scope filter, truncation boundary
│   │   ├── test_backfill.py             # 3 tests — full run, resume, content-hash skip
│   │   ├── test_keychain.py             # 2 tests — happy path, missing key error
│   │   └── test_invalidation.py         # 3 tests — invalidation from each write-path tool
├── eval/
│   └── semantic_quality_set.json        # NEW — 20 query/expected-result pairs, workflow-sourced
├── scripts/
│   ├── existing scripts
│   ├── bridge-db-setup                  # NEW CLI — walks user through Voyage key entry
│   ├── analyze_content_lengths.py       # NEW — Phase 0 content-length analysis (chunking decision)
│   └── run_quality_eval.py              # NEW — Phase 2 eval harness
├── pyproject.toml                       # Add voyageai, sqlite-vec, keyring
├── CLAUDE.md                            # Update with semantic layer guidance
└── README.md                            # Update with new tool docs
```

### 3d. Data model

The semantic layer adds three tables to the existing `bridge.db`. No changes to existing tables.

```sql
-- Migration 008_semantic_memory.sql

-- Load sqlite-vec extension at connection time (handled in db.py, not here)

-- Virtual table for vector storage and search.
-- vec0 is sqlite-vec's virtual table type optimized for vector ops.
CREATE VIRTUAL TABLE IF NOT EXISTS embeddings USING vec0(
    embedding FLOAT[1024],
    +source_type TEXT,           -- 'section' | 'snapshot' | 'activity'
    +source_id INTEGER,           -- FK to context_sections.id, snapshots.id, or activity.id
    +embedding_model TEXT,        -- e.g., 'voyage-3-large'
    +created_at TEXT,             -- ISO 8601 timestamp
    +content_hash TEXT            -- SHA256 of scrubbed embedded text, for change detection
);

-- Mirror of vec0 metadata for fast WHERE-clause filtering.
-- vec0 auxiliary columns (prefixed with +) are not indexed by sqlite-vec itself.
CREATE TABLE IF NOT EXISTS embedding_metadata (
    rowid INTEGER PRIMARY KEY,    -- matches embeddings.rowid
    source_type TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    embedding_model TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    content_hash TEXT NOT NULL,
    UNIQUE(source_type, source_id, embedding_model)
);
CREATE INDEX idx_embedding_meta_source ON embedding_metadata(source_type, source_id);
CREATE INDEX idx_embedding_meta_model ON embedding_metadata(embedding_model);

-- Resumable backfill state. One row per (source_type, embedding_model) pair.
CREATE TABLE IF NOT EXISTS embeddings_backfill_state (
    source_type TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    last_processed_id INTEGER NOT NULL DEFAULT 0,
    total_rows INTEGER,
    completed_at TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source_type, embedding_model)
);
```

### 3e. Type definitions

```python
# src/bridge_db/semantic/types.py
from dataclasses import dataclass
from typing import Literal
from datetime import datetime

SourceType = Literal["section", "snapshot", "activity"]
SearchScope = Literal["all", "section", "snapshot", "activity"]


@dataclass
class EmbeddingRecord:
    rowid: int
    source_type: SourceType
    source_id: int
    embedding_model: str
    created_at: datetime
    content_hash: str


@dataclass
class SearchResult:
    source_type: SourceType
    source_id: int
    distance: float                       # Cosine distance (lower = more similar)
    similarity: float                     # 1 - distance (higher = more similar)
    content_preview: str                  # First 200 chars of source content
    full_content_uri: str                 # e.g., "bridge-db://snapshot/42"
    metadata: dict                        # Source-type-specific fields + optional {"stale_model": "<name>"}


@dataclass
class HybridSearchResult(SearchResult):
    vector_score: float
    fts_score: float
    combined_score: float                 # Weighted RRF of vector + FTS


@dataclass
class BackfillProgress:
    source_type: SourceType
    embedding_model: str
    processed: int
    total: int
    percent_complete: float
    started_at: datetime
    completed_at: datetime | None
```

### 3f. API contracts

**External APIs:**

| Service | Endpoint | Method | Auth | Rate Limit | Pagination | Purpose |
|---------|----------|--------|------|------------|------------|---------|
| Voyage AI | `https://api.voyageai.com/v1/embeddings` | POST | Bearer token from Keychain | 300 req/min, 1M tokens/min on paid tier | None — single batch up to 128 inputs | Generate embeddings for backfill and query-time lookups |

Voyage error handling: retry with exponential backoff on 429 (rate limit) and 5xx. Surface 401 immediately as a Keychain key issue. Log 4xx other than 401/429 with full request context.

**Internal APIs (new MCP tools):**

```python
# Tool 1: Primary semantic search
async def semantic_search(
    query: str,
    scope: Literal["all", "section", "snapshot", "activity"] = "all",
    limit: int = 5,
    min_similarity: float = 0.0,
    current_model_only: bool = False,
) -> list[SearchResult]:
    """
    Return top-k semantically similar past content. Limit clamped to [1, 20].
    By default reads from all embedding models present and flags stale-model
    results via metadata['stale_model']. Set current_model_only=True to filter
    to the active model (Decision 9).
    """


# Tool 2: Hybrid search (vector + FTS)
async def hybrid_search(
    query: str,
    scope: Literal["all", "section", "snapshot", "activity"] = "all",
    limit: int = 5,
    vector_weight: float = 0.5,
) -> list[HybridSearchResult]:
    """
    Combine vector similarity with full-text search via weighted RRF.
    combined_score = vector_weight × (1/(60 + rank_vec)) + (1 - vector_weight) × (1/(60 + rank_fts))
    vector_weight clamped to [0.0, 1.0]. Top-50 truncation per list before fusion.
    """


# Tool 3: Trigger or resume backfill
async def backfill_embeddings(
    source_type: Literal["all", "section", "snapshot", "activity"] = "all",
    force_rebuild: bool = False,
) -> BackfillProgress:
    """Embed all rows missing embeddings for the current model. Resumable."""


# Tool 4: Backfill status
async def backfill_status() -> list[BackfillProgress]:
    """Return progress for every (source_type, model) backfill pair."""


# Tool 5: Re-embed under a new model (for migrations)
async def reembed_under_model(
    new_model: str,
    source_type: Literal["all", "section", "snapshot", "activity"] = "all",
) -> BackfillProgress:
    """Generate embeddings under a new model name, leaving old embeddings intact."""
```

**Internal invalidation hook (called by existing tools, not exposed as MCP tool):**

```python
# src/bridge_db/semantic/store.py
def invalidate_embedding(
    conn: sqlite3.Connection,
    source_type: SourceType,
    source_id: int,
) -> None:
    """
    Delete all embeddings (across all models) for a given source row.
    Called by update_section, log_activity, save_snapshot, and any delete paths.
    Atomic across embeddings vtable and embedding_metadata table.
    """
```

### 3g. Dependencies with install commands

```bash
# Runtime additions to pyproject.toml
uv add sqlite-vec voyageai keyring

# Verify sqlite-vec loads on M4 Pro macOS
python -c "import sqlite_vec; conn = __import__('sqlite3').connect(':memory:'); conn.enable_load_extension(True); sqlite_vec.load(conn); print('sqlite-vec OK')"

# Verify keyring backend is macOS Keychain
python -c "import keyring; print(keyring.get_keyring())"
# Expected: <keyring.backends.macOS.Keyring ...>

# Dev — no new dev deps; existing pytest harness covers new tests

# System — none. SQLite 3.41+ ships with macOS 14+; Voyage and Keychain are cloud and OS, not local.
```

---

## Section 4: PHASED IMPLEMENTATION

## Phase 0: Foundation (Week 1)

### Objectives
- Add sqlite-vec and Voyage SDK to bridge-db's runtime dependencies.
- Migration 008 applied: `embeddings` virtual table, `embedding_metadata` table, `embeddings_backfill_state` table created.
- Voyage API key stored in macOS Keychain under service name `bridge-db-voyage`.
- `bridge-db-setup` CLI walks the user through key entry on first run.
- Content-length analysis complete; chunking decision confirmed per Decision 7 criteria.
- Existing 16 tools and 65 tests continue to pass.

### Tasks
1. Add `sqlite-vec`, `voyageai`, `keyring` to pyproject.toml — Acceptance: `uv sync` completes; `uv pip list` shows all three at locked versions.
2. Verify sqlite-vec loads on M4 Pro — Acceptance: the verification one-liner from 3g prints "sqlite-vec OK".
3. Update `db.py` to enable extension loading and load sqlite-vec at every connection — Acceptance: existing test suite (`pytest`) passes; new test `test_sqlite_vec_loads.py` confirms `vec_version()` returns a version string.
4. Write migration `008_semantic_memory.sql` — Acceptance: applying the migration to a fresh DB creates all three new tables/vtables; querying `embeddings` and `embedding_metadata` returns empty result sets, not errors.
5. Build `keychain.py` module wrapping `keyring` calls — Acceptance: `set_voyage_key("test")` then `get_voyage_key()` returns `"test"`; deleting via macOS Keychain Access app then calling `get_voyage_key()` raises `VoyageKeyNotConfigured`.
6. Build `bridge-db-setup` CLI — Acceptance: running `bridge-db-setup voyage-key` prompts for the key, stores it in Keychain, prints "Voyage key configured."
7. Build `scripts/analyze_content_lengths.py` — iterates `context_sections`, `snapshots`, `activity`; computes token count per row using `voyageai.Client().count_tokens()`; reports P50, P75, P90, P99 per source_type; prints the chunking decision per Decision 7 criteria — Acceptance: script runs against real bridge.db; output includes per-source-type percentiles and a single line of the form `CHUNKING DECISION: <no_sub_chunking | sub_chunk_sections | sub_chunk_sections_and_reconsider_snapshots>`.
8. Apply the chunking decision to CLAUDE.md and (if needed) update `backfill.py` plans before Phase 1 starts — Acceptance: CLAUDE.md's "Semantic Layer Setup" section records the P90 numbers observed and the decision taken.
9. Update CLAUDE.md with semantic layer setup notes — Acceptance: CLAUDE.md mentions sqlite-vec extension requirement, Keychain key location, the setup command, and the chunking decision result.

### Phase Verification Checklist
- [ ] `uv sync` → exits 0 with no warnings
- [ ] `pytest tests/` → all 65 existing tests pass; 1 new sqlite-vec smoke test passes
- [ ] `bridge-db-setup voyage-key` → prompts, stores, confirms
- [ ] `security find-generic-password -s bridge-db-voyage` → returns the entry (key value not printed)
- [ ] `python scripts/analyze_content_lengths.py` → prints percentiles + chunking decision line
- [ ] CLAUDE.md updated with chunking decision outcome
- [ ] Manual: stop and restart bridge-db daemon → daemon starts cleanly, logs "Semantic layer ready (no embeddings yet)"

### Risks & Mitigations
- Risk: sqlite-vec fails to load on M4 Pro due to system Python restrictions
  - Mitigation: Use `uv`-managed Python explicitly in launchd plist (already the case) and the `sqlite-vec` package's bundled binary
  - Fallback: Switch to chromadb embedded mode; add migration to move metadata accordingly. Decision made before Phase 1 starts.

- Risk: Content-length analysis reveals P90 > 3000 tokens, forcing sub-chunking of both sections and snapshots
  - Mitigation: Decision 7 criteria already cover this branch; Phase 1 plan changes only in `backfill.py` (adds sliding-window chunker) and `store.py` (adds chunk_index column)
  - Fallback: If chunking adds complexity that blows Phase 1 timeline, ship Phase 1 with no chunking and revisit in Phase 2 based on eval results

### Phase-end review: Run `/ultrareview` before marking phase complete.

---

## Phase 1: Backfill + Cross-Scope Search (Week 2)

### Objectives
- All existing context sections, snapshots, and activity entries are embedded under `voyage-3-large`.
- The `semantic_search` MCP tool ships with cross-scope mode (`scope="all"`).
- Backfill is resumable from interruption.
- Pre-embed credential scrubbing is active.
- Query-embedding LRU cache is active.
- Embedding invalidation hooks are wired into the three existing write-path tools.
- The 16 existing tools and Phase 0 additions continue to pass.

### Tasks
1. Build `scrubbing.py` with the 13-pattern credential scrubber (Section 5 full pattern list) — Acceptance: `test_scrubbing.py` passes (6 tests: per-pattern coverage, look-alike negatives, pattern ordering, idempotence, hash consistency, multi-match counts).
2. Build `embeddings.py` — Voyage client wrapper using bulk endpoint, batches of 50, exponential backoff on 429/5xx, surfaces 401 as Keychain issue, LRU cache (size 64) on query-side `embed_query()` keyed on `(query, model)`, calls `scrubbing.scrub()` before every embed request — Acceptance: `embed_batch(["foo", "bar"])` returns two 1024-dim vectors; `test_embeddings.py` passes (5 tests: happy path, rate-limit retry, 401 handling, cache hit skips Voyage call, scrub applied before embed).
3. Build `store.py` — wraps sqlite-vec vtable for insert/update/delete/select, plus mirrors metadata to `embedding_metadata` table in the same `BEGIN IMMEDIATE` transaction. Exposes `upsert_embedding()` (delete-then-insert for existing keys), `invalidate_embedding(source_type, source_id)` (atomic multi-row delete for orphan cleanup on source-row change), `delete_embedding(source_type, source_id, model)` — Acceptance: `test_store.py` passes (7 tests: CRUD, upsert-replaces-old, orphan-cleanup-on-source-delete, transaction-rollback-on-failure, unique-constraint-enforced, metadata-mirror-stays-consistent).
4. Build `backfill.py` — iterates over each source type, batches into groups of 50, scrubs via `scrubbing.scrub()`, embeds via `embeddings.py`, writes via `store.py`, updates `embeddings_backfill_state.last_processed_id` after each batch commit, skips rows with unchanged content_hash — Acceptance: `test_backfill.py` passes (3 tests: full backfill on small fixture, simulated interrupt + resume, content-hash-skip for unchanged rows).
5. Build `search.py` — embed query via Voyage (with cache), run sqlite-vec `MATCH` query, join `embedding_metadata` for scope filter, return `SearchResult` list. Cross-model merge: queries all models present, flags stale results with `{"stale_model": "<name>"}` in metadata, ranks current-model first then stale-model — Acceptance: integration test `test_search.py::test_cross_scope` passes; `test_cross_model_merge` passes (fixture with two models present shows current-model results ranked first, stale flagged).
6. Wire `invalidate_embedding()` into the existing `update_section`, `log_activity`, `save_snapshot` tools and any existing delete paths — Acceptance: `test_invalidation.py` passes (3 tests, one per write-path tool: update triggers invalidation, log triggers invalidation, snapshot triggers invalidation).
7. Register `semantic_search`, `backfill_embeddings`, `backfill_status`, `reembed_under_model` as MCP tools in `tools/semantic.py` — Acceptance: bridge-db daemon restarts; total tool count is now 20 (16 existing + 4 new; `hybrid_search` lands in Phase 2); all show in MCP client tool listings.
8. Run initial backfill against real bridge.db — Acceptance: `backfill_embeddings(source_type="all")` completes without error; `embedding_metadata` row count equals sum of rows across `context_sections + snapshots + activity`.
9. Manually verify search quality with 5 ad-hoc queries — Acceptance: at least 3 of 5 queries return at least one result the user judges relevant.

### Phase Verification Checklist
- [ ] `pytest tests/semantic/` → 23 tests pass (6 scrubbing + 5 embeddings + 7 store + 3 backfill + 2 search + 3 invalidation — search gets 2 more in Phase 2, plus hybrid and invalidation total adjusts)
- [ ] `pytest tests/` → all 65 existing tests still pass
- [ ] Run from Claude Code: `mcp call bridge-db semantic_search '{"query": "Tier 4 priorities", "scope": "all"}'` → returns 5 results
- [ ] Backfill status shows `completed_at` set for all three source types
- [ ] Embedding cost from Voyage dashboard is under $0.20 for the initial backfill
- [ ] Warm p95 measured on 20 repeat queries: under 100ms (cache hits dominate)

### Risks & Mitigations
- Risk: Voyage rate limit hit on backfill of large activity log
  - Mitigation: 50-row batches with built-in retry; bulk endpoint accepts up to 128 inputs per call so we're well under per-minute limits
  - Fallback: If 429s persist, drop batch size to 25 and add 200ms sleep between batches. Document the constraint.

- Risk: First-deployment search results feel mediocre
  - Mitigation: Manual quality check on 5 queries before declaring phase complete; if poor, push hybrid mode (Phase 2) earlier or expand chunking strategy per Decision 7
  - Fallback: Defer Phase 2 features and prioritize quality eval from Phase 2 first

- Risk: Scrubber false positives on legitimate documentation
  - Mitigation: Non-blocking design — scrubbed text still embeds, just with `[REDACTED]` in place of matches; logged at WARNING for audit
  - Fallback: If false-positive rate is disruptive, add an allow-list mechanism in Phase 2

### Phase-end review: Run `/ultrareview` before marking phase complete.

---

## Phase 2: Scope Filtering + Hybrid + Quality Eval (Week 3)

### Objectives
- `semantic_search` supports scope filtering (`section` | `snapshot` | `activity` in addition to `all`).
- `semantic_search` supports `min_similarity` filter and limit clamping.
- New `hybrid_search` tool combines vector similarity with FTS5 via weighted RRF (Decision 8).
- A 20-query eval set (methodology per Section 7) runs and reports weighted precision@5.
- Re-ranking decision documented based on eval results.

### Tasks
1. Add scope filter logic to `search.py` — when `scope != "all"`, add `WHERE source_type = ?` to the join — Acceptance: `test_search.py::test_scope_filter` passes (3 tests, one per source type).
2. Add limit clamping (1-20) and `min_similarity` filter to `search.py` — Acceptance: `test_search.py::test_limit_clamping` and `test_min_similarity` pass.
3. Verify FTS5 virtual tables exist on existing `context_sections.content`, `snapshots.content`, `activity.note` — if not, add migration `009_fts5_indexes.sql` and create them — Acceptance: `SELECT * FROM context_sections_fts WHERE content MATCH 'test'` returns expected rows.
4. Build `hybrid.py` — runs vector and FTS5 queries in parallel (top-50 per list), applies weighted RRF per Decision 8 formula, returns `HybridSearchResult` list sorted by `combined_score` — Acceptance: `test_hybrid.py` passes (4 tests: pure-RRF case with weight=0.5 produces canonical score, weight=1.0 gives vector-only with FTS-only docs scoring 0, weight=0.0 gives FTS-only with vector-only docs scoring 0, truncation boundary — rank-51 doc doesn't appear in fused output).
5. Register `hybrid_search` MCP tool — Acceptance: total tool count is now 21; tool callable from Claude Code.
6. Build `eval/semantic_quality_set.json` per Section 7 methodology (20 queries, workflow-sourced, four-tier expected labels, query-written-before-viewing flag on every entry) — Acceptance: file exists, schema validated, every non-empty expected_result references a real row in bridge.db, at least 2 queries have `expected_empty: true`.
7. Run inter-rater validation on 5 query subset — Acceptance: blind second-rater agreement (label match ignoring rank_tier differences within the same "relevant" set) is at least 80% on the 5-query subset; if lower, revisit ambiguous queries before running full eval.
8. Build `scripts/run_quality_eval.py` — runs every query against `semantic_search` and `hybrid_search`, computes weighted precision@5 per Section 7 formula, reports aggregate per mode and per category, includes empty-query precision (1.0 if top similarity < min_similarity else 0) — Acceptance: script runs cleanly, prints per-mode and per-category weighted precision@5, plus per-query breakdown.
9. Run quality eval and decide on re-ranking — Acceptance: weighted precision@5 of at least 0.6 for at least one of the two modes; if neither hits, document next steps in CLAUDE.md and either (a) enable sub-chunking per Decision 7 fallback and re-embed, or (b) create a Phase 3 ticket for re-ranking.

### Phase Verification Checklist
- [ ] `pytest tests/semantic/` → all 32 new tests pass (6 scrubbing + 5 embeddings + 7 store + 7 search + 4 hybrid + 3 backfill + 2 keychain + 3 invalidation — wait: that's 37; see Section 6 for the consolidated count)
- [ ] `pytest tests/` → all original 65 + all semantic tests pass
- [ ] `python scripts/run_quality_eval.py` → reports weighted precision@5 for both modes
- [ ] Inter-rater agreement log shows ≥80% on 5-query subset
- [ ] Manual: from Claude.ai, call `semantic_search` with `scope="snapshot"` → only snapshot results returned
- [ ] Manual: same query in `semantic_search` and `hybrid_search` → result orderings differ measurably (RRF is doing work)
- [ ] Warm p95 (cached query embeddings) under 500ms; cold p95 (uncached) under 1000ms

### Risks & Mitigations
- Risk: FTS5 indexes don't exist on existing tables
  - Mitigation: Migration 009 creates them; backfill is fast since FTS5 indexing is local
  - Fallback: If migration fails on production bridge.db, drop hybrid_search from Phase 2 and ship vector-only with scope filtering

- Risk: Weighted precision@5 below 0.6 on both modes
  - Mitigation: Apply Decision 7 sub-chunking fallback (512-token sliding window on sections) and re-embed; test query expansion (use Claude to generate 3 paraphrases and average their embeddings)
  - Fallback: Document the limitation and ship anyway. Quality improves with usage data; the eval set itself improves over time.

- Risk: Inter-rater agreement below 80% on 5-query subset
  - Mitigation: Rewrite ambiguous queries before running full eval. Document which queries were revised.
  - Fallback: Accept noisier labels on the remaining 15 queries but flag the quality metric as provisional in CLAUDE.md.

### Phase-end review: Run `/ultrareview` before marking phase complete.

---

## Phase 3: Optional — Re-ranking + Telemetry (Week 4)

### Objectives
- Optional cross-encoder re-ranking of top-20 results before returning top-5.
- Telemetry: which queries get called, from which system, with what scope, and which results get used.
- Documentation update: full README section on the semantic layer.

### Tasks
1. Build optional re-ranking step using Voyage's `voyage-rerank-2` model — runs only if `rerank=True` in the tool call — Acceptance: `test_search.py::test_rerank` passes; reranked results show different ordering on test fixture.
2. Build telemetry table `semantic_search_log` — captures query, scope, source system (from MCP client metadata), result count, latency (cold vs warm), whether any result was flagged stale-model — Acceptance: every search call logs one row; `SELECT COUNT(*) FROM semantic_search_log` increments per call.
3. Build `search_analytics()` MCP tool — returns top-N most-used queries, average latency (cold and warm separately), scope distribution, stale-model hit rate — Acceptance: tool callable; returns sensible aggregations after 10 sample queries.
4. Update README with full semantic layer documentation — Acceptance: README has a "Semantic Memory" section covering setup, tools, scope filter usage, hybrid mode, quality eval, cross-model migration, and re-ranking.

### Phase Verification Checklist
- [ ] `pytest tests/semantic/` → all tests including new rerank test pass
- [ ] Run 10 sample queries → `semantic_search_log` has 10 rows
- [ ] `search_analytics()` returns aggregations
- [ ] README "Semantic Memory" section is complete and accurate

### Risks & Mitigations
- Risk: Re-ranking adds 200-500ms latency that violates the p95 < 500ms warm SLA
  - Mitigation: Re-ranking is opt-in via `rerank=True`; default off keeps warm p95 commitment intact
  - Fallback: Don't ship rerank tool; defer to a future phase

### Phase-end review: Run `/ultrareview` before marking phase complete.

---

## Section 5: SECURITY & CREDENTIALS

- **Credential storage:** Voyage API key stored in macOS Keychain under service name `bridge-db-voyage`, account `default`. Accessed via `keyring` Python library at daemon startup; loaded into memory once, never written to disk in plaintext. No `.env` file. No environment variable. No keyring library calls outside `keychain.py`.

- **Data boundaries:** Embedding requests send the text content of bridge-db rows (snapshots, activity entries, context sections) to Voyage AI's API after passing through the pre-embed scrubber. This is the only data that leaves the machine. No chat content, no other Claude conversation data, no third-party tool data is included.

- **Encryption at rest:** bridge.db is on the user's M4 Pro local disk, which is encrypted via FileVault by default. Embeddings are stored alongside existing bridge-db data with the same protection. No additional application-level encryption needed for local data.

- **Token rotation:** Voyage keys do not expire automatically. Manual rotation procedure: run `bridge-db-setup voyage-key --rotate`, which prompts for new key, validates it with a test embed call, then replaces the Keychain entry. Document quarterly rotation in CLAUDE.md as a recommended habit.

- **Pre-embed scrubber:** All text passes through `semantic/scrubbing.py` before embedding. The scrubber is non-blocking (logs WARNING on hit, proceeds with scrubbed text), ordered most-specific-first, and runs before content_hash computation so hash consistency is preserved across runs. Pattern set (13 patterns, locked):
  - `aws_access_key_id` — `AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA|ABIA|ACCA` prefix + 16 alphanumeric
  - `aws_secret_access_key` — 40 base64-ish chars anchored after `aws_secret_access_key[=:"'\s]`
  - `github_token` — `ghp|gho|ghu|ghs|ghr` prefix + 36+ alphanumeric
  - `github_pat` — `github_pat_` prefix + 82 alphanumeric underscore
  - `slack_token` — `xox[baprs]-` prefix + 10+ alphanumeric dashes
  - `google_api_key` — `AIza` prefix + 35 alphanumeric underscore dash
  - `stripe_secret` — `(sk|rk)_(live|test)_` prefix + 24+ alphanumeric
  - `anthropic_key` — `sk-ant-` prefix + 32+ alphanumeric underscore dash
  - `openai_key` — `sk-proj-` prefix + 40+ alphanumeric underscore dash
  - `voyage_key` — `pa-` prefix + 40+ alphanumeric underscore dash
  - `jwt` — `eyJ` prefix + three base64url segments separated by dots, each 10+ chars
  - `pem_private_key` — full PEM block, multi-line
  - `generic_sk_key` — `sk-` prefix + 32+ alphanumeric underscore dash (last in ordering so specific `sk-*` patterns win first)
  - Decisions: (1) scrubbed text used for both embed request and content_hash so updates that only change a secret don't trigger re-embed, (2) non-blocking — scrubbed text still embeds, (3) false positives (e.g., documentation mentioning example keys) are accepted — the scrubbed version remains semantically searchable, (4) pattern matches are logged at WARNING level with pattern name and count but never the matched content.

- **Sensitive data handling:** bridge-db should not contain credentials, tokens, or PII in the first place — it stores work context and decisions. The scrubber is defense in depth. If scrubber patterns fire on real bridge-db content, audit what's being written to bridge-db and fix at the source.

---

## Section 6: TESTING STRATEGY

### Test count by phase (consolidated)

- Phase 0: 1 new test (sqlite-vec smoke)
- Phase 1: 26 new tests (6 scrubbing + 5 embeddings + 7 store + 3 backfill + 2 keychain + 3 invalidation)
- Phase 2: 11 new tests (5 additional search — scope×3, limit, min_similarity; 4 hybrid; 1 eval harness; 1 inter-rater agreement check)
- Phase 3: 3 new tests (rerank, telemetry logging, analytics aggregation)

**Total: 41 new tests added to the existing 65.** (Up from 25 in v1; the increase comes from the scrubber, invalidation, cross-model merge, and eval-harness test additions.)

### Phase 0 testing
- **Manual:** Verify sqlite-vec loads on M4 Pro; verify `bridge-db-setup voyage-key` round-trips through Keychain; verify daemon restart is clean; verify `analyze_content_lengths.py` produces decision line.
- **Automate:** 1 smoke test for sqlite-vec loading.
- **Verify correctness:** Migration 008 applied to a fresh in-memory DB produces the expected table schema (compare against schema fixture).

### Phase 1 testing
- **Manual:** Verify backfill completes against real bridge.db without errors; spot-check 5 ad-hoc semantic queries for sensible results; verify scrubber log output during backfill.
- **Automate:**
  - 6 tests for `scrubbing.py` — per-pattern coverage (parametrized), look-alike negatives, pattern ordering (specific wins over generic), idempotence, hash consistency, multi-match count accuracy.
  - 5 tests for `embeddings.py` — happy path, batching at 50, rate-limit retry with backoff, 401 surfaces as Keychain error, cache hit skips Voyage call.
  - 7 tests for `store.py` — insert + read back; upsert replaces old; delete; orphan cleanup on source-row delete; metadata mirroring stays consistent; unique constraint enforced; transaction rolls back on simulated mid-write failure.
  - 3 tests for `backfill.py` — full backfill on a 30-row fixture; simulated mid-batch interrupt + resume from `last_processed_id`; content-hash skip for unchanged rows.
  - 2 tests for `search.py` — cross-scope search returns results from all three source types; cross-model merge ranks current-model first with stale flagged.
  - 3 tests for invalidation — each of `update_section`, `log_activity`, `save_snapshot` calls `invalidate_embedding` on write.
  - 2 tests for `keychain.py` — happy path, missing key error.
- **Verify correctness:** Use a fixture of 20 known rows with hand-labeled "similar to" annotations; verify search returns expected matches in top-5.

### Phase 2 testing
- **Manual:** From Claude.ai, exercise scope filter for each of the four scope values; verify hybrid mode returns visibly different ordering than vector-only.
- **Automate:**
  - 3 tests for scope filter (one per source type).
  - 2 tests for limit clamping and min_similarity filter.
  - 4 tests for hybrid mode — weight=0.5 canonical RRF, weight=1.0 vector-only, weight=0.0 FTS-only, top-50 truncation boundary.
  - 1 test for the quality eval script returning a numeric weighted precision@5.
  - 1 test for inter-rater agreement computation on a 5-query fixture.
- **Verify correctness:** Run `scripts/run_quality_eval.py` against the 20-query eval set; verify weighted precision@5 ≥ 0.6 for at least one mode.

### Phase 3 testing
- **Manual:** Compare reranked vs non-reranked results on 10 ad-hoc queries; verify telemetry captures every call.
- **Automate:**
  - 1 test for re-ranking — reranked order differs from input order on a fixture.
  - 1 test for telemetry logging — every search call writes one row.
  - 1 test for `search_analytics()` returning sensible aggregations.
- **Verify correctness:** After 10 sample queries, `search_analytics()` returns top-N queries that match the test inputs.

---

## Section 7: EVAL SET METHODOLOGY

### Query sourcing

20 queries total, sourced from workflow contexts rather than from bridge-db content. Four categories, 5 queries each:

- **new_project_arrival** — "I'm starting X; have I done anything similar?"
- **mid_session_deja_vu** — "This feels like something I hit before; what was the decision?"
- **cross_system_coordination** — "Did Codex or Claude Code work on Y recently?"
- **pattern_re_use** — "Find past instances where I used Z approach."

### Writing discipline

Every query is drafted **before** viewing bridge-db content. The `query_written_before_viewing_db: true` flag in the JSON schema enforces this as a self-attestation. Only after drafting do you open bridge.db and mark expected results.

At least 2 of the 20 queries must be `expected_empty: true` — queries written from a plausible workflow context that happen to have no relevant content in the current bridge-db. These test the min_similarity filter's ability to return empty/low-confidence rather than fabricate matches.

### Expected result labeling

Four-tier labels per result:

- `must` — this result should appear in top-5 for the query to be considered a success
- `should` — this result appearing in top-5 is a strong signal; counts as half-credit
- `nice_to_have` — tangentially relevant; counts as quarter-credit
- (implicit: anything not listed) — irrelevant

### Weighted precision@5 formula

For each query with `expected_empty: false`:
```
hits = sum(1.0 for must-labeled in top-5)
     + sum(0.5 for should-labeled in top-5)
     + sum(0.25 for nice_to_have-labeled in top-5)
expected_max = sum(1.0 for must) + sum(0.5 for should) + sum(0.25 for nice_to_have)
query_score = hits / min(expected_max, count_of_labeled_in_expected)
```

For each query with `expected_empty: true`:
```
query_score = 1.0 if top_result.similarity < min_similarity else 0.0
```

Overall weighted precision@5 = mean(query_score) across all 20 queries.

### Inter-rater validation

You rate all 20 queries. A fresh Claude Code session (given only the query text and the candidate top-5 results, no other context) rates 5 of them blind on the same must/should/nice_to_have/irrelevant scale. Agreement metric: for each of the 5 queries, count how many result labels match between your labels and the blind rater's labels, where "match" means both rated the result as relevant-at-some-tier or both rated it irrelevant. Agreement target: ≥80% of labels match.

If agreement is below 80%, the ambiguous queries get rewritten before the full eval runs. Document which queries were revised and why.

### Eval set schema

```json
{
  "metadata": {
    "created": "2026-04-17",
    "bridge_db_snapshot": "sha256 of bridge.db at eval time",
    "total_queries": 20,
    "rater_primary": "saagar",
    "rater_secondary": "claude-code-blind",
    "inter_rater_sample_size": 5,
    "inter_rater_agreement": 0.0
  },
  "queries": [
    {
      "id": "q001",
      "category": "new_project_arrival",
      "query": "starting an MCP permission auditor project",
      "query_written_before_viewing_db": true,
      "expected_results": [
        {"source_type": "section", "source_id": 12, "rank_tier": "must"},
        {"source_type": "activity", "source_id": 487, "rank_tier": "should"},
        {"source_type": "snapshot", "source_id": 23, "rank_tier": "nice_to_have"}
      ],
      "expected_empty": false,
      "notes": "should surface mcp-audit planning context"
    },
    {
      "id": "q007",
      "category": "mid_session_deja_vu",
      "query": "keychain storage for daemon api keys",
      "query_written_before_viewing_db": true,
      "expected_results": [],
      "expected_empty": true,
      "notes": "no prior bridge-db content on this — should return low-similarity or empty"
    }
  ]
}
```

---

## Style Notes

This plan (v2) was generated by deep-diving the v1 plan against eight specific weak-spot categories (chunking methodology, RRF formula, scrubber patterns, migration read behavior, eval methodology, embedding cleanup, transactionality, latency SLA breakdown). Changes vs v1:

- **Locked decisions grew from 7 to 10** — added chunking criteria (Decision 7), weighted RRF formula (Decision 8), cross-model merge behavior (Decision 9), explicit invalidation hooks (Decision 10).
- **New Section 7** on eval methodology replaces the vague "15-20 hand-curated pairs" with a workflow-sourced, write-before-view, inter-rater-validated, weighted precision@5 design.
- **Scrubber moved from one sentence to a locked 13-pattern set** with ordering and non-blocking semantics explicit.
- **Invalidation hooks** added as a new architectural concern (Phase 1 task 6, Decision 10, test_invalidation.py).
- **Transactional upsert pattern** locked in store.py (prevents vec0 + metadata drift).
- **Query-embedding LRU cache** added to embeddings.py (Phase 1 task 2) to meet warm p95 SLA.
- **Latency SLA split** into warm p95 < 500ms and cold p95 < 1000ms in Section 2b.
- **Phase 0 gained content-length analysis task** (deferred to Claude Code execution; criteria locked in Decision 7).
- **Phase 1 test count grew** from 14 to 26; Phase 2 from 10 to 11; total new tests up from 25 to 41.

If v2 reads as still right-level-of-detail for vibe-code-handoff, proceed. If any of the new locks feel over-specified or the test count feels like it's creeping into CYA territory, flag before handoff.
