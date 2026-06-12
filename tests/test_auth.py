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
