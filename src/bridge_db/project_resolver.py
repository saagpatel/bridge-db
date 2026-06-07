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
from dataclasses import dataclass
from pathlib import Path

from bridge_db import config

_NON_ALNUM = re.compile(r"[^a-z0-9]")

# (path, mtime_ns) -> compiled index, so a long-running server picks up new
# auditor runs without restarting and without re-reading on every call.
_cache: dict[tuple[str, int], "_Index"] = {}


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


def _compile_index(registry: dict) -> _Index:
    norm_to_key: dict[str, str] = {}
    for entry in registry.get("entries", []):
        key = entry.get("canonical_key")
        if not key:
            continue
        forms = {_normalize(entry.get("display_name"))}
        repo = entry.get("repo_full_name")
        if repo:
            forms.add(_normalize(_repo_base(repo)))
        if "/" in (key or ""):
            forms.add(_normalize(key))
        for alias in entry.get("aliases", []):
            forms.add(_normalize(_strip_alias_prefix(alias)))
        for form in forms:
            if form:
                norm_to_key.setdefault(form, key)
    override_norm_to_key = {
        _normalize(raw): key for raw, key in registry.get("resolution_overrides", {}).items()
    }
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
        registry = json.loads(registry_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    index = _compile_index(registry)
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
