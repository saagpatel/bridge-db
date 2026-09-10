# Durable Ledger Implementation Plan (v2 — hardened 2026-07-10)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> v2 supersedes v1 after a three-agent hardening audit (anchor verification, test-idiom extraction, adversarial review). All file:line anchors below were verified against the repo at HEAD `d145eca`; all test code follows the repo's actual fixtures verbatim.

**Goal:** Make `SHIPPED`/`LEDGER`-tagged activity rows permanently retention-exempt (a durable cross-system ledger), observable (prune audit + health invariant BD-INV-1), surfaced (signal pinning + export), with the coupled v13 INV-13 handoff-claimant fix — per the approved design in `docs/internal/ACTIVITY-LEDGER-DISCOVERY-2026-07-09.md` (§§9–11).

**Architecture:** Exempt-in-place: the prune in `insert_activity_row` becomes one `DELETE ... RETURNING` whose predicate keeps `newest-50 ∪ protected` (Interpretation A). No new table; protected rows stay recall-searchable for free (FTS GC is a NOT-IN sweep keyed to survivors; health `expected` is a plain COUNT — both rebalance automatically). Durability is decided at write time via tags. The v13 migration carries only `pending_handoffs.claimed_by`; the retention change is pure code, no DDL.

**Tech Stack:** Python 3.12+, aiosqlite (SQLite 3.50.4 — `RETURNING` available), FastMCP (stdio), pytest (`asyncio_mode = "auto"` — async tests need NO marker), pyright strict, ruff.

## Global Constraints

- Branch: `feat/durable-ledger` off `main` — never commit to main.
- Verifier after every task: `uv run pytest && uv run pyright && uv run ruff check` (baseline: 345 passed / 0 errors / clean, 2026-07-10).
- Protected tags: exactly `SHIPPED` and `LEDGER`, matched **case-insensitively** via `upper(value)`. Never hardwire other systems' tag names (operator decision 2026-07-10).
- Keep-set: **Interpretation A** — retained = `newest 50 per source ∪ all protected`.
- FTS5 invariant: every base-row delete pairs with `gc_fts_orphans` in the same transaction.
- Audit convention: `log_audit` fires **after** `db.commit()`, never inside the transaction. `ok=False` on audit lines is house-standard for advisory refusals (pick_up_handoff precedent).
- `get_activity_signal` keeps returning a flat `list[dict]`. Documented output ceiling after Task 4: up to `limit + LEDGER_SIGNAL_LIMIT` entries.
- v13 lockstep rule: DDL goes in BOTH the migration ladder AND the fresh-schema block, or `test_fresh_vs_migrated_schema_convergence` fails.
- **Migration is forward-only:** `ensure_schema` refuses `user_version > SCHEMA_VERSION` (db.py:517-520). Once the live DB is at v13, pre-v13 code cannot open it — recovery after merge is fix-forward, never branch-revert. Low mechanical risk (additive column), but the doctrine is on record here.
- No PII in commit messages; conventional commits; no Co-Authored-By trailer.

## Task dependency DAG (do NOT parallelize across an arrow)

```
Task 1 ──► Tasks 2, 4, 5, 6        (consume protected_tags_predicate / InsertActivityResult / config constants)
Task 7 ──► Tasks 8, 9              (consume claimed_by column)
Tasks 3, 10, 11                    independent (any time)
Task 12                            last (merge gate)
```
Recommended order: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12. Safe parallel pairs for subagent execution: {3, 10} alongside anything; {2, 4, 5, 6} among themselves after 1; {8, 9} sequentially after 7 (9 builds on 8's tests).

## Test-harness facts (verified — every new test MUST follow these)

- `CaptureMCP` captures tools into **`.fns`** (dict) — NOT `.tools`. Each test module defines an `fns` fixture:
  ```python
  @pytest.fixture
  def fns(db: aiosqlite.Connection) -> dict[str, Any]:
      cap = CaptureMCP()
      mod.register(cap)
      return cap.fns
  ```
  (`tests/test_activity.py:17-21` — already exists there and in `test_handoffs.py`, `test_health.py`; reuse, don't redefine.)
- `make_ctx` and `CaptureMCP` are **imported from conftest** (`from conftest import CaptureMCP, make_ctx`) — `make_ctx` is a plain function, NOT a fixture. Never put `make_ctx` in a test's parameter list; call `make_ctx(db)` inline.
- Tool functions are closures inside `register()` — reachable ONLY via `fns["<tool_name>"]`, never importable.
- `db` fixture (`conftest.py:35-40`) uses `open_db`, which sets `row_factory = aiosqlite.Row` (`db.py:528`) — `row["col"]` access is valid everywhere, including inside `insert_activity_row`.
- Audit/recall JSONL paths are isolated per-test by the autouse `isolate_jsonl_logs` fixture; `AUTH_MODE` is pinned `"off"`, so `get_principal(ctx)` is None and `gate_identity` falls back to `caller` in tests.
- `test_activity.py` already imports at module top: `json`, `config`, `insert_activity_row`, `CaptureMCP`, `make_ctx`, and `from bridge_db.tools import activity as mod`.
- `test_health.py` has an autouse `patch_db_path` fixture; health tests must `(tmp_path / "test.db").touch()` so `db_exists=True`.

---

### Task 1: Retention exemption for protected tags

**Files:**
- Modify: `src/bridge_db/config.py` (add constant below line 36)
- Modify: `src/bridge_db/db.py:8` (imports), `:722-777` (`insert_activity_row`)
- Modify: `src/bridge_db/__main__.py:398,416,419` (`run_log_session_boundary` — REQUIRED, see Step 3c)
- Test: `tests/test_activity.py` (append after `test_log_activity_retention_keeps_highest_ids_when_created_at_ties`, line ~1024)

**Interfaces:**
- Produces: `config.LEDGER_PROTECTED_TAGS: frozenset[str]`; in `db.py`: `InsertActivityResult(NamedTuple)` with `activity_id: int`, `pruned_rows: list[tuple[int, str]]` (row id, raw tags JSON); `protected_tags_predicate(column: str = "tags") -> tuple[str, list[str]]`; `insert_activity_row(...) -> InsertActivityResult` (was `int`).
- Callers verified: ONLY `__main__.py:398` binds the return (uses it at `:416` and `:419` — MUST switch to `.activity_id`, else the audit line silently interpolates the NamedTuple repr and pyright stays green). `activity.py:224` and all 16 test callsites discard the return — no change needed in this task.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_activity.py` (module already imports `json`, `config`, `insert_activity_row`; no new imports needed):

```python
async def test_protected_rows_survive_retention_prune(db: aiosqlite.Connection) -> None:
    limit = config.ACTIVITY_RETENTION_PER_SOURCE
    await insert_activity_row(
        db, source="cc", timestamp="2026-01-01", project_name="p",
        summary="shipped thing", tags=["SHIPPED"], retention_limit=limit,
    )
    for i in range(limit + 10):
        await insert_activity_row(
            db, source="cc", timestamp="2026-01-02", project_name="p",
            summary=f"noise {i}", retention_limit=limit,
        )
    await db.commit()

    cursor = await db.execute(
        "SELECT COUNT(*) FROM activity_log WHERE source='cc' "
        "AND EXISTS (SELECT 1 FROM json_each(tags) WHERE upper(value)='SHIPPED')"
    )
    row = await cursor.fetchone()
    assert row is not None and row[0] == 1  # survived past the cap

    cursor = await db.execute("SELECT COUNT(*) FROM activity_log WHERE source='cc'")
    row = await cursor.fetchone()
    assert row is not None and row[0] == limit + 1  # newest-50 ∪ protected


async def test_protected_tag_match_is_case_insensitive(db: aiosqlite.Connection) -> None:
    limit = config.ACTIVITY_RETENTION_PER_SOURCE
    await insert_activity_row(
        db, source="cc", timestamp="2026-01-01", project_name="p",
        summary="lowercase ledger", tags=["ledger"], retention_limit=limit,
    )
    for i in range(limit + 5):
        await insert_activity_row(
            db, source="cc", timestamp="2026-01-02", project_name="p",
            summary=f"noise {i}", retention_limit=limit,
        )
    await db.commit()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM activity_log WHERE source='cc' "
        "AND EXISTS (SELECT 1 FROM json_each(tags) WHERE upper(value)='LEDGER')"
    )
    row = await cursor.fetchone()
    assert row is not None and row[0] == 1


async def test_protected_rows_keep_fts_mirror(db: aiosqlite.Connection) -> None:
    limit = config.ACTIVITY_RETENTION_PER_SOURCE
    result = await insert_activity_row(
        db, source="cc", timestamp="2026-01-01", project_name="p",
        summary="durable entry", tags=["LEDGER"], retention_limit=limit,
    )
    for i in range(limit + 5):
        await insert_activity_row(
            db, source="cc", timestamp="2026-01-02", project_name="p",
            summary=f"noise {i}", retention_limit=limit,
        )
    await db.commit()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM content_index WHERE source_type='activity' AND source_id=?",
        (str(result.activity_id),),
    )
    row = await cursor.fetchone()
    assert row is not None and row[0] == 1
    cursor = await db.execute("SELECT COUNT(*) FROM content_index WHERE source_type='activity'")
    fts_row = await cursor.fetchone()
    cursor = await db.execute("SELECT COUNT(*) FROM activity_log")
    base_row = await cursor.fetchone()
    assert fts_row is not None and base_row is not None and fts_row[0] == base_row[0]


