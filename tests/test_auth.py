"""Tests for channel-derived principal identity (auth.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from bridge_db import auth, config
from bridge_db.audit import iter_jsonl
from bridge_db.models import SourceTrust


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
    [
        ("off", "off"),
        ("warn", "warn"),
        ("enforce", "enforce"),
        ("WARN", "warn"),
        ("bogus", "enforce"),
        ("", "enforce"),
    ],
)
def test_auth_mode_normalizes_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setattr(config, "AUTH_MODE", raw)
    assert auth.auth_mode() == expected


def test_get_principal_reads_lifespan_context() -> None:
    class _Lifespan:
        principal = "cc"

    class _RequestContext:
        lifespan_context = _Lifespan()

    class _Ctx:
        request_context = _RequestContext()

    assert auth.get_principal(_Ctx()) == "cc"


def test_get_principal_malformed_ctx_returns_none() -> None:
    assert auth.get_principal(object()) is None


def test_load_principals_skips_malformed_entries(tmp_path: Path) -> None:
    path = tmp_path / "principals.json"
    payload = {
        "version": 1,
        "principals": {
            "bad-caller": "not-a-dict",
            "no-hash": {"enrolled_at": "2026-06-12T00:00:00Z"},
            "cc": {"token_sha256": auth.hash_token("token-cc"), "enrolled_at": "x"},
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = auth.load_principals(path)
    assert loaded == {auth.hash_token("token-cc"): "cc"}


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
    assert audit_events()[0]["tool"] == "auth.mismatch"


@pytest.mark.parametrize("mode", ["warn", "enforce"])
def test_clamp_blocks_operator_in_active_modes(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr(config, "AUTH_MODE", mode)
    stored, clamped = auth.clamp_source_trust("operator", caller="claude_ai", tool="create_handoff")
    assert (stored, clamped) == ("agent", True)
    events = audit_events()
    assert events[0]["tool"] == "auth.trust_clamped"


@pytest.mark.parametrize("requested", ["agent", "ingested", None])
def test_clamp_passes_non_operator_through(
    monkeypatch: pytest.MonkeyPatch, requested: SourceTrust | None
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


async def test_app_lifespan_binds_principal_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_db.server import app_lifespan
    from bridge_db.server import mcp as server_mcp

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
    from bridge_db.server import app_lifespan
    from bridge_db.server import mcp as server_mcp

    principals_path = tmp_path / "principals.json"
    write_principals(principals_path, {"cc": "token-cc"})
    monkeypatch.setattr(config, "PRINCIPALS_PATH", principals_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "bind-test2.db")
    monkeypatch.setenv("BRIDGE_DB_PRINCIPAL_TOKEN", "stolen-or-stale")

    async with app_lifespan(server_mcp) as app_ctx:
        assert app_ctx.principal is None
    bind_events = [e for e in audit_events() if e["tool"] == "auth.bind"]
    assert len(bind_events) == 1
    assert bind_events[0]["ok"] is False


async def test_app_lifespan_no_token_binds_none_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_db.server import app_lifespan
    from bridge_db.server import mcp as server_mcp

    monkeypatch.setattr(config, "PRINCIPALS_PATH", tmp_path / "principals.json")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "bind-test3.db")
    monkeypatch.delenv("BRIDGE_DB_PRINCIPAL_TOKEN", raising=False)

    async with app_lifespan(server_mcp) as app_ctx:
        assert app_ctx.principal is None
    assert audit_events() == []
