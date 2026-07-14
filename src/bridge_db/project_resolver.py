"""Resolve free-form project names to canonical keys.

Read-only consumer of GithubRepoAuditor's ``project-registry.json``. bridge-db
never hard-depends on it: if the registry file is absent or unreadable, every
resolution returns ``registry_present=False`` and a ``None`` canonical key, so
``log_activity`` behaves exactly as before. When the registry IS present and a
name does not match, that is surfaced (``matched=False``) so drift can be
flagged via the existing audit log rather than silently recorded.

This is a consumer-side resolution helper, not a new coordination surface:
bridge-db stores the resolved key alongside the activity it already stores.
GithubRepoAuditor owns the keyspace; bridge-db stores GHRA's repo_full_name
natural key when one exists, and leaves repo-less/unmatched rows as ``NULL``.
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
    ambiguous: bool = False
    notion_page_id: str | None = None
    notion_title: str | None = None


@dataclass(frozen=True)
class _Index:
    exact_to_entries: dict[str, frozenset[_ResolvedEntry]]
    norm_to_entries: dict[str, frozenset[_ResolvedEntry]]
    override_norm_to_entries: dict[str, frozenset[_ResolvedEntry]]


@dataclass(frozen=True)
class _ResolvedEntry:
    canonical_key: str | None
    notion_page_id: str | None = None
    notion_title: str | None = None


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    text = str(value)
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return _NON_ALNUM.sub("", text.lower())


def _exact(value: str | None) -> str:
    return value.strip().casefold() if value else ""


def _repo_base(repo_full_name: str | None) -> str:
    return repo_full_name.rsplit("/", 1)[-1] if repo_full_name else ""


def _strip_alias_prefix(alias: str) -> str:
    return alias.split(":", 1)[1] if ":" in alias else alias


def _entry_canonical_key(entry: Mapping[str, object]) -> str | None:
    repo = entry.get("repo_full_name")
    if isinstance(repo, str) and repo.strip():
        return repo.strip()
    # Repo-less projects carry a stable ``supp:<slug>`` key minted by the
    # auditor (GHRA owns the keyspace) per the signed IDENTITY-DECISION-RECORD.
    # Older registries without the field resolve to None, unchanged.
    supp = entry.get("supp_key")
    if isinstance(supp, str) and supp.startswith("supp:"):
        return supp
    return None


def _compile_index(registry: Mapping[str, object]) -> _Index:
    exact_candidates: dict[str, set[_ResolvedEntry]] = {}
    norm_candidates: dict[str, set[_ResolvedEntry]] = {}
    entries_by_legacy_key: dict[str, _ResolvedEntry] = {}
    entries_by_repo_full_name: dict[str, _ResolvedEntry] = {}
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
        raw_display = entry.get("display_name")
        display = raw_display if isinstance(raw_display, str) else None
        raw_forms: set[str] = set()
        if display is not None:
            raw_forms.add(display)
        repo = entry.get("repo_full_name")
        if isinstance(repo, str) and repo:
            raw_forms.add(_repo_base(repo))
            raw_forms.add(repo)
        raw_forms.add(raw_key)
        raw_bridge_names = entry.get("bridge_project_names", [])
        bridge_names: list[object] = []
        if isinstance(raw_bridge_names, list):
            bridge_names = cast(list[object], raw_bridge_names)
        for raw_bridge_name in bridge_names:
            if isinstance(raw_bridge_name, str):
                raw_forms.add(raw_bridge_name)
        raw_aliases = entry.get("aliases", [])
        aliases: list[object] = []
        if isinstance(raw_aliases, list):
            aliases = cast(list[object], raw_aliases)
        for raw_alias in aliases:
            if isinstance(raw_alias, str):
                raw_forms.add(_strip_alias_prefix(raw_alias))
        page_id = entry.get("notion_local_page_id")
        title = entry.get("notion_local_title")
        resolved = _ResolvedEntry(
            canonical_key=_entry_canonical_key(entry),
            notion_page_id=page_id.strip() if isinstance(page_id, str) and page_id.strip() else None,
            notion_title=title if isinstance(title, str) and title else display,
        )
        entries_by_legacy_key[raw_key] = resolved
        if resolved.canonical_key is not None:
            entries_by_repo_full_name[resolved.canonical_key] = resolved
        for raw_form in raw_forms:
            exact_form = _exact(raw_form)
            norm_form = _normalize(raw_form)
            if exact_form:
                exact_candidates.setdefault(exact_form, set()).add(resolved)
            if norm_form:
                norm_candidates.setdefault(norm_form, set()).add(resolved)
    override_candidates: dict[str, set[_ResolvedEntry]] = {}
    raw_overrides = registry.get("resolution_overrides", {})
    overrides: dict[object, object] = (
        cast(dict[object, object], raw_overrides) if isinstance(raw_overrides, dict) else {}
    )
    for raw, key in overrides.items():
        if isinstance(raw, str) and isinstance(key, str):
            target = entries_by_legacy_key.get(key) or entries_by_repo_full_name.get(key)
            if target is None and "/" in key:
                target = _ResolvedEntry(canonical_key=key)
            if target is not None:
                override_candidates.setdefault(_normalize(raw), set()).add(target)
    return _Index(
        exact_to_entries={
            form: frozenset(candidates) for form, candidates in exact_candidates.items()
        },
        norm_to_entries={
            form: frozenset(candidates) for form, candidates in norm_candidates.items()
        },
        override_norm_to_entries={
            form: frozenset(candidates) for form, candidates in override_candidates.items()
        },
    )


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
    candidates = index.override_norm_to_entries.get(norm)
    if candidates is None:
        candidates = index.exact_to_entries.get(_exact(project_name))
    if candidates is None:
        candidates = index.norm_to_entries.get(norm)
    if not candidates:
        return Resolution(canonical_key=None, registry_present=True, matched=False)
    if len(candidates) != 1:
        return Resolution(
            canonical_key=None,
            registry_present=True,
            matched=False,
            ambiguous=True,
        )
    entry = next(iter(candidates))
    return Resolution(
        canonical_key=entry.canonical_key,
        registry_present=True,
        matched=True,
        notion_page_id=entry.notion_page_id,
        notion_title=entry.notion_title,
    )