async def test_prune_returns_pruned_rows(db: aiosqlite.Connection) -> None:
    limit = config.ACTIVITY_RETENTION_PER_SOURCE
    for i in range(limit):
        await insert_activity_row(
            db, source="cc", timestamp="2026-01-01", project_name="p",
            summary=f"row {i}", retention_limit=limit,
        )
    result = await insert_activity_row(
        db, source="cc", timestamp="2026-01-02", project_name="p",
        summary="the 51st", retention_limit=limit,
    )
    await db.commit()
    assert len(result.pruned_rows) == 1
    pruned_id, pruned_tags = result.pruned_rows[0]
    assert isinstance(pruned_id, int) and pruned_tags == "[]"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_activity.py -k "protected or pruned_rows" -v`
Expected: FAIL — survival counts wrong (row pruned); `AttributeError: 'int' object has no attribute 'activity_id' / 'pruned_rows'`.

- [ ] **Step 3: Implement**

(3a) `src/bridge_db/config.py` — add directly below `ACTIVITY_RETENTION_PER_SOURCE` (line 36):

```python
# Tags whose rows are permanently exempt from activity retention (BD-INV-1).
# Matched case-insensitively. Do not add other systems' tag names here —
# LEDGER is the universal opt-in for durable entries.
LEDGER_PROTECTED_TAGS: frozenset[str] = frozenset({"SHIPPED", "LEDGER"})
```

(3b) `src/bridge_db/db.py`:

Imports — line 8 becomes `from typing import Any, NamedTuple, cast`, and add `from bridge_db import config` to the import block (**`config` is NOT currently imported in db.py — this is required, not optional**).

Add above `insert_activity_row`:

```python
class InsertActivityResult(NamedTuple):
    """Result of insert_activity_row: new row id + rows removed by retention."""

    activity_id: int
    pruned_rows: list[tuple[int, str]]  # (id, raw tags JSON) of pruned rows


def protected_tags_predicate(column: str = "tags") -> tuple[str, list[str]]:
    """SQL fragment matching rows whose tags include a retention-protected tag.

    Case-insensitive by BD-INV-1: a lowercase 'ledger' from any writer must
    still protect the row (every other tag matcher in this codebase is
    exact-case; this one deliberately is not).
    """
    tags = sorted(config.LEDGER_PROTECTED_TAGS)
    placeholders = ", ".join("?" for _ in tags)
    sql = f"EXISTS (SELECT 1 FROM json_each({column}) WHERE upper(value) IN ({placeholders}))"
    return sql, [t.upper() for t in tags]
```

Replace the retention block (`db.py:764-775`, currently the plain `DELETE ... NOT IN (... LIMIT ?)` + `gc_fts_orphans`) with a single-statement `DELETE ... RETURNING` (SQLite 3.50.4 ≥ 3.35; the repo's tests already use `RETURNING`):

```python
    pruned_rows: list[tuple[int, str]] = []
    if retention_limit is not None:
        protected_sql, protected_params = protected_tags_predicate()
        cursor = await db.execute(
            f"""
            DELETE FROM activity_log
            WHERE source = ? AND id NOT IN (
                SELECT id FROM activity_log WHERE source = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
            )
            AND NOT {protected_sql}
            RETURNING id, tags
            """,  # noqa: S608 — predicate assembled from a closed literal set
            (source, source, retention_limit, *protected_params),
        )
        deleted = await cursor.fetchall()
        if deleted:
            pruned_rows = [(row["id"], row["tags"]) for row in deleted]
            await gc_fts_orphans(db, "activity")

    return InsertActivityResult(activity_id=int(activity_id), pruned_rows=pruned_rows)
```

Change the signature to `-> InsertActivityResult` and the docstring first line to: `"""Insert an activity row, keep the FTS mirror in sync, and apply protected-aware retention."""`

(3c) `src/bridge_db/__main__.py` — REQUIRED (silent-corruption risk: the f-strings at `:416`/`:419` would happily render the NamedTuple and pyright stays green). At line 398:

```python
    insert_result = await insert_activity_row(
        db,
        source="cc",
        ...  # existing kwargs unchanged
    )
    activity_id = insert_result.activity_id
