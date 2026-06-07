"""Resolve free-form project names to canonical keys.

Read-only consumer of GithubRepoAuditor's ``project-registry.json``. bridge-db
never hard-depends on it: if the registry file is absent or unreadable, every
resolution returns ``registry_present=False`` and a ``None`` canonical key, so
``log_activity`` behaves exactly as before. When the registry IS present and a
name does not match, that is surfaced (``matched=False``) so drift can be
flagged via the existing audit log rather than silently recorded.

This is a consumer-side resolution helper, not a new coordination surface:
bridge-db stores the resolved key alongside the activity it already stores.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from bridge_db import config

_NON_ALNUM = re.compile(r"[^a-z0-9]")

# (path, mtime_ns) -> compiled index, so a long-running server picks up new
# auditor runs without restarting and without re-reading on every call.
_cache: dict[tuple[str, int], _Index] = {}


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving one project name."""

    canonical_key: str | None
    registry_present: bool
    matched: bool


@dataclass(frozen=True)
class _Index:
    norm_to_key: dict[str, str]
    override_norm_to_key: dict[str, str]


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    text = str(value)
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return _NON_ALNUM.sub("", text.lower())


def _repo_base(repo_full_name: str | None) -> str:
    return repo_full_name.rsplit("/", 1)[-1] if repo_full_name else ""


def _strip_alias_prefix(alias: str) -> str:
    return alias.split(":", 1)[1] if ":" in alias else alias


def _compile_index(registry: Mapping[str, object]) -> _Index:
    norm_to_key: dict[str, str] = {}
    raw_entries = registry.get("entries", [])
    entries: list[object] = []
    if isinstance(raw_entries, list):
        entries = cast(list[object], raw_entries)
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            continue
        entry = cast(dict[str, object], raw_entry)
        raw_key = entry.get("canonical_key")
        if not isinstance(raw_key, str) or not raw_key:
            continue
        key = raw_key
        raw_display = entry.get("display_name")
        forms = {_normalize(raw_display if isinstance(raw_display, str) else None)}
        repo = entry.get("repo_full_name")
        if repo:
            forms.add(_normalize(_repo_base(repo if isinstance(repo, str) else None)))
        if "/" in key:
            forms.add(_normalize(key))
        raw_aliases = entry.get("aliases", [])
        aliases: list[object] = []
        if isinstance(raw_aliases, list):
            aliases = cast(list[object], raw_aliases)
        for raw_alias in aliases:
            if isinstance(raw_alias, str):
                forms.add(_normalize(_strip_alias_prefix(raw_alias)))
        for form in forms:
            if form:
                norm_to_key.setdefault(form, key)
    override_norm_to_key: dict[str, str] = {}
    raw_overrides = registry.get("resolution_overrides", {})
    overrides: dict[object, object] = (
        cast(dict[object, object], raw_overrides) if isinstance(raw_overrides, dict) else {}
    )
    for raw, key in overrides.items():
        if isinstance(raw, str) and isinstance(key, str):
            override_norm_to_key[_normalize(raw)] = key
    return _Index(norm_to_key=norm_to_key, override_norm_to_key=override_norm_to_key)


def _load_index(registry_path: Path) -> _Index | None:
    try:
        mtime = registry_path.stat().st_mtime_ns
    except OSError:
        return None
    cache_key = (str(registry_path), mtime)
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        registry = cast(object, json.loads(registry_path.read_text()))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(registry, dict):
        return None
    index = _compile_index(cast(dict[str, object], registry))
    _cache.clear()  # only the current registry version matters
    _cache[cache_key] = index
    return index


def resolve(project_name: str, registry_path: Path | None = None) -> Resolution:
    """Resolve a project name to its canonical key via the auditor registry."""
    index = _load_index(registry_path or config.PROJECT_REGISTRY_PATH)
    if index is None:
        return Resolution(canonical_key=None, registry_present=False, matched=False)
    norm = _normalize(project_name)
    if not norm:
        return Resolution(canonical_key=None, registry_present=True, matched=False)
    key = index.override_norm_to_key.get(norm) or index.norm_to_key.get(norm)
    return Resolution(canonical_key=key, registry_present=True, matched=key is not None)
