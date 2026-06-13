# Stage 1: Authenticated Channel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

---

## STATUS — COMPLETE (2026-06-12)

**Implemented and verified on branch `feat/stage1-authenticated-channel` (HEAD `bf60f5a`, 17 commits).** All 10 tasks below shipped via subagent-driven TDD with per-task two-stage review (spec + quality). Final gate: **259 tests pass, pyright strict clean, ruff clean.** Branch is local + pushed to `origin`; **not merged** (operator's call).

Post-plan review fixes from the 7-angle `/code-review` gate (beyond the original 10 tasks):
- `175b2a7` — **C1 (high):** `pick_up_handoff` provenance gate now keys on the bound principal (`get_principal(ctx) or caller`), closing a warn-mode bypass where a codex-bound connection could spoof `caller='cc'` past the Codex refusal.
- `bf60f5a` — **C4/C5 (medium):** an autouse fixture pins `AUTH_MODE='off'` for tests (the suite was silently env-coupled); a blank `BRIDGE_DB_PRINCIPAL_TOKEN` is now audited as a bind failure instead of binding silently unbound.

Deferred — surfaced, non-blocking (code defaults to `off`, so production is unchanged until the operator flips the env var):
- 20 live `pending_handoffs` rows carry migration-set `source_trust='operator'` → relabel before `enforce` (Rollout Phase E below).
- `sync_from_file` has no `caller` param to gate on (blast radius bounded to `ingested` writes); no registration-time enforcement that future write tools include `require_caller`; `health` re-reads `principals.json` per call.

**Remaining work = operator rollout, not engineering.** See "Rollout Runbook" below. The per-task `- [ ]` checkboxes are preserved as the historical plan record; **this STATUS block is the source of truth for current state.**

---

**Goal:** Replace bridge-db's self-asserted `caller` parameter with channel-derived principal identity, block source-trust self-promotion at the API, and stop `sync_from_file` from laundering disk edits under existing trust labels.

**Architecture:** Each MCP client process carries a per-principal bearer token in its spawn env (`BRIDGE_DB_PRINCIPAL_TOKEN`); the server resolves it against an operator-managed enrollment file at startup and binds the whole stdio connection to one principal. A `require_caller` check at the top of every caller-bearing write tool cross-checks the claimed `caller` against the bound principal, governed by a three-mode rollout dial (`off` → `warn` → `enforce`). Independently of identity: no MCP write may mint `source_trust='operator'` (clamped to `'agent'` + audited), and `sync_from_file` imports changed file content as `'ingested'`. Operator-only label promotion moves to a TTY-gated CLI.

**Tech Stack:** Python 3.12, FastMCP (stdio), aiosqlite, pytest (asyncio_mode=auto), pyright strict, ruff. **No DB schema change** — enrollment lives in `principals.json`, mode in env.

**Design decisions (locked):**
- Tokens are hashed (SHA-256) at rest in `~/.local/share/bridge-db/principals.json` (mode 0600); plaintext exists only in each client's spawn env. On a single-user Mac this does not stop a determined local process from reading another client's config — it makes impersonation an explicit, auditable act instead of a one-string-parameter accident, and gives the harness hooks a concrete sensitive path to guard.
- `BRIDGE_DB_AUTH_MODE`: `off` (default; byte-for-byte current behavior — this is the rollback lever), `warn` (allow + audit every mismatch), `enforce` (reject mismatches and unbound writes). Any unrecognized value resolves to `enforce` (fail closed).
- Trust clamp and sync demotion activate when mode is `warn` or `enforce` — label correctness shouldn't wait for identity enforcement, but everything reverts with the single `off` lever.
- Clamp, don't reject, `source_trust='operator'` requests: rejection would hard-break Claude.ai's existing dispatch prompts mid-migration; clamping stores the correct label, flags `source_trust_clamped: true` in the response, and audits. The pickup gate (`pick_up_handoff` confirm) already enforces the consequence.
- Read tools stay open (T0 in the governance design). `mark_shipped_processed` (legacy, no `caller` param) is untouched — it's already audit-flagged via F7 and slated for retirement, not retrofit.

**Tools receiving `require_caller` (8):** `log_activity`, `create_handoff`, `pick_up_handoff`, `clear_handoff`, `confirm_shipped_sync`, `save_snapshot`, `update_section`, `record_cost`.
**Tools receiving the trust clamp (4):** `update_section`, `create_handoff`, `log_activity`, `save_snapshot`.

---

## File Structure

- Create: `src/bridge_db/auth.py` — principal store loading, token resolution, mode resolution, `require_caller`, `clamp_source_trust`. One responsibility: every identity/label-authority decision lives here.
- Create: `tests/test_auth.py` — unit tests for the above.
- Modify: `src/bridge_db/config.py` — `PRINCIPALS_PATH`, `AUTH_MODE`.
- Modify: `src/bridge_db/server.py` — `AppContext.principal`, bind-at-startup in `app_lifespan`.
- Modify: `tests/conftest.py` — `make_ctx(conn, principal=None)`.
- Modify: `src/bridge_db/tools/{activity,handoffs,context,snapshots,cost}.py` — one `require_caller` line per tool; clamp in the four minting tools; sync demotion in `context.py`.
- Modify: `src/bridge_db/tools/health.py` — auth block in health metrics.
- Modify: `src/bridge_db/__main__.py` — `--enroll`, `--list-principals`, `--revoke-principal`, `--promote-section`.
- Modify: `tests/test_{activity,handoffs,context,snapshots,cost,health,cli}.py` — integration coverage.
- Modify: `CLAUDE.md`, `integration-spec.md`, `OPERATOR-CHECKLIST.md` — conventions, registration, rollout.

Branch: all work on `feat/stage1-authenticated-channel` (created in Task 1, Step 1).

---

### Task 1: `auth.py` core — principal store and token resolution

**Files:**
- Create: `src/bridge_db/auth.py`
- Modify: `src/bridge_db/config.py` (append after `META_SHIPPED_EVENTS_PATH`)
- Test: `tests/test_auth.py`

- [ ] **Step 1: Create the branch**

```bash
cd ~/Projects/bridge-db && git checkout -b feat/stage1-authenticated-channel
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_auth.py`:

```python
"""Tests for channel-derived principal identity (auth.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bridge_db import auth, config


def write_principals(path: Path, entries: dict[str, str]) -> None:
    """Write a principals file mapping caller id -> raw token (hashed on write)."""
    payload = {
        "version": 1,
        "principals": {
            caller: {"token_sha256": auth.hash_token(token), "enrolled_at": "2026-06-12T00:00:00Z"}
            for caller, token in entries.items()
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_hash_token_is_sha256_hex() -> None:
    assert auth.hash_token("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_load_principals_missing_file_returns_empty(tmp_path: Path) -> None:
    assert auth.load_principals(tmp_path / "nope.json") == {}


def test_load_principals_malformed_file_returns_empty(tmp_path: Path) -> None:
    bad = tmp_path / "principals.json"
    bad.write_text("{not json", encoding="utf-8")
    assert auth.load_principals(bad) == {}


def test_load_principals_maps_hash_to_caller(tmp_path: Path) -> None:
    path = tmp_path / "principals.json"
    write_principals(path, {"cc": "token-cc", "codex": "token-codex"})
    loaded = auth.load_principals(path)
    assert loaded[auth.hash_token("token-cc")] == "cc"
    assert loaded[auth.hash_token("token-codex")] == "codex"
    assert len(loaded) == 2


def test_resolve_principal_known_unknown_and_none(tmp_path: Path) -> None:
    path = tmp_path / "principals.json"
    write_principals(path, {"cc": "token-cc"})
    principals = auth.load_principals(path)
    assert auth.resolve_principal("token-cc", principals) == "cc"
    assert auth.resolve_principal("wrong", principals) is None
    assert auth.resolve_principal(None, principals) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("off", "off"), ("warn", "warn"), ("enforce", "enforce"),
     ("WARN", "warn"), ("bogus", "enforce"), ("", "enforce")],
)
def test_auth_mode_normalizes_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setattr(config, "AUTH_MODE", raw)
    assert auth.auth_mode() == expected
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bridge_db.auth'` (or `AttributeError` on `config.AUTH_MODE`).

- [ ] **Step 4: Add config constants**

Append to `src/bridge_db/config.py`:

```python
# Principal enrollment store: maps sha256(token) -> caller id. Operator-managed
# via `python -m bridge_db --enroll <caller>`; mode 0600. Override for tests.
PRINCIPALS_PATH: Path = Path(
    os.environ.get(
        "BRIDGE_DB_PRINCIPALS_PATH",
        str(DB_PATH.parent / "principals.json"),
    )
)

# Auth rollout dial: 'off' (legacy, no checks), 'warn' (allow + audit mismatches),
# 'enforce' (reject mismatches and unbound writes). Unrecognized values are
# treated as 'enforce' by auth.auth_mode() — fail closed.
AUTH_MODE: str = os.environ.get("BRIDGE_DB_AUTH_MODE", "off")
```

- [ ] **Step 5: Write `src/bridge_db/auth.py` (store + resolution + mode)**

```python
"""Channel-derived principal identity: enrollment store, binding, caller checks.

Every identity and label-authority decision lives here. The server binds one
principal per stdio connection at startup (from BRIDGE_DB_PRINCIPAL_TOKEN);
write tools call require_caller() to cross-check the claimed caller against
that binding, and minting tools call clamp_source_trust() to block operator
label self-promotion. Behavior is governed by config.AUTH_MODE.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError

from bridge_db import config
from bridge_db.audit import log_audit

logger = logging.getLogger("bridge_db.auth")

_VALID_MODES = frozenset({"off", "warn", "enforce"})


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def auth_mode() -> str:
    """Normalize config.AUTH_MODE; anything unrecognized fails closed to 'enforce'."""
    mode = config.AUTH_MODE.strip().lower()
    return mode if mode in _VALID_MODES else "enforce"


def load_principals(path: Path) -> dict[str, str]:
    """Read the enrollment file into a sha256(token) -> caller map.

    Missing or malformed file -> {} (nothing binds, enforce mode denies writes).
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = raw["principals"]
        return {
            entry["token_sha256"]: caller
            for caller, entry in entries.items()
            if isinstance(entry, dict) and isinstance(entry.get("token_sha256"), str)
        }
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.warning("principals file unreadable: %s", path)
        return {}


def resolve_principal(token: str | None, principals: dict[str, str]) -> str | None:
    if not token:
        return None
    return principals.get(hash_token(token))


def get_principal(ctx: Any) -> str | None:
    """Read the connection-bound principal off the lifespan context. None-safe."""
    try:
        return getattr(ctx.request_context.lifespan_context, "principal", None)
    except AttributeError:
        return None
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_auth.py -v`
Expected: PASS (all 6 test functions / 11 cases).

- [ ] **Step 7: Lint, typecheck, commit**

```bash
uv run ruff check && uv run pyright
git add src/bridge_db/auth.py src/bridge_db/config.py tests/test_auth.py
git commit -m "feat(auth): principal enrollment store, token resolution, mode dial"
```

---

### Task 2: `require_caller` and `clamp_source_trust`

**Files:**
- Modify: `src/bridge_db/auth.py` (append)
- Test: `tests/test_auth.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_auth.py`:

```python
from bridge_db.audit import iter_jsonl
from mcp.server.fastmcp.exceptions import ToolError


class _FakeLifespan:
    def __init__(self, principal: str | None) -> None:
        self.principal = principal


class _FakeRequestContext:
    def __init__(self, principal: str | None) -> None:
        self.lifespan_context = _FakeLifespan(principal)


class _FakeCtx:
    def __init__(self, principal: str | None) -> None:
        self.request_context = _FakeRequestContext(principal)


def audit_events() -> list[dict[str, object]]:
    return list(iter_jsonl(config.AUDIT_LOG_PATH))


def test_require_caller_off_mode_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "AUTH_MODE", "off")
    auth.require_caller(_FakeCtx(None), "cc", tool="log_activity")  # no raise
    assert audit_events() == []


def test_require_caller_match_passes_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "AUTH_MODE", "enforce")
    auth.require_caller(_FakeCtx("cc"), "cc", tool="log_activity")
    assert audit_events() == []


def test_require_caller_warn_mismatch_allows_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "AUTH_MODE", "warn")
    auth.require_caller(_FakeCtx("codex"), "claude_ai", tool="create_handoff")
    events = audit_events()
    assert len(events) == 1
    assert events[0]["tool"] == "auth.mismatch"
    assert events[0]["ok"] is False
    assert "principal=codex" in str(events[0]["detail"])


def test_require_caller_enforce_mismatch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "AUTH_MODE", "enforce")
    with pytest.raises(ToolError, match="bound to 'codex'"):
        auth.require_caller(_FakeCtx("codex"), "cc", tool="log_activity")
    assert audit_events()[0]["tool"] == "auth.mismatch"


def test_require_caller_enforce_unbound_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "AUTH_MODE", "enforce")
    with pytest.raises(ToolError, match="Unauthenticated connection"):
        auth.require_caller(_FakeCtx(None), "cc", tool="log_activity")


def test_clamp_blocks_operator_when_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "AUTH_MODE", "warn")
    stored, clamped = auth.clamp_source_trust("operator", caller="claude_ai", tool="create_handoff")
    assert (stored, clamped) == ("agent", True)
    events = audit_events()
    assert events[0]["tool"] == "auth.trust_clamped"


@pytest.mark.parametrize("requested", ["agent", "ingested", None])
def test_clamp_passes_non_operator_through(
    monkeypatch: pytest.MonkeyPatch, requested: str | None
) -> None:
    monkeypatch.setattr(config, "AUTH_MODE", "enforce")
    assert auth.clamp_source_trust(requested, caller="cc", tool="log_activity") == (
        requested,
        False,
    )


def test_clamp_inactive_in_off_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "AUTH_MODE", "off")
    assert auth.clamp_source_trust("operator", caller="cc", tool="log_activity") == (
        "operator",
        False,
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_auth.py -v`
Expected: FAIL — `AttributeError: module 'bridge_db.auth' has no attribute 'require_caller'`.

- [ ] **Step 3: Implement (append to `src/bridge_db/auth.py`)**

```python
def require_caller(ctx: Any, caller: str, tool: str) -> None:
    """Cross-check the claimed caller against the connection-bound principal.

    off: no-op. warn: allow but audit mismatches. enforce: reject mismatches
    and unbound connections. Match is always silent.
    """
    mode = auth_mode()
    if mode == "off":
        return
    principal = get_principal(ctx)
    if principal == caller:
        return
    detail = f"tool={tool} principal={principal or 'unbound'} caller={caller} mode={mode}"
    log_audit("auth.mismatch", caller, None, ok=False, detail=detail)
    logger.warning("auth mismatch: %s", detail)
    if mode == "warn":
        return
    if principal is None:
        raise ToolError(
            "Unauthenticated connection: no BRIDGE_DB_PRINCIPAL_TOKEN bound. "
            "Enroll with `python -m bridge_db --enroll <caller>` and set the "
            "token in this client's MCP spawn env."
        )
    raise ToolError(f"Caller mismatch: connection bound to '{principal}', cannot act as '{caller}'")


def clamp_source_trust(
    requested: str | None, caller: str, tool: str
) -> tuple[str | None, bool]:
    """Block MCP-side minting of the 'operator' label.

    Returns (stored_value, clamped). Active in warn and enforce modes; 'off'
    preserves legacy behavior so the rollback lever stays total. Operator
    labels are minted only via the TTY-gated CLI (--promote-section) or
    pre-existing rows.
    """
    if auth_mode() == "off" or requested != "operator":
        return requested, False
    log_audit(
        "auth.trust_clamped",
        caller,
        None,
        ok=False,
        detail=f"tool={tool} requested=operator stored=agent",
    )
    logger.warning("source_trust clamp: tool=%s caller=%s operator->agent", tool, caller)
    return "agent", True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_auth.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check && uv run pyright
git add src/bridge_db/auth.py tests/test_auth.py
git commit -m "feat(auth): require_caller cross-check and operator-label clamp"
```

---

### Task 3: Bind principal at server startup; extend test fixture

**Files:**
- Modify: `src/bridge_db/server.py:25-38` (AppContext + app_lifespan)
- Modify: `tests/conftest.py:35-46` (make_ctx)
- Test: `tests/test_auth.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_auth.py`:

```python
async def test_app_lifespan_binds_principal_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_db.server import app_lifespan, mcp as server_mcp

    principals_path = tmp_path / "principals.json"
    write_principals(principals_path, {"cc": "token-cc"})
    monkeypatch.setattr(config, "PRINCIPALS_PATH", principals_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "bind-test.db")
    monkeypatch.setenv("BRIDGE_DB_PRINCIPAL_TOKEN", "token-cc")

    async with app_lifespan(server_mcp) as app_ctx:
        assert app_ctx.principal == "cc"


async def test_app_lifespan_unknown_token_binds_none_and_audits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_db.server import app_lifespan, mcp as server_mcp

    principals_path = tmp_path / "principals.json"
    write_principals(principals_path, {"cc": "token-cc"})
    monkeypatch.setattr(config, "PRINCIPALS_PATH", principals_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "bind-test2.db")
    monkeypatch.setenv("BRIDGE_DB_PRINCIPAL_TOKEN", "stolen-or-stale")

    async with app_lifespan(server_mcp) as app_ctx:
        assert app_ctx.principal is None
    events = audit_events()
    assert events[0]["tool"] == "auth.bind"
    assert events[0]["ok"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_auth.py -k lifespan -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'principal'` or `AttributeError: 'AppContext' object has no attribute 'principal'`.

- [ ] **Step 3: Implement in `src/bridge_db/server.py`**

Replace the `AppContext` dataclass and `app_lifespan` (lines 25–38) with:

```python
@dataclass
class AppContext:
    db: aiosqlite.Connection
    principal: str | None = None


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncGenerator[AppContext, None]:  # noqa: ARG001
    from bridge_db.audit import log_audit
    from bridge_db.auth import auth_mode, load_principals, resolve_principal

    token = os.environ.get("BRIDGE_DB_PRINCIPAL_TOKEN")
    principal = resolve_principal(token, load_principals(config.PRINCIPALS_PATH))
    if token and principal is None:
        log_audit("auth.bind", None, None, ok=False, detail="token present but not enrolled")
    logger.info(
        "bridge-db starting, db=%s principal=%s auth_mode=%s",
        config.DB_PATH,
        principal or "unbound",
        auth_mode(),
    )
    db = await open_db(config.DB_PATH)
    try:
        yield AppContext(db=db, principal=principal)
    finally:
        await db.close()
        logger.info("bridge-db shut down")
```

(`os` is already imported in server.py.)

- [ ] **Step 4: Extend `make_ctx` in `tests/conftest.py`**

Replace the `make_ctx` function (lines 35–46) with:

```python
def make_ctx(conn: aiosqlite.Connection, principal: str | None = None) -> Any:
    """Build a minimal mock Context satisfying ctx.request_context.lifespan_context.

    `principal` mirrors AppContext.principal — the channel-bound identity used
    by auth.require_caller. Defaults to None (unbound), matching legacy tests.
    """
    # Alias before the class body: `principal = principal` inside a class body
    # raises NameError (class bodies cannot close over a name they also bind).
    bound_principal = principal

    class _AppContext:
        db = conn
        principal = bound_principal

    class _RequestContext:
        lifespan_context = _AppContext()

    ctx = MagicMock()
    ctx.request_context = _RequestContext()
    return ctx
```

- [ ] **Step 5: Run the full suite to verify nothing regressed**

Run: `uv run pytest`
Expected: PASS — all 203 existing + new tests (default `AUTH_MODE` in tests is whatever the env provides; `off` unless set — the new checks are inert for legacy tests).

- [ ] **Step 6: Lint, typecheck, commit**

```bash
uv run ruff check && uv run pyright
git add src/bridge_db/server.py tests/conftest.py tests/test_auth.py
git commit -m "feat(auth): bind connection principal in app_lifespan; fixture support"
```

---

### Task 4: Wire `require_caller` into the 8 caller-bearing tools

**Files:**
- Modify: `src/bridge_db/tools/activity.py` (`log_activity`, `confirm_shipped_sync`)
- Modify: `src/bridge_db/tools/handoffs.py` (`create_handoff`, `pick_up_handoff`, `clear_handoff`)
- Modify: `src/bridge_db/tools/context.py` (`update_section`)
- Modify: `src/bridge_db/tools/snapshots.py` (`save_snapshot`)
- Modify: `src/bridge_db/tools/cost.py` (`record_cost`)
- Test: `tests/test_activity.py`, `tests/test_handoffs.py` (append)

- [ ] **Step 1: Write the failing integration tests**

Append to `tests/test_activity.py` (imports at top of the new block):

```python
from bridge_db import config as bridge_config
from tests.conftest import CaptureMCP, make_ctx


async def test_log_activity_enforce_rejects_caller_mismatch(
    db, monkeypatch
) -> None:
    from mcp.server.fastmcp.exceptions import ToolError

    from bridge_db.tools import activity as activity_module

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "enforce")
    cap = CaptureMCP()
    activity_module.register(cap)
    with pytest.raises(ToolError, match="bound to 'codex'"):
        await cap.fns["log_activity"](
            caller="cc",
            project_name="TestProject",
            summary="forged write",
            ctx=make_ctx(db, principal="codex"),
        )


async def test_log_activity_enforce_allows_matching_principal(db, monkeypatch) -> None:
    from bridge_db.tools import activity as activity_module

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "enforce")
    cap = CaptureMCP()
    activity_module.register(cap)
    result = await cap.fns["log_activity"](
        caller="cc",
        project_name="TestProject",
        summary="legit write",
        ctx=make_ctx(db, principal="cc"),
    )
    assert result["ok"] is True


async def test_log_activity_warn_allows_mismatch(db, monkeypatch) -> None:
    from bridge_db.tools import activity as activity_module

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "warn")
    cap = CaptureMCP()
    activity_module.register(cap)
    result = await cap.fns["log_activity"](
        caller="cc",
        project_name="TestProject",
        summary="mismatched but warned",
        ctx=make_ctx(db, principal="codex"),
    )
    assert result["ok"] is True
```

Append to `tests/test_handoffs.py`:

```python
async def test_create_handoff_enforce_rejects_unbound_connection(db, monkeypatch) -> None:
    from mcp.server.fastmcp.exceptions import ToolError

    from bridge_db import config as bridge_config
    from bridge_db.tools import handoffs as handoffs_module
    from tests.conftest import CaptureMCP, make_ctx

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "enforce")
    cap = CaptureMCP()
    handoffs_module.register(cap)
    with pytest.raises(ToolError, match="Unauthenticated connection"):
        await cap.fns["create_handoff"](
            caller="claude_ai",
            project_name="TestProject",
            ctx=make_ctx(db, principal=None),
        )
```

(If these test files import `pytest`/fixtures differently, match their existing header style.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_activity.py tests/test_handoffs.py -k "enforce or warn_allows" -v`
Expected: FAIL — the forged/unbound writes currently succeed (no ToolError raised).

- [ ] **Step 3: Add the check to all 8 tools**

In each of the five tool modules, add the import alongside the existing `bridge_db` imports:

```python
from bridge_db.auth import require_caller
```

Then add one line as the **first statement after the docstring** of each tool, with the tool's own name:

```python
        require_caller(ctx, caller, tool="log_activity")
```

Apply to: `log_activity` and `confirm_shipped_sync` in `activity.py`; `create_handoff`, `pick_up_handoff`, `clear_handoff` in `handoffs.py`; `update_section` in `context.py`; `save_snapshot` in `snapshots.py`; `record_cost` in `cost.py` — each with its own `tool=` string. In `create_handoff`, place it **above** the existing `if caller != "claude_ai"` check (channel identity first, then role rule).

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest`
Expected: PASS — new tests green; legacy tests unaffected (mode defaults to `off`, and `make_ctx` defaults `principal=None` which only matters in warn/enforce).

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check && uv run pyright
git add src/bridge_db/tools tests/test_activity.py tests/test_handoffs.py
git commit -m "feat(auth): cross-check caller against bound principal in all 8 write tools"
```

---

### Task 5: Wire the trust clamp into the 4 minting tools

**Files:**
- Modify: `src/bridge_db/tools/context.py` (`update_section`), `src/bridge_db/tools/handoffs.py` (`create_handoff`), `src/bridge_db/tools/activity.py` (`log_activity`), `src/bridge_db/tools/snapshots.py` (`save_snapshot`)
- Test: `tests/test_context.py`, `tests/test_handoffs.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_context.py`:

```python
async def test_update_section_clamps_operator_self_promotion(db, monkeypatch) -> None:
    from bridge_db import config as bridge_config
    from bridge_db.tools import context as context_module
    from tests.conftest import CaptureMCP, make_ctx

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "warn")
    cap = CaptureMCP()
    context_module.register(cap)
    result = await cap.fns["update_section"](
        caller="claude_ai",
        section_name="career",
        content="# Career\nupdated",
        source_trust="operator",
        ctx=make_ctx(db, principal="claude_ai"),
    )
    assert result["source_trust"] == "agent"
    assert result["source_trust_clamped"] is True

    cursor = await db.execute(
        "SELECT source_trust FROM context_sections WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row["source_trust"] == "agent"
```

Append to `tests/test_handoffs.py`:

```python
async def test_create_handoff_clamps_operator_label(db, monkeypatch) -> None:
    from bridge_db import config as bridge_config
    from bridge_db.tools import handoffs as handoffs_module
    from tests.conftest import CaptureMCP, make_ctx

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "warn")
    cap = CaptureMCP()
    handoffs_module.register(cap)
    result = await cap.fns["create_handoff"](
        caller="claude_ai",
        project_name="TestProject",
        source_trust="operator",
        ctx=make_ctx(db, principal="claude_ai"),
    )
    assert result["source_trust"] == "agent"
    assert result["source_trust_clamped"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_context.py tests/test_handoffs.py -k clamp -v`
Expected: FAIL — stored label is `operator`, no `source_trust_clamped` key.

- [ ] **Step 3: Implement the clamp in each minting tool**

Add `clamp_source_trust` to the existing auth import in the four modules:

```python
from bridge_db.auth import clamp_source_trust, require_caller
```

In each tool, immediately after the `require_caller(...)` line:

```python
        source_trust, source_trust_clamped = clamp_source_trust(
            source_trust, caller=caller, tool="update_section"
        )
```

(with the tool's own name), and add `"source_trust_clamped": source_trust_clamped` to the tool's return dict. In `update_section`, the existing echo-the-stored-label logic stays — only the input is clamped. In `log_activity` and `save_snapshot`, `source_trust` has default `"agent"`; the clamp is a no-op there unless someone passes `"operator"`, which is exactly the case to catch.

Note on pyright strict: `clamp_source_trust` returns `str | None`; where the tool's local variable is typed `SourceTrust` / `SourceTrust | None`, narrow with `cast(SourceTrust, ...)` is NOT needed if you annotate the auth function precisely instead. Preferred fix — give `clamp_source_trust` an overload-free precise signature in `auth.py`:

```python
from bridge_db.models import SourceTrust  # add at top of auth.py


def clamp_source_trust(
    requested: SourceTrust | None, caller: str, tool: str
) -> tuple[SourceTrust | None, bool]:
```

(The body is unchanged; `"agent"` is a valid `SourceTrust` literal. Update the Task 2 unit tests' `requested` parametrize type hint to `SourceTrust | None` if pyright complains.)

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest`
Expected: PASS.

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check && uv run pyright
git add src/bridge_db/auth.py src/bridge_db/tools tests/test_context.py tests/test_handoffs.py
git commit -m "feat(auth): clamp MCP-side operator label minting to agent"
```

---

### Task 6: `sync_from_file` — change detection and ingest demotion

**Files:**
- Modify: `src/bridge_db/tools/context.py:72-99` (`sync_owned_sections_from_file`)
- Test: `tests/test_context.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_context.py`. Section headings must match `_SECTION_HEADING_MAP` in `context.py` — the parser keys on the full heading `## Career & Professional Target`, not `## Career` (see the existing bridge-file fixture at `tests/test_context.py:225` and reuse its pattern if a shared helper exists):

```python
def _write_bridge_file(tmp_path, career_body: str):
    """Minimal bridge markdown containing one owned section."""
    path = tmp_path / "bridge.md"
    path.write_text(
        f"## Career & Professional Target\n\n{career_body}\n", encoding="utf-8"
    )
    return path


async def test_sync_demotes_changed_section_to_ingested(db, tmp_path, monkeypatch) -> None:
    from bridge_db import config as bridge_config
    from bridge_db.tools.context import _upsert_section, sync_owned_sections_from_file

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "warn")
    await _upsert_section(
        db=db, section_name="career", owner="claude_ai",
        content="original content", source_trust="operator",
    )
    await db.commit()

    path = _write_bridge_file(tmp_path, "edited on disk by who-knows-what")
    result = await sync_owned_sections_from_file(db=db, bridge_path=path)

    assert "career" in result["demoted"]
    cursor = await db.execute(
        "SELECT source_trust FROM context_sections WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row["source_trust"] == "ingested"


async def test_sync_preserves_label_when_content_unchanged(db, tmp_path, monkeypatch) -> None:
    from bridge_db import config as bridge_config
    from bridge_db.tools.context import (
        _upsert_section,
        parse_owned_sections,
        sync_owned_sections_from_file,
    )

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "warn")
    path = _write_bridge_file(tmp_path, "stable content")
    # Seed the DB with exactly what the file parser will produce, labeled operator.
    parsed = parse_owned_sections(path.read_text(encoding="utf-8"))
    await _upsert_section(
        db=db, section_name="career", owner="claude_ai",
        content=parsed["career"], source_trust="operator",
    )
    await db.commit()

    result = await sync_owned_sections_from_file(db=db, bridge_path=path)

    assert "career" in result["unchanged"]
    assert result["demoted"] == []
    cursor = await db.execute(
        "SELECT source_trust FROM context_sections WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row["source_trust"] == "operator"


async def test_sync_off_mode_keeps_legacy_label_preservation(db, tmp_path, monkeypatch) -> None:
    from bridge_db import config as bridge_config
    from bridge_db.tools.context import _upsert_section, sync_owned_sections_from_file

    monkeypatch.setattr(bridge_config, "AUTH_MODE", "off")
    await _upsert_section(
        db=db, section_name="career", owner="claude_ai",
        content="original", source_trust="operator",
    )
    await db.commit()

    path = _write_bridge_file(tmp_path, "changed content")
    result = await sync_owned_sections_from_file(db=db, bridge_path=path)

    assert result["demoted"] == []  # legacy path: no demotion bookkeeping
    cursor = await db.execute(
        "SELECT source_trust FROM context_sections WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row["source_trust"] == "operator"  # documented legacy laundering, off-mode only
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_context.py -k sync -v`
Expected: FAIL — `KeyError: 'demoted'` (return dict lacks the new keys).

- [ ] **Step 3: Replace `sync_owned_sections_from_file`**

Replace the function body (`src/bridge_db/tools/context.py:72-99`) with:

```python
async def sync_owned_sections_from_file(db: Any, bridge_path: Path) -> dict[str, Any]:
    """Read the bridge file and upsert the Claude.ai-owned context sections.

    With auth active (mode != 'off'), the file is an unauthenticated channel:
    unchanged sections are skipped (label preserved), changed or new sections
    are imported as source_trust='ingested' and reported in `demoted` so the
    operator can review and promote via `--promote-section`. In 'off' mode the
    legacy preserve-label upsert runs unchanged (rollback lever).
    """
    if not bridge_path.exists():
        raise ToolError(f"Bridge file not found: {bridge_path}")

    auth_active = auth_mode() != "off"
    parsed_sections = parse_owned_sections(bridge_path.read_text(encoding="utf-8"))
    synced_sections: list[str] = []
    unchanged: list[str] = []
    demoted: list[str] = []

    for section_name in SECTION_OWNERS:
        if section_name not in parsed_sections:
            continue
        content = parsed_sections[section_name]

        if auth_active:
            cursor = await db.execute(
                "SELECT content FROM context_sections WHERE section_name = ?",
                (section_name,),
            )
            row = await cursor.fetchone()
            if row is not None and row["content"] == content:
                unchanged.append(section_name)
                continue
            await _upsert_section(
                db=db,
                section_name=section_name,
                owner="claude_ai",
                content=content,
                source_trust="ingested",
            )
            demoted.append(section_name)
        else:
            await _upsert_section(
                db=db, section_name=section_name, owner="claude_ai", content=content
            )

        synced_sections.append(section_name)

    await db.commit()
    if demoted:
        log_audit(
            "sync_from_file.demoted",
            None,
            None,
            ok=True,
            detail=f"sections={','.join(demoted)} label=ingested (file channel)",
        )
    logger.info(
        "synced %d claude_ai section(s) from %s (unchanged=%d, demoted=%d)",
        len(synced_sections), bridge_path, len(unchanged), len(demoted),
    )
    return {
        "ok": True,
        "path": str(bridge_path),
        "sections_synced": synced_sections,
        "unchanged": unchanged,
        "demoted": demoted,
        "count": len(synced_sections),
    }
```

Add to `context.py` imports: `from bridge_db.auth import auth_mode, clamp_source_trust, require_caller` (merging with the Task 4/5 import line) and `from bridge_db.audit import log_audit` if not already imported in this module.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest`
Expected: PASS — including pre-existing sync tests (they run in `off` mode and hit the legacy branch).

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check && uv run pyright
git add src/bridge_db/tools/context.py tests/test_context.py
git commit -m "feat(auth): import changed bridge-file sections as ingested; skip unchanged"
```

---

### Task 7: Enrollment + promotion CLI

**Files:**
- Modify: `src/bridge_db/__main__.py` (new run functions + argparse wiring in `main()`)
- Test: `tests/test_cli.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py` (match the file's existing import style):

```python
import json

from bridge_db import auth, config
from bridge_db.__main__ import run_enroll, run_list_principals, run_promote_section, run_revoke_principal


def test_enroll_writes_hashed_token_with_0600(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(config, "PRINCIPALS_PATH", tmp_path / "principals.json")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    assert run_enroll("cc") is True
    out = capsys.readouterr().out
    # The raw token is printed exactly once, on its own line after the marker.
    token_line = [line for line in out.splitlines() if line.startswith("  token: ")]
    assert len(token_line) == 1
    token = token_line[0].removeprefix("  token: ").strip()

    data = json.loads((tmp_path / "principals.json").read_text(encoding="utf-8"))
    assert data["principals"]["cc"]["token_sha256"] == auth.hash_token(token)
    assert (tmp_path / "principals.json").stat().st_mode & 0o777 == 0o600


def test_enroll_refuses_without_tty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "PRINCIPALS_PATH", tmp_path / "principals.json")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert run_enroll("cc") is False
    assert not (tmp_path / "principals.json").exists()


def test_enroll_rejects_unknown_caller(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "PRINCIPALS_PATH", tmp_path / "principals.json")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert run_enroll("mallory") is False


def test_revoke_removes_entry(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "PRINCIPALS_PATH", tmp_path / "principals.json")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    run_enroll("cc")
    assert run_revoke_principal("cc") is True
    data = json.loads((tmp_path / "principals.json").read_text(encoding="utf-8"))
    assert "cc" not in data["principals"]


def test_list_principals_shows_enrolled(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(config, "PRINCIPALS_PATH", tmp_path / "principals.json")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    run_enroll("cc")
    capsys.readouterr()  # discard enroll output
    assert run_list_principals() is True
    out = capsys.readouterr().out
    assert "cc" in out


async def test_promote_section_sets_operator_label(db, tmp_path, monkeypatch, capsys) -> None:
    # run_promote_section opens its own DB; point config at a temp DB seeded here.
    from bridge_db.tools.context import _upsert_section

    await _upsert_section(
        db=db, section_name="career", owner="claude_ai",
        content="reviewed content", source_trust="ingested",
    )
    await db.commit()
    # run_promote_section opens its own connection to the same file the `db`
    # fixture created (tmp_path / "test.db"); WAL mode permits both.
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    assert await run_promote_section("career") is True
    cursor = await db.execute(
        "SELECT source_trust FROM context_sections WHERE section_name = 'career'"
    )
    row = await cursor.fetchone()
    assert row["source_trust"] == "operator"
```

(Note: the promote test relies on the `db` fixture writing to `tmp_path / "test.db"` — see `tests/conftest.py:30`. WAL mode permits a second connection from the CLI function.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -k "enroll or revoke or list_principals or promote" -v`
Expected: FAIL — `ImportError: cannot import name 'run_enroll'`.

- [ ] **Step 3: Implement the CLI functions in `src/bridge_db/__main__.py`**

Add after `run_log_session_boundary` (module already imports `sys`, `asyncio`, `datetime`; add `import json`, `import os`, `import secrets` at the top with the existing imports):

```python
def _require_tty(action: str) -> bool:
    """Operator ceremonies require an interactive terminal. Agents run non-TTY."""
    if sys.stdin.isatty():
        return True
    print(f"refused: --{action} is an operator ceremony and requires an interactive TTY")
    return False


def _read_principals_file() -> dict[str, Any]:
    from bridge_db import config

    if config.PRINCIPALS_PATH.exists():
        try:
            data = json.loads(config.PRINCIPALS_PATH.read_text(encoding="utf-8"))
            if isinstance(data.get("principals"), dict):
                return data
        except json.JSONDecodeError:
            print(f"warning: malformed principals file at {config.PRINCIPALS_PATH}, rewriting")
    return {"version": 1, "principals": {}}


def _write_principals_file(data: dict[str, Any]) -> None:
    from bridge_db import config

    config.PRINCIPALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.PRINCIPALS_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(config.PRINCIPALS_PATH, 0o600)


def run_enroll(caller: str) -> bool:
    """Generate a token for one caller, store its hash, print the token once."""
    from bridge_db.audit import log_audit
    from bridge_db.auth import hash_token
    from bridge_db.models import CALLER_IDS

    if caller not in CALLER_IDS:
        print(f"refused: unknown caller '{caller}'. Known: {', '.join(CALLER_IDS)}")
        return False
    if not _require_tty("enroll"):
        return False

    token = secrets.token_urlsafe(32)
    data = _read_principals_file()
    rotated = caller in data["principals"]
    data["principals"][caller] = {
        "token_sha256": hash_token(token),
        "enrolled_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_principals_file(data)
    log_audit("auth.enroll", caller, None, ok=True, detail=f"rotated={rotated}")

    print(f"bridge-db enrollment — principal '{caller}' {'rotated' if rotated else 'enrolled'}")
    print("  Set this token in the client's MCP spawn env (shown once, not stored):")
    print(f"  token: {token}")
    print("  env:   BRIDGE_DB_PRINCIPAL_TOKEN")
    return True


def run_revoke_principal(caller: str) -> bool:
    from bridge_db.audit import log_audit

    if not _require_tty("revoke-principal"):
        return False
    data = _read_principals_file()
    if caller not in data["principals"]:
        print(f"no enrollment found for '{caller}'")
        return False
    del data["principals"][caller]
    _write_principals_file(data)
    log_audit("auth.revoke", caller, None, ok=True, detail=None)
    print(f"revoked '{caller}' — its connections bind as unbound on next spawn")
    return True


def run_list_principals() -> bool:
    data = _read_principals_file()
    if not data["principals"]:
        print("no principals enrolled")
        return True
    print("enrolled principals")
    for caller, entry in sorted(data["principals"].items()):
        print(
            f"  {caller}: enrolled_at={entry.get('enrolled_at', '?')}, "
            f"hash={str(entry.get('token_sha256', ''))[:8]}…"
        )
    return True


async def run_promote_section(section_name: str) -> bool:
    """Operator-only label promotion for a context section (TTY-gated)."""
    from bridge_db import config
    from bridge_db.audit import log_audit
    from bridge_db.db import open_db
    from bridge_db.models import SECTION_OWNERS

    if section_name not in SECTION_OWNERS:
        print(f"refused: unknown section '{section_name}'. Known: {sorted(SECTION_OWNERS)}")
        return False
    if not _require_tty("promote-section"):
        return False

    db = await open_db(config.DB_PATH)
    try:
        cursor = await db.execute(
            "SELECT source_trust, updated_at FROM context_sections WHERE section_name = ?",
            (section_name,),
        )
        row = await cursor.fetchone()
        if row is None:
            print(f"no stored section '{section_name}'")
            return False
        await db.execute(
            "UPDATE context_sections SET source_trust = 'operator' WHERE section_name = ?",
            (section_name,),
        )
        await db.commit()
    finally:
        await db.close()

    log_audit(
        "auth.promote_section",
        "operator-cli",
        None,
        ok=True,
        detail=f"section={section_name} {row['source_trust']}->operator",
    )
    print(
        f"promoted '{section_name}': {row['source_trust']} -> operator "
        f"(content as of {row['updated_at']})"
    )
    return True
```

Wire into `main()` — add the arguments after `--duration-minutes`:

```python
    parser.add_argument(
        "--enroll",
        metavar="CALLER",
        help="Enroll a principal: generate a token, store its hash (operator TTY only)",
    )
    parser.add_argument(
        "--revoke-principal",
        metavar="CALLER",
        help="Remove a principal's enrollment (operator TTY only)",
    )
    parser.add_argument(
        "--list-principals", action="store_true", help="List enrolled principals"
    )
    parser.add_argument(
        "--promote-section",
        metavar="SECTION",
        help="Set a context section's source_trust to 'operator' (operator TTY only)",
    )
```

and the dispatch before the `mcp.run()` fallthrough:

```python
    if args.enroll:
        sys.exit(0 if run_enroll(args.enroll) else 1)
    if args.revoke_principal:
        sys.exit(0 if run_revoke_principal(args.revoke_principal) else 1)
    if args.list_principals:
        sys.exit(0 if run_list_principals() else 1)
    if args.promote_section:
        sys.exit(0 if asyncio.run(run_promote_section(args.promote_section)) else 1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check && uv run pyright
git add src/bridge_db/__main__.py tests/test_cli.py
git commit -m "feat(auth): enrollment, revocation, and section-promotion CLI ceremonies"
```

---

### Task 8: Auth visibility in `health`

**Files:**
- Modify: `src/bridge_db/tools/health.py` (`collect_health_metrics`)
- Test: `tests/test_health.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_health.py`:

```python
async def test_health_reports_auth_block(db, tmp_path, monkeypatch) -> None:
    import json as _json

    from bridge_db import config as bridge_config
    from bridge_db.tools.health import collect_health_metrics

    principals_path = tmp_path / "principals.json"
    principals_path.write_text(
        _json.dumps({"version": 1, "principals": {"cc": {"token_sha256": "x" * 64}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(bridge_config, "PRINCIPALS_PATH", principals_path)
    monkeypatch.setattr(bridge_config, "AUTH_MODE", "warn")

    metrics = await collect_health_metrics(db)
    assert metrics["auth"] == {
        "mode": "warn",
        "principals_file_exists": True,
        "principals_enrolled": 1,
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_health.py -k auth -v`
Expected: FAIL — `KeyError: 'auth'`.

- [ ] **Step 3: Implement**

In `src/bridge_db/tools/health.py`, add to the imports:

```python
from bridge_db.auth import auth_mode, load_principals
```

and inside `collect_health_metrics`, add to the returned dict (alongside the existing top-level keys):

```python
        "auth": {
            "mode": auth_mode(),
            "principals_file_exists": config.PRINCIPALS_PATH.exists(),
            "principals_enrolled": len(load_principals(config.PRINCIPALS_PATH)),
        },
```

(If `collect_health_metrics` builds the dict incrementally, add the key the same way its siblings are added. `config` is already imported in health.py.)

- [ ] **Step 4: Run the full suite, lint, typecheck, commit**

```bash
uv run pytest && uv run ruff check && uv run pyright
git add src/bridge_db/tools/health.py tests/test_health.py
git commit -m "feat(auth): surface auth mode and enrollment count in health"
```

---

### Task 9: Documentation

**Files:**
- Modify: `CLAUDE.md` (Conventions, Commands, Registration sections; also fix the stale "Schema at v5" note — live DB is v7)
- Modify: `integration-spec.md` (Claude.ai paths: direct MCP now requires a token; file fallback imports as `ingested`)
- Modify: `OPERATOR-CHECKLIST.md` (enrollment + env wiring + mode rollout)

- [ ] **Step 1: CLAUDE.md — add to Conventions**

```markdown
- **Channel auth (Stage 1)**: each client's MCP spawn env carries `BRIDGE_DB_PRINCIPAL_TOKEN`;
  the server binds the connection to one principal at startup (`principals.json`, managed via
  `--enroll`). `BRIDGE_DB_AUTH_MODE` = `off` (legacy) | `warn` (allow + audit mismatches) |
  `enforce` (reject); unrecognized values fail closed to `enforce`. With auth active:
  no MCP write may mint `source_trust='operator'` (clamped to `agent`, audited), and
  `sync_from_file` imports changed file content as `ingested` (promote via
  `--promote-section`, TTY-only).
```

Add to the Commands block:

```bash
uv run python -m bridge_db --enroll cc            # enroll/rotate a principal (TTY only)
uv run python -m bridge_db --list-principals      # show enrolled principals
uv run python -m bridge_db --revoke-principal cc  # revoke a principal (TTY only)
uv run python -m bridge_db --promote-section career  # operator label promotion (TTY only)
```

Update the Registration section:

```bash
claude mcp add --scope user bridge-db \
  --env BRIDGE_DB_PRINCIPAL_TOKEN=<cc-token> \
  --env BRIDGE_DB_AUTH_MODE=warn \
  -- uv run --directory ~/Projects/bridge-db python -m bridge_db
```

- [ ] **Step 2: integration-spec.md — update the two Claude.ai paths**

Under "Primary path" add: direct MCP through Claude Desktop requires `BRIDGE_DB_PRINCIPAL_TOKEN` (the `claude_ai` enrollment) in the Desktop MCP config's `env` block once `BRIDGE_DB_AUTH_MODE` leaves `off`. Under "Fallback path" add: file edits import as `source_trust='ingested'` when auth is active; the operator reviews and promotes via `--promote-section`. The file is an unauthenticated channel by design.

- [ ] **Step 3: OPERATOR-CHECKLIST.md — append the Stage 1 rollout section**

Copy the full "Rollout Runbook" section below (it is the operator-facing artifact).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md integration-spec.md OPERATOR-CHECKLIST.md
git commit -m "docs(auth): stage-1 channel auth conventions, registration, rollout runbook"
```

---

### Task 10: Final verification and review gate

- [ ] **Step 1: Full local verify**

```bash
uv run pytest && uv run pyright && uv run ruff check
uv run python -m bridge_db --doctor
```

Expected: all tests pass (203 pre-existing + ~25 new), pyright strict clean, ruff clean, doctor green.

- [ ] **Step 2: Manual smoke (operator TTY)**

```bash
BRIDGE_DB_PRINCIPALS_PATH=/tmp/smoke-principals.json uv run python -m bridge_db --enroll cc
BRIDGE_DB_PRINCIPALS_PATH=/tmp/smoke-principals.json uv run python -m bridge_db --list-principals
echo | BRIDGE_DB_PRINCIPALS_PATH=/tmp/smoke-principals.json uv run python -m bridge_db --enroll codex  # piped stdin → must refuse (non-TTY)
rm /tmp/smoke-principals.json
```

- [ ] **Step 3: `/code-review` on the branch diff** (per demand-elegance gate: new module + auth logic — squarely in scope). Address critical findings before merge.

- [ ] **Step 4: Merge gate** — squash to one commit on the feature branch is NOT needed (commits are already logical units); merge per repo convention after review.

---

## Rollout Runbook (operator steps, after merge — also lands in OPERATOR-CHECKLIST.md)

**Phase A — Enroll (one TTY session):**
1. `uv run python -m bridge_db --enroll cc` / `--enroll codex` / `--enroll claude_ai` / `--enroll notion_os` / `--enroll personal_ops` — capture each token once.

**Phase B — Wire envs (each client spawns its own server process):**
2. **CC:** `claude mcp remove bridge-db -s user`, then re-add with `--env BRIDGE_DB_PRINCIPAL_TOKEN=<cc> --env BRIDGE_DB_AUTH_MODE=warn` (full command in CLAUDE.md Registration).
3. **Claude Desktop (claude_ai):** add `"env": {"BRIDGE_DB_PRINCIPAL_TOKEN": "<claude_ai>", "BRIDGE_DB_AUTH_MODE": "warn"}` to the bridge-db entry in `claude_desktop_config.json`.
4. **Codex:** add the same two vars to the bridge-db server's `env` table in `~/.codex/config.toml`.
5. **personal-ops:** set both vars in the spawn env in `~/.local/share/personal-ops/app/src/bridge-db.ts` (it spawns bridge-db as an MCP subprocess).
6. **notion-os:** locate its bridge-db spawn (`rg -n "bridge_db|bridge-db" ~/Projects/Notion/src`) and set both vars there.
7. Add `~/.local/share/bridge-db/principals.json` to the harness sensitive-path guard list (`mcp-gate-policy.json`) — both CC and Codex hooks read it live.

**Phase C — Warn burn-in (≥1 week):**
8. Watch `audit_tail(tool="auth.mismatch")` and `audit_tail(tool="auth.trust_clamped")` every few days. Expected findings: any consumer skill or prompt still passing a wrong `caller` or `source_trust='operator'`. Fix consumers (grep `~/.claude/skills` and Claude.ai project prompts for `create_handoff`/`update_section` call sites — known lesson: MCP param changes don't auto-propagate to skills).
9. Watch for `auth.bind` failures (token present but not enrolled = a client wired with a stale/wrong token).

**Phase D — Enforce:**
10. Flip `BRIDGE_DB_AUTH_MODE=enforce` in all five client configs. Verify each client can still write (one `log_activity` per system) and that a deliberately wrong-caller call is rejected.

**Phase E — Legacy label cleanup (one-time):**
11. The 20 pre-existing `operator`-labeled pending handoffs were labeled before minting was gated. Review and relabel the unconsumed ones so the pickup gate actually gates:
    `sqlite3 ~/.local/share/bridge-db/bridge.db "UPDATE pending_handoffs SET source_trust='agent' WHERE status='pending' AND source_trust='operator';"`
    (Run manually after eyeballing `get_pending_handoffs` — any handoff you genuinely dictated can stay `operator`.)
12. Re-run `uv run python -m bridge_db --status` and confirm the pending-handoff trust breakdown reflects the relabel.

**Rollback at any point:** set `BRIDGE_DB_AUTH_MODE=off` in the affected client(s) — restores byte-for-byte legacy behavior including sync label preservation. No DB migration to unwind.

---

## Out of scope (later stages of the governance design)

- Provenance chains / `parent_refs` and floor propagation (Stage 2)
- `mesh-policy.json` PDP and action tiers (Stage 3)
- Approval-queue-routed promotions replacing `--promote-section` (Stage 4)
- Hash-chained audit, quarantine-on-revoke (Stage 5)
- `mark_shipped_processed` retirement (tracked separately; F7 guard already flags misuse)