```

Lines 416 and 419 keep using `activity_id` unchanged.

(3d) `src/bridge_db/tools/activity.py:224` — leave as a bare `await insert_activity_row(...)` in THIS task (binding an unused variable would trip ruff F841; Task 2 binds it when it's used).

- [ ] **Step 4: Run the new + existing prune tests**

Run: `uv run pytest tests/test_activity.py tests/test_db.py tests/test_cli.py -v`
Expected: new tests PASS; `test_log_activity_prunes_to_retention_limit` (`:982`) and `test_log_activity_retention_keeps_highest_ids_when_created_at_ties` (`:996`) still PASS unchanged (untagged rows — unprotected path identical, including the created_at/id tie-break).

- [ ] **Step 5: Full verifier + commit**

Run: `uv run pytest && uv run pyright && uv run ruff check`
```bash
git add src/bridge_db/config.py src/bridge_db/db.py src/bridge_db/__main__.py tests/test_activity.py
git commit -m "feat(retention): exempt SHIPPED/LEDGER-tagged rows from activity prune (BD-INV-1)"
```

---

### Task 2: Prune audit line (depends: Task 1)

**Files:**
- Modify: `src/bridge_db/tools/activity.py:224-238` (`log_activity`)
- Test: `tests/test_activity.py`

**Interfaces:**
- Consumes: `InsertActivityResult.pruned_rows`; `log_audit` (already imported at `activity.py:13`); `json` (already imported at `:3`).
- Produces: audit JSONL events `tool="log_activity.prune"`, emitted post-commit. Detail format is bounded: `ids_head` carries at most 20 ids (the audit JSONL is append-only and never rotated — an unbounded id list from a bulk-import prune would write a multi-KB line).

- [ ] **Step 1: Write the failing test** (monkeypatch `mod.log_audit` — valid because activity.py imports `log_audit` as a module attribute, so the closure resolves it at call time):

```python
async def test_prune_emits_audit_line(
    db: aiosqlite.Connection, fns: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str | None]] = []

    def fake_log_audit(
        tool: str, caller: str, project: str, ok: bool = True, detail: str | None = None
    ) -> None:
        calls.append((tool, detail))

    monkeypatch.setattr(mod, "log_audit", fake_log_audit)
    ctx = make_ctx(db)
    for i in range(config.ACTIVITY_RETENTION_PER_SOURCE + 1):
        await fns["log_activity"](caller="cc", project_name="p", summary=f"row {i}", ctx=ctx)

    prune_calls = [c for c in calls if c[0] == "log_activity.prune"]
    assert len(prune_calls) == 1  # only the 51st insert pruned anything
    detail = prune_calls[0][1]
    assert detail is not None
    assert "pruned=1" in detail and "ids_head=" in detail and "source=cc" in detail
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_activity.py::test_prune_emits_audit_line -v` → FAIL (no prune audit call).

- [ ] **Step 3: Implement** — in `log_activity`: bind the insert result at line 224 (`insert_result = await insert_activity_row(...)`), then AFTER `await db.commit()` (line 236) and after the existing `log_audit("log_activity", caller, project_name, ok=True)` (line 238), add:

```python
        if insert_result.pruned_rows:
            pruned_ids = [row_id for row_id, _ in insert_result.pruned_rows]
            pruned_tags = sorted(
                {tag for _, raw in insert_result.pruned_rows for tag in json.loads(raw)}
            )
            log_audit(
                "log_activity.prune",
                caller,
                project_name,
                ok=True,
                detail=(
                    f"pruned={len(pruned_ids)} ids_head={pruned_ids[:20]} "
                    f"tags={pruned_tags} source={caller}"
                ),
            )
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_activity.py -v` → PASS.

- [ ] **Step 5: Full verifier + commit**

```bash
git add src/bridge_db/tools/activity.py tests/test_activity.py
git commit -m "feat(retention): audit-log pruned activity rows (count, ids_head, tags) post-commit"
```

---

### Task 3: get_shipped_events hardening (independent)

**Files:**
- Modify: `src/bridge_db/tools/activity.py:119-149` (policy loader), `:440-500` (`get_shipped_events`)
- Test: `tests/test_activity.py` (add `import os` to the module's import block)

**Interfaces:**
- Produces: `get_shipped_events(since, unprocessed_only, limit: int = 200)` (`limit` ge=1, le=1000 — the Notion client already sends `limit`, silently dropped today; this makes it real). `unprocessed_only=True` now ALSO excludes rows having a `shipped_event_dispositions` row (a disposition is terminal — it leaves the feed like PROCESSED does). Module global `_META_POLICY_CACHE: tuple[float, dict[str, Any]] | None = None` (mtime cache — the loader currently re-reads + re-parses the JSON file per returned row).

- [ ] **Step 1: Write the failing tests**

```python
async def test_unprocessed_only_excludes_dispositioned_rows(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc", project_name="A", summary="shipped", tags=["SHIPPED"], ctx=ctx
    )
    cursor = await db.execute("SELECT id FROM activity_log")
    row = await cursor.fetchone()
    assert row is not None
    await db.execute(
        """
        INSERT INTO shipped_event_dispositions (
            activity_id, disposition_type, reason, decided_by
        )
        VALUES (?, 'unsynced_by_policy', 'experimental artifact', 'codex')
        """,
        (row["id"],),
    )
    await db.commit()

    unprocessed = await fns["get_shipped_events"](unprocessed_only=True, ctx=ctx)
    assert unprocessed == []

    everything = await fns["get_shipped_events"](ctx=ctx)
    assert len(everything) == 1
    assert everything[0]["policy_disposition"]["disposition_type"] == "unsynced_by_policy"


async def test_get_shipped_events_honors_limit(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    for i in range(5):
        await fns["log_activity"](
            caller="cc", project_name=f"p{i}", summary=f"ship {i}",
            tags=["SHIPPED"], timestamp=f"2026-07-0{i + 1}", ctx=ctx,
        )
    limited = await fns["get_shipped_events"](limit=2, ctx=ctx)
    assert len(limited) == 2
    assert limited[0]["project_name"] == "p4"  # newest first


def test_meta_policy_cache_is_mtime_keyed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_path = tmp_path / "meta-shipped-events.json"
    policy_path.write_text(
        json.dumps({"projects": {"proj": {"reason": "first"}}}), encoding="utf-8"
    )
    monkeypatch.setattr(config, "META_SHIPPED_EVENTS_PATH", policy_path)
    monkeypatch.setattr(mod, "_META_POLICY_CACHE", None)  # reset the module global

    first = mod._load_meta_shipped_event_policy("proj")
    assert first is not None and first["reason"] == "first"

    # Overwrite content but pin mtime — the cache must serve the old value.
    stat = policy_path.stat()
    policy_path.write_text(
        json.dumps({"projects": {"proj": {"reason": "second"}}}), encoding="utf-8"
    )
    os.utime(policy_path, (stat.st_atime, stat.st_mtime))
    cached = mod._load_meta_shipped_event_policy("proj")
    assert cached is not None and cached["reason"] == "first"

    # Bump mtime — the cache must refresh.
    os.utime(policy_path, (stat.st_atime, stat.st_mtime + 10))
    refreshed = mod._load_meta_shipped_event_policy("proj")
    assert refreshed is not None and refreshed["reason"] == "second"
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_activity.py -k "dispositioned or honors_limit or mtime_keyed" -v` → FAIL (dispositioned row returned; `TypeError: unexpected keyword argument 'limit'`; second read returns "second").

- [ ] **Step 3: Implement**

(a) Signature — add after `unprocessed_only`:

```python
        limit: Annotated[
            int, Field(description="Max shipped events to return, newest first", ge=1, le=1000)
        ] = 200,
```

(b) Extend the `unprocessed_only` branch (`activity.py:460-463`):

```python
        if unprocessed_only:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM json_each(tags) WHERE value = 'PROCESSED')"
            )
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM shipped_event_dispositions AS d2 "
                "WHERE d2.activity_id = a.id)"
            )
```

(c) After `ORDER BY a.timestamp DESC` (`:494`) add `LIMIT ?`, and `params.append(limit)` immediately before the `db.execute`.

(d) Replace the file-read head of the policy loader with an mtime-cached root loader (module-level, above `_load_meta_shipped_event_policy`):

```python
_META_POLICY_CACHE: tuple[float, dict[str, Any]] | None = None


def _load_meta_policy_root() -> dict[str, Any]:
    global _META_POLICY_CACHE
    try:
        mtime = config.META_SHIPPED_EVENTS_PATH.stat().st_mtime
    except OSError:
        return {}
    if _META_POLICY_CACHE is not None and _META_POLICY_CACHE[0] == mtime:
        return _META_POLICY_CACHE[1]
    try:
        raw = json.loads(config.META_SHIPPED_EVENTS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    root = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
    _META_POLICY_CACHE = (mtime, root)
    return root
```

In `_load_meta_shipped_event_policy` (`:123`), replace the `try: raw = json.loads(...read_text...)` block with `policy_root = _load_meta_policy_root()` and keep everything from `projects = policy_root.get("projects")` down unchanged (`cast` is already imported at `:6`).

- [ ] **Step 4: Run** — `uv run pytest tests/test_activity.py -v` → PASS (existing shipped tests seed ≤2 rows — default limit 200 preserves their behavior).

- [ ] **Step 5: Full verifier + commit**

```bash
git add src/bridge_db/tools/activity.py tests/test_activity.py
git commit -m "feat(shipped): real limit param, dispositions leave unprocessed feed, mtime-cached meta policy"
```

---

### Task 4: Pinned ledger entries in get_activity_signal (depends: Task 1)

**Files:**
- Modify: `src/bridge_db/config.py` (add `LEDGER_SIGNAL_LIMIT`), `src/bridge_db/tools/activity.py:312-438` (`get_activity_signal`)
- Test: `tests/test_activity.py`

**Interfaces:**
- Consumes: `protected_tags_predicate` (import in activity.py: extend the existing `from bridge_db.db import ...` line); `_LIFECYCLE_ACTIVITY_SQL` (module constant, `activity.py:33-41`); `_activity_payload(row, *, kind=None)` (verified: accepts `kind`, emits `id`).
- Produces: signal entries with `kind: "ledger"` — same payload keys as `kind: "activity"` — PREPENDED to the flat list, capped at `config.LEDGER_SIGNAL_LIMIT = 10`, deduplicated against the substantive window by id, and excluded from lifecycle rows (a protected session-boundary row must not surface twice — once aggregated, once pinned). **Documented output ceiling: up to `limit + LEDGER_SIGNAL_LIMIT` entries** (`limit` governs only non-ledger entries). Pins respect the `source` and `since` filters — deliberate: `source` scoping is correct for per-system reads, and `since` is an explicit operator narrowing that should win over pinning; the wired `/start` call passes neither.

- [ ] **Step 1: Write the failing tests**

```python
async def test_signal_pins_protected_rows_beyond_window(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc", project_name="p", summary="durable milestone",
        tags=["SHIPPED"], timestamp="2026-01-01", ctx=ctx,
    )
    for i in range(config.ACTIVITY_RETENTION_PER_SOURCE + 10):
        await fns["log_activity"](
            caller="cc", project_name="p", summary=f"noise {i}",
            timestamp="2026-01-02", ctx=ctx,
        )

    signal = await fns["get_activity_signal"](limit=5, ctx=ctx)
    ledger = [e for e in signal if e["kind"] == "ledger"]
    assert len(ledger) == 1
    assert ledger[0]["summary"] == "durable milestone"
    assert len([e for e in signal if e["kind"] != "ledger"]) <= 5


async def test_signal_ledger_dedupes_recent_protected(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["log_activity"](
        caller="cc", project_name="p", summary="fresh ship", tags=["SHIPPED"], ctx=ctx
    )
    signal = await fns["get_activity_signal"](limit=10, ctx=ctx)
    matches = [e for e in signal if e["summary"] == "fresh ship"]
    assert len(matches) == 1
    assert matches[0]["kind"] == "ledger"


async def test_signal_stays_flat_list(db: aiosqlite.Connection, fns: dict[str, Any]) -> None:
    signal = await fns["get_activity_signal"](ctx=make_ctx(db))
    assert isinstance(signal, list)
    assert all(isinstance(e, dict) and "kind" in e for e in signal)
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_activity.py -k "signal_pins or signal_ledger or flat_list" -v` → FAIL (no `kind=="ledger"` entries).

- [ ] **Step 3: Implement**

`config.py` — below `LEDGER_PROTECTED_TAGS`:

```python
# Max pinned ledger entries returned by get_activity_signal (and mirrored by
# cold-start surfacing). Small on purpose: recall covers the long tail.
LEDGER_SIGNAL_LIMIT: int = 10
```

In `get_activity_signal`, after the substantive query result (`substantive_rows`, line ~432) — note the house pattern: `params` holds ONLY the source/since filter params; `substantive_params = [*params, limit]` at `:421` copies rather than mutates; do the same:

```python
        protected_sql, protected_params = protected_tags_predicate()
        ledger_conditions = [*conditions, protected_sql, f"NOT {_LIFECYCLE_ACTIVITY_SQL}"]
        ledger_where = "WHERE " + " AND ".join(ledger_conditions)
        ledger_cursor = await db.execute(
            f"""
            SELECT id, source, timestamp, project_name, summary, branch, tags, created_at, canonical_key, source_trust
            FROM activity_log
            {ledger_where}
            ORDER BY timestamp DESC, created_at DESC, id DESC
            LIMIT ?
            """,
            [*params, *protected_params, config.LEDGER_SIGNAL_LIMIT],
        )
        ledger_rows = await ledger_cursor.fetchall()
        ledger_ids = {r["id"] for r in ledger_rows}
        ledger_entries = [_activity_payload(r, kind="ledger") for r in ledger_rows]
```

Then change the final assembly (`:434-438`) to dedupe and prepend (pins bypass `_select_activity_signal_entries`):

```python
        entries = [
            *aggregates.values(),
            *[
                _activity_payload(r, kind="activity")
                for r in substantive_rows
                if r["id"] not in ledger_ids
            ],
        ]
        return [*ledger_entries, *_select_activity_signal_entries(entries, limit)]
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_activity.py tests/test_provenance_boundary.py -v` → PASS (verified: existing signal tests at `test_activity.py:193-399` seed NO SHIPPED/LEDGER rows, so their ledger sets are empty and length assertions hold; SHIPPED-seeding tests start at `:410` and don't call the signal tool).

- [ ] **Step 5: Full verifier + commit**

```bash
git add src/bridge_db/config.py src/bridge_db/tools/activity.py tests/test_activity.py
git commit -m "feat(signal): pin protected ledger rows as kind=ledger entries in get_activity_signal"
```

---

### Task 5: BD-INV-1 health metrics + dogfood gate (depends: Task 1)

**Files:**
- Modify: `src/bridge_db/tools/health.py:128-214` (`collect_health_metrics`), `src/bridge_db/__main__.py:268-300` (`run_dogfood` print block + gate)
- Test: `tests/test_health.py`

**Interfaces:**
- Consumes: `protected_tags_predicate` (import from `bridge_db.db` in health.py).
- Produces: three new top-level keys in `collect_health_metrics`'s return dict: `ledger_protected_count: int`, `receipt_orphan_count: int`, `disposition_orphan_count: int`. Dogfood gate additionally requires both orphan counts == 0. **Surface decision (deliberate):** `--status` / `collect_status_summary`'s curated `signals` are intentionally NOT extended — `--dogfood` (which reads `collect_health_metrics` directly) is the BD-INV-1 verification surface; Task 12 verifies there. BD-INV-1: *"Retention never deletes a protected row, its receipt, or its disposition"* — enforced by the Task-1 predicate, the Task-2 prune audit line, and these counts.

- [ ] **Step 1: Write the failing tests** (test_health.py has autouse `patch_db_path`; tests must `.touch()` the db file; `insert_activity_row` needs importing there — extend the existing `from bridge_db.db import (...)` block):

```python
async def test_health_reports_ledger_and_orphan_metrics(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / "test.db").touch()
    result = await fns["health"](ctx=make_ctx(db))
    assert result["ledger_protected_count"] == 0
    assert result["receipt_orphan_count"] == 0
    assert result["disposition_orphan_count"] == 0


async def test_health_counts_protected_rows(
    db: aiosqlite.Connection, fns: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / "test.db").touch()
    await insert_activity_row(
        db, source="cc", timestamp="2026-01-01", project_name="p",
        summary="durable", tags=["LEDGER"],
    )
    await db.commit()
    result = await fns["health"](ctx=make_ctx(db))
    assert result["ledger_protected_count"] == 1
```

(If the `health` tool nests `collect_health_metrics` output rather than spreading it top-level, assert wherever `db_exists`/`schema_version` live — those are metrics keys and the existing `test_health_returns_ok_on_healthy_db` asserts them top-level, so top-level is expected.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_health.py -k "ledger or orphan" -v` → FAIL (KeyError).

- [ ] **Step 3: Implement**

In `collect_health_metrics`, after the `processed_shipped_without_receipt_count` block (line ~170):

```python
    protected_sql, protected_params = protected_tags_predicate()
    cursor = await db.execute(
        f"SELECT COUNT(*) FROM activity_log WHERE {protected_sql}",  # noqa: S608
        protected_params,
    )
    ledger_row = await cursor.fetchone()
    ledger_protected_count: int = ledger_row[0] if ledger_row else 0

    cursor = await db.execute(
        "SELECT COUNT(*) FROM shipped_sync_receipts AS r "
        "WHERE NOT EXISTS (SELECT 1 FROM activity_log AS a WHERE a.id = r.activity_id)"
    )
    receipt_orphan_row = await cursor.fetchone()
    receipt_orphan_count: int = receipt_orphan_row[0] if receipt_orphan_row else 0

    cursor = await db.execute(
        "SELECT COUNT(*) FROM shipped_event_dispositions AS d "
        "WHERE NOT EXISTS (SELECT 1 FROM activity_log AS a WHERE a.id = d.activity_id)"
    )
    disposition_orphan_row = await cursor.fetchone()
    disposition_orphan_count: int = disposition_orphan_row[0] if disposition_orphan_row else 0
```

Add the three keys to the returned dict beside the shipped counts. In `__main__.py` `run_dogfood`: add a print line in the `:268-291` block (after the WAL line at `:280`, matching neighboring format):

```python
    print(
        f"  Ledger: protected={health['ledger_protected_count']} "
        f"receipt_orphans={health['receipt_orphan_count']} "
        f"disposition_orphans={health['disposition_orphan_count']}"
    )
```

and extend the gate (`:293-300`) — the new conjuncts read `health[...]` (NOT `summary["signals"]` — the metrics live in `collect_health_metrics`'s dict, bound to `health` at `:256`):

```python
    return bool(
        summary["ok"]
        and summary["signals"]["pending_handoffs"] == 0
        and summary["signals"]["actionable_unprocessed_shipped"] == 0
        and summary["signals"]["processed_shipped_without_receipt"] == 0
        and health["fts_index"]["ok"]
        and not health["wal_warning"]
        and health["receipt_orphan_count"] == 0
        and health["disposition_orphan_count"] == 0
    )
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_health.py tests/test_cli.py -v` → PASS (verified: only `metrics["auth"]` is asserted exhaustively in existing health tests; three added top-level keys are safe).

- [ ] **Step 5: Full verifier + commit**

```bash
git add src/bridge_db/tools/health.py src/bridge_db/__main__.py tests/test_health.py
git commit -m "feat(health): BD-INV-1 ledger metrics + receipt/disposition orphan gates in dogfood"
```

---

### Task 6: Pinned Ledger section in export_bridge_markdown (depends: Task 1)

**Files:**
- Modify: `src/bridge_db/tools/export.py` (query after the extra-sources loop ending `:236`; assembly at `:287-291`)
- Test: `tests/test_export.py`

**Interfaces:**
- Consumes: `protected_tags_predicate` (import from `bridge_db.db` in export.py); `_render_activity_rows` (`export.py:170-179` — reads exactly `timestamp, project_name, summary, branch, tags`, the same columns the ledger query selects).
- Produces: `## Pinned Ledger` section — newest 15 protected rows across ALL sources — after the per-source activity sections; omitted when empty.

- [ ] **Step 1: Write the failing tests** (test_export.py imports `build_markdown as _build_markdown` and calls it with positional `db`; add `insert_activity_row` to its imports):

```python
async def test_export_renders_pinned_ledger_section(
    db: aiosqlite.Connection, all_fns: dict[str, Any]
) -> None:
    await insert_activity_row(
        db, source="cc", timestamp="2026-01-01", project_name="p",
        summary="durable milestone", tags=["LEDGER"],
    )
    await db.commit()
    md = await _build_markdown(db)
    assert "## Pinned Ledger" in md
    assert "durable milestone" in md


async def test_export_omits_pinned_ledger_when_empty(
    db: aiosqlite.Connection, all_fns: dict[str, Any]
) -> None:
    md = await _build_markdown(db)
    assert "## Pinned Ledger" not in md
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_export.py -k pinned -v` → FAIL.

- [ ] **Step 3: Implement** — after the extra-sources loop (ends `:236`):

```python
    # --- Pinned Ledger (retention-protected rows, all sources) ---
    protected_sql, protected_params = protected_tags_predicate()
    cursor = await db.execute(
        "SELECT timestamp, project_name, summary, branch, tags FROM activity_log "
        f"WHERE {protected_sql} "  # noqa: S608
        "ORDER BY timestamp DESC, created_at DESC, id DESC LIMIT 15",
        protected_params,
    )
    ledger_rows = await cursor.fetchall()
    ledger_md = ""
    if ledger_rows:
        ledger_md = "## Pinned Ledger\n" + "\n".join(_render_activity_rows(ledger_rows)) + "\n"
```

Assembly — the verified tail is (`export.py:281-291`): `parts.append(cc_activity_md)` ... `parts.append(codex_activity_md)` then the `for extra_md in extra_activity_mds:` loop (`:287-289`) then `return "\n".join(parts)` (`:291`). Insert between the loop and the return:

```python
    if ledger_md:
        parts.append("")
        parts.append(ledger_md)
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_export.py -v` → PASS.

- [ ] **Step 5: Full verifier + commit**

```bash
git add src/bridge_db/tools/export.py tests/test_export.py
git commit -m "feat(export): Pinned Ledger section mirrors retention-protected rows"
```

---

### Task 7: v13 migration — pending_handoffs.claimed_by (independent of 1-6)

**Files:**
- Modify: `src/bridge_db/db.py:15` (`SCHEMA_VERSION = 12` → `13`), `:93-106` (fresh-schema `pending_handoffs` — `source_trust` is the last column at `:105`; it needs a trailing comma), after the `_MIGRATION_V11_TO_V12` string (starts `:422`), ladder list (`:492-504` — last entry `(12, _MIGRATION_V11_TO_V12, None),` at `:503`)
- Test: existing `tests/test_migration.py` + `tests/test_schema_convergence_concurrency.py::test_fresh_vs_migrated_schema_convergence` are the gate

**Interfaces:**
- Produces: `pending_handoffs.claimed_by TEXT` (NULL for all pre-v13 and unclaimed rows). Tasks 8-9 consume it.
- **Forward-only:** after the live DB reaches v13, pre-v13 code refuses to open it (`db.py:517-520`). Post-merge recovery is fix-forward only.

- [ ] **Step 1: Baseline** — `uv run pytest tests/test_schema_convergence_concurrency.py -v` → PASS.

- [ ] **Step 2: The three lockstep edits**

```python
SCHEMA_VERSION = 13
```

Fresh block — `:105` gains a trailing comma; add below it:

```sql
    claimed_by TEXT
```

After `_MIGRATION_V11_TO_V12` ends:

```python
_MIGRATION_V12_TO_V13 = "ALTER TABLE pending_handoffs ADD COLUMN claimed_by TEXT;\n"
```

Ladder — append after `:503`:

```python
        (13, _MIGRATION_V12_TO_V13, None),
```

- [ ] **Step 3: Run** — `uv run pytest tests/test_migration.py tests/test_schema_convergence_concurrency.py -v` → PASS (the convergence test dumps fresh vs v1→HEAD-migrated schemas and asserts identical — it fails if any of the three edits is missing).

- [ ] **Step 4: Full verifier + commit**

```bash
git add src/bridge_db/db.py
git commit -m "feat(schema): v13 - pending_handoffs.claimed_by claimant column (forward-only)"
```

---

### Task 8: pick_up_handoff records the claimant (depends: Task 7)

**Files:**
- Modify: `src/bridge_db/tools/handoffs.py:229-236` (CAS UPDATE), `:285-292` (response dict)
- Test: `tests/test_handoffs.py`

**Interfaces:**
- Consumes: `claimed_by` column; existing `gate_identity = get_principal(ctx) or caller` (`handoffs.py:192`; `get_principal` already imported at `:11`).
- Produces: `claimed_by` persisted at claim time = `gate_identity`; response gains `"claimed_by"`. Under the test suite `AUTH_MODE` is `off`, so `gate_identity == caller`.

- [ ] **Step 1: Write the failing test** (mirrors `test_handoff_lifecycle_across_pending_pickup_and_clear` at `test_handoffs.py:438` — codex pickup requires `source_trust="operator"` on create to pass the provenance gate):

```python
async def test_pick_up_records_claimant(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    created = await fns["create_handoff"](
        caller="claude_ai",
        project_name="ClaimProj",
        project_path="/tmp/claimproj",
        phase="Phase 1",
        source_trust="operator",
        ctx=ctx,
    )
    picked = await fns["pick_up_handoff"](
        caller="codex", handoff_id=created["handoff_id"], ctx=ctx
    )
    assert picked["status"] == "active"
    assert picked["claimed_by"] == "codex"

    cursor = await db.execute(
        "SELECT claimed_by FROM pending_handoffs WHERE id = ?", (created["handoff_id"],)
    )
    row = await cursor.fetchone()
    assert row is not None and row["claimed_by"] == "codex"
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_handoffs.py::test_pick_up_records_claimant -v` → FAIL.

- [ ] **Step 3: Implement** — CAS UPDATE (`:229-236`) becomes:

```python
        cursor = await db.execute(
            """
            UPDATE pending_handoffs
            SET status = 'active',
                picked_up_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                claimed_by = ?
            WHERE id = ? AND status = 'pending'
            """,
            (gate_identity, handoff_id),
        )
```

and the success response dict (`:285-292`) gains `"claimed_by": gate_identity,`.

- [ ] **Step 4: Run** — `uv run pytest tests/test_handoffs.py -v` → PASS.

- [ ] **Step 5: Full verifier + commit**

```bash
git add src/bridge_db/tools/handoffs.py tests/test_handoffs.py
git commit -m "feat(handoffs): record claimant identity on pick_up_handoff"
```

---

### Task 9: clear_handoff claimant gate — INV-13 (depends: Task 8)

**Files:**
- Modify: `src/bridge_db/tools/handoffs.py:294-356` (`clear_handoff` — SELECT+UPDATE core at `:321-343`)
- Test: `tests/test_handoffs.py`

**Interfaces:**
- Consumes: `claimed_by`; `get_principal` + `log_audit` (both already imported).
- Produces semantics: **`pending` rows always clearable** (preserves the opportunistic `/finish`–`/bank` contract); **`active` rows clearable only when `claimed_by` is NULL (legacy pre-v13) or equals `gate_identity`**. Refusals reported, not raised: response gains `refused_ids: list[int]`, `refused_count: int`; all-refused gets a `reason`; refusals emit `log_audit("clear_handoff.refused_foreign_claim", ..., ok=False)` (house-consistent: pick_up_handoff uses `ok=False` for advisory refusals). `ok=True` on refusal is deliberate — consistent with the opportunistic no-op contract, unlike pick_up's hard refusals; say so in the docstring. Scope honesty for the docstring: all cc windows share one principal, so this protects **cross-role (cc↔codex) only**; under live `warn` auth it is accident-safety, not adversarial protection.

- [ ] **Step 1: Write the failing tests**

```python
async def test_clear_refuses_other_roles_active_claim(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    created = await fns["create_handoff"](
        caller="claude_ai", project_name="ForeignClaim",
        project_path="/tmp/fc", phase="Phase 1", source_trust="operator", ctx=ctx,
    )
    await fns["pick_up_handoff"](caller="codex", handoff_id=created["handoff_id"], ctx=ctx)

    result = await fns["clear_handoff"](caller="cc", project_name="ForeignClaim", ctx=ctx)
    assert result["ok"] is True
    assert result["cleared"] is False
    assert result["refused_count"] == 1
    assert result["refused_ids"] == [created["handoff_id"]]
    assert "reason" in result

    cursor = await db.execute(
        "SELECT status FROM pending_handoffs WHERE id = ?", (created["handoff_id"],)
    )
    row = await cursor.fetchone()
    assert row is not None and row["status"] == "active"


async def test_clear_allows_own_active_claim(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    created = await fns["create_handoff"](
        caller="claude_ai", project_name="OwnClaim",
        project_path="/tmp/oc", phase="Phase 1", source_trust="operator", ctx=ctx,
    )
    await fns["pick_up_handoff"](caller="codex", handoff_id=created["handoff_id"], ctx=ctx)

    result = await fns["clear_handoff"](caller="codex", project_name="OwnClaim", ctx=ctx)
    assert result["cleared"] is True
    assert result["cleared_count"] == 1
    assert result["refused_count"] == 0


async def test_clear_allows_unclaimed_pending(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    await fns["create_handoff"](
        caller="claude_ai", project_name="NeverClaimed",
        project_path="/tmp/nc", phase="Phase 1", ctx=ctx,
    )
    result = await fns["clear_handoff"](caller="cc", project_name="NeverClaimed", ctx=ctx)
    assert result["cleared"] is True
    assert result["refused_count"] == 0


async def test_clear_allows_legacy_null_claimant_active(
    db: aiosqlite.Connection, fns: dict[str, Any]
) -> None:
    ctx = make_ctx(db)
    created = await fns["create_handoff"](
        caller="claude_ai", project_name="LegacyActive",
        project_path="/tmp/la", phase="Phase 1", source_trust="operator", ctx=ctx,
    )
    await fns["pick_up_handoff"](caller="codex", handoff_id=created["handoff_id"], ctx=ctx)
    # Simulate a pre-v13 active row: claimant identity was never recorded.
    await db.execute(
        "UPDATE pending_handoffs SET claimed_by = NULL WHERE id = ?",
        (created["handoff_id"],),
    )
    await db.commit()

    result = await fns["clear_handoff"](caller="cc", project_name="LegacyActive", ctx=ctx)
    assert result["cleared"] is True
    assert result["cleared_count"] == 1
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_handoffs.py -k "refuses_other_roles or own_active or unclaimed_pending or legacy_null" -v` → the refusal test FAILS (today everything clears); the three allow-tests pass (KeyError on `refused_count` makes them fail too — either way, red first).

- [ ] **Step 3: Implement** — replace `:321-343` (SELECT ids → blanket UPDATE) with:

```python
        gate_identity = get_principal(ctx) or caller

        cursor = await db.execute(
            f"""
            SELECT id, status, claimed_by
            FROM pending_handoffs
            WHERE {match_sql} AND status != 'cleared'
            ORDER BY dispatched_at DESC, id DESC
            """,
            match_params,
        )
        rows = await cursor.fetchall()
        if not rows:
            # Not an error — handoff may not exist; /end calls this opportunistically
            return {"ok": True, "cleared": False, "reason": "No active handoff found for project"}

        clearable_ids: list[int] = []
        refused_ids: list[int] = []
        for row in rows:
            claimant = row["claimed_by"]
            if row["status"] == "active" and claimant is not None and claimant != gate_identity:
                refused_ids.append(row["id"])
            else:
                clearable_ids.append(row["id"])

        if clearable_ids:
            id_placeholders = ", ".join("?" for _ in clearable_ids)
            await db.execute(
                f"""
                UPDATE pending_handoffs
                SET status = 'cleared', cleared_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                WHERE id IN ({id_placeholders})
                """,  # noqa: S608
                clearable_ids,
            )
        await db.commit()

        if refused_ids:
            log_audit(
                "clear_handoff.refused_foreign_claim",
                caller,
                project_name,
                ok=False,
                detail=f"refused_ids={refused_ids} gate_identity={gate_identity}",
            )

        if not clearable_ids:
            return {
                "ok": True,
                "cleared": False,
                "reason": "All matched handoffs are actively claimed by another identity",
                "refused_ids": refused_ids,
                "refused_count": len(refused_ids),
                "project_name": project_name,
                "canonical_key": canonical,
            }

        return {
            "ok": True,
            "cleared": True,
            "handoff_id": clearable_ids[0],
            "handoff_ids": clearable_ids,
            "cleared_count": len(clearable_ids),
            "refused_ids": refused_ids,
            "refused_count": len(refused_ids),
            "project_name": project_name,
            "canonical_key": canonical,
        }
```

Update the docstring with the semantics + the scope-honesty and ok-asymmetry notes from the Interfaces block. (The existing `logger.info` clear line should use `len(clearable_ids)`.)

- [ ] **Step 4: Run the whole handoff suite** — `uv run pytest tests/test_handoffs.py -v` → PASS. Verified expectations for the existing suite: `test_clear_handoff_by_project_name`, both canonical-alias clear tests, and `test_clear_handoff_missing_project_returns_ok` clear PENDING rows → pass unchanged. `test_clear_handoff_clears_all_matching_rows` picks up AND clears as the same caller under `principal=None`, so `claimed_by == gate_identity` → **passes UNCHANGED with cleared_count still 2 — do NOT edit its assertion** (the discovery doc's "2→1" prediction was wrong). `test_handoff_lifecycle_across_pending_pickup_and_clear` (codex→codex) passes unchanged.

- [ ] **Step 5: Full verifier + commit**

```bash
git add src/bridge_db/tools/handoffs.py tests/test_handoffs.py
git commit -m "fix(handoffs): gate active-handoff clears on claimant identity (INV-13, cross-role)"
```

---

### Task 10: log_activity API contract — tag vocabulary + retention docs (independent)

**Files:**
- Modify: `src/bridge_db/tools/activity.py:185-216` (tags Field description + docstring)

- [ ] **Step 1: Replace the tags Field description** (currently advertises five retired tags):

```python
        tags: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Optional tags (indexed for recall). SHIPPED = durable ship "
                    "event, syncs to the Notion Build Log and is retention-"
                    "protected. LEDGER = durable catch-up entry ('what happened "
                    "/ what it does / what it points to'), retention-protected, "
                    "pinned by get_activity_signal. Both match case-"
                    "insensitively. Other tags are free-form and searchable but "
                    "NOT retention-protected."
                )
            ),
        ] = None,
```

- [ ] **Step 2: Replace the docstring** (through the end of the tag-conventions block):

```python
        """Log a session activity entry.

        Retention (BD-INV-1): unprotected entries auto-prune to the most recent
        50 per source; entries tagged SHIPPED or LEDGER (case-insensitive) are
        NEVER pruned. Every prune emits a `log_activity.prune` audit line.

        Tag conventions (tags are indexed in content_index, so they are recall-able):
        - SHIPPED: a feature/artifact reached a durable, usable state. Requires an
          eventual confirm_shipped_sync receipt or record_shipped_event_disposition —
          unsynced SHIPPED rows nag in health until terminally resolved.
        - LEDGER: a durable operator-facing record for the next agent's catch-up.
          Attach when the operator says "log this to BridgeDB" or the entry should
          outlive the rolling window.
        - Anything else: free-form, searchable, prunable.
        """
```

- [ ] **Step 3: Verifier + commit**

Run: `uv run pytest && uv run pyright && uv run ruff check`
```bash
git add src/bridge_db/tools/activity.py
git commit -m "docs(api): log_activity tag vocabulary - SHIPPED/LEDGER retention semantics"
```

---

### Task 11: In-repo doc reconciliation (independent)

**Files:**
- Modify: `CLAUDE.md` (Conventions "Activity retention: 50 per source" line + Gotchas SessionEnd/shipped entries + schema line "Schema at v12" → v13 note), `README.md` (~line 196 buffer/ledger split, ~line 24 scope line), `AGENTS.md` (~line 24), `ROADMAP.md` (~lines 156-188 scope pin), `docs/internal/OPERATOR-CHECKLIST.md` (~lines 195-211), `integration-spec.md` (SHIPPED lifecycle lines ~140-157)

Load-bearing new language (adapt per file, keep each file's voice):

> Activity retention: unprotected rows keep the newest 50 per source; rows tagged
> `SHIPPED` or `LEDGER` (case-insensitive) are permanently retained — **BD-INV-1:
> retention never deletes a protected row, its receipt, or its disposition.**
> Enforced by the prune predicate, the `log_activity.prune` audit line, and the
> health orphan/ledger metrics. The durable ledger is cross-system *state*
> coordination (ROADMAP's own reopening clause), not a knowledge store — the
> semantic-search scope closure stands.

Also: OPERATOR-CHECKLIST's "activity_log ... not a proof ledger" paragraph → two-tier reality (protected rows ARE durable; receipts can no longer cascade-die because their parent rows never prune — state this is intentional, so a reviewer doesn't "fix" the FK separately). ROADMAP records the reopening decision dated 2026-07-10 with a pointer to `ACTIVITY-LEDGER-DISCOVERY-2026-07-09.md`. CLAUDE.md's shipped-sync gotcha gains: dispositioned rows no longer appear under `unprocessed_only`.

- [ ] **Step 1: Apply the edits** (read each target section first; minimal surgical diffs).
- [ ] **Step 2: Verifier + commit**

```bash
git add CLAUDE.md README.md AGENTS.md ROADMAP.md docs/internal/OPERATOR-CHECKLIST.md integration-spec.md
git commit -m "docs: reconcile retention/ledger split with BD-INV-1 across repo docs"
```

---

### Task 12: Merge gate (last)

- [ ] **Step 1: Full verifier** — `uv run pytest && uv run pyright && uv run ruff check` → all green.
- [ ] **Step 2: Review the branch diff** — `git diff main...feat/durable-ledger` — demand-elegance pass + `/code-review` (python-reviewer route; the diff exceeds 200 lines).
- [ ] **Step 3: Post-merge live smoke** — first server start migrates the live DB to v13 (**forward-only — after this, no branch-revert; fix-forward only**). Then `uv run python -m bridge_db --dogfood`: expect the new `Ledger:` line reporting `protected=15` (the 15 live SHIPPED rows), both orphan counts 0. (`--status` intentionally does NOT carry the new metrics — dogfood is the BD-INV-1 surface.) Note: dogfood may legitimately show unrelated reds (e.g. pending handoffs) — the check here is the Ledger line and orphan gates, not a blanket green.
- [ ] **Step 4: Operator merges + pushes** (operator-gated per house rules).

---

## Phase 2 — Estate wiring (after repo merge; some steps operator-gated)

- [ ] **P2.1 `~/.claude/rules/agent-division.md`** — tag table gains `LEDGER` ("durable catch-up record; retention-protected; attach when the operator says 'log this to BridgeDB'"); note SHIPPED is now retention-protected too. Edit tool only.
- [ ] **P2.2 `~/.claude/skills/_shared/bridge-log-protocol.md` + `bank`/`finish` skills** — attach `LEDGER` on operator-directed durable logs. Edit tool only.
- [ ] **P2.3 `~/.claude/skills/start/SKILL.md`** — after the `get_recent_activity(source="cc", limit=5)` step (line ~44), add: call `get_activity_signal(limit=5)` and internalize the `kind=="ledger"` pinned entries. NOTE the contract: ledger pins bypass `limit`, so this returns up to 5 substantive + up to 10 pinned = up to 15 entries.
- [ ] **P2.4 `~/.claude/hooks/bridge-db-recall-warmup.sh`** — the hook runs its own `sqlite3 -readonly` CTE query (`WITH scoped/lifecycle/substantive`, outer SELECT is POSITIONAL on `bucket, source, text`). Add a `UNION ALL` ledger branch selecting the SAME column shape, project-scoped, `EXISTS (SELECT 1 FROM json_each(scoped.tags) WHERE upper(value) IN ('SHIPPED','LEDGER'))`, with its OWN `LIMIT 5` — and keep it OUTSIDE the final overall `LIMIT 5` (or raise that limit), else a busy project's recent rows bury the pins. Edit tool only. **OPERATOR STEP after edit:** `! bash ~/.claude/hooks/hook-integrity-regen.sh` (re-bless the hook hash; otherwise an advisory drift alert fires every session start).
- [ ] **P2.5 (optional, operator call)** `~/.codex/config.toml` — wire `get_activity_signal` for Codex.

## Follow-ups from execution (final whole-branch review, 2026-07-10 — log with the parked list)

- Harden the post-commit prune-audit block: try/except around `json.loads(raw)` in
  `log_activity` so a corrupt tags row can't fail a committed write (activity.py ~291).
- Add an export Pinned-Ledger cap test (LIMIT 15 / cross-source / ordering) next time
  test_export.py is touched — the cap is load-bearing as the ledger grows.
- Task 9 hardening pair: one-line DB-status assertion in the own-claim clear test, and
  (when picked up) repeat the claimant predicate in clear_handoff's UPDATE WHERE to
  close the residual gate-bypass race window.
- One clarifying clause in log_activity's tags description: case-insensitivity applies
  to retention protection; the shipped sync feed and health nags match exact-case
  SHIPPED.
- Cosmetic, next db.py hygiene commit only: align `_MIGRATION_V12_TO_V13` to the
  triple-quoted sibling style.

## Parked / follow-ups (explicitly NOT this train)

- Stale-handoff reap (health staleness is advisory-only; no mutation path exists) — future hygiene pass.
- cc session-identity primitive (makes INV-13 meaningful cc↔cc) — larger auth change.
- `vibe-code-handoff` skill's dead `create_handoff(caller:"cc")` path — pre-existing, separate cleanup.
- Tag-vocabulary drift cleanup (30 live values; retired tags dominate) — decide after LEDGER lands.
- Notion client polish (pass `since`; update limit-forwarding test values) — server-side Task 3 already bounds the feed.
- Two-bucket boundary cap — only if cc's unprotected window still feels crowded after this ships.
