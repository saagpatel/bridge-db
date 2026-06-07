"""Tests for the canonical project-name resolver (consumer of the auditor registry)."""

from __future__ import annotations

import json
from pathlib import Path

from bridge_db.project_resolver import resolve


def _entry(
    key: str,
    display: str,
    repo: str | None = None,
    aliases: list[str] | None = None,
    bridge_project_names: list[str] | None = None,
    notion_local_page_id: str | None = None,
    notion_local_title: str | None = None,
) -> dict[str, object]:
    return {
        "canonical_key": key,
        "display_name": display,
        "repo_full_name": repo,
        "aliases": aliases or [],
        "bridge_project_names": bridge_project_names or [],
        "notion_local_page_id": notion_local_page_id,
        "notion_local_title": notion_local_title,
    }


def _write_registry(
    path: Path, entries: list[dict[str, object]], overrides: dict[str, str] | None = None
) -> Path:
    path.write_text(json.dumps({"entries": entries, "resolution_overrides": overrides or {}}))
    return path


def test_absent_registry_is_pass_through(tmp_path: Path) -> None:
    result = resolve("MCPAudit", registry_path=tmp_path / "missing.json")
    assert result.registry_present is False
    assert result.matched is False
    assert result.canonical_key is None


def test_matches_display_and_alias_spellings(tmp_path: Path) -> None:
    reg = _write_registry(
        tmp_path / "r.json", [_entry("MCPAudit", "MCPAudit", aliases=["notion:MCP Audit"])]
    )
    assert resolve("MCP Audit", registry_path=reg).canonical_key == "MCPAudit"
    assert resolve("mcpaudit", registry_path=reg).canonical_key == "MCPAudit"


def test_matches_bridge_project_names_and_exposes_notion_target(tmp_path: Path) -> None:
    reg = _write_registry(
        tmp_path / "r.json",
        [
            _entry(
                "Fun:GamePrjs/LoreKeeper",
                "LoreKeeper",
                repo="saagpatel/LoreKeeper",
                bridge_project_names=["lore-keeper-ship-lane"],
                notion_local_page_id="326c21f1-caf0-81c3-8759-e5aa28dee730",
                notion_local_title="LoreKeeper",
            )
        ],
    )

    result = resolve("lore-keeper-ship-lane", registry_path=reg)

    assert result.canonical_key == "Fun:GamePrjs/LoreKeeper"
    assert result.notion_page_id == "326c21f1-caf0-81c3-8759-e5aa28dee730"
    assert result.notion_title == "LoreKeeper"


def test_override_resolves_hard_normalization_failure(tmp_path: Path) -> None:
    reg = _write_registry(
        tmp_path / "r.json",
        [_entry("Notion", "Notion", repo="saagpatel/notion-operating-system")],
        overrides={"notion_os": "Notion"},
    )
    assert resolve("notion_os", registry_path=reg).canonical_key == "Notion"


def test_present_but_unmatched_is_flagged_not_passed_through(tmp_path: Path) -> None:
    reg = _write_registry(tmp_path / "r.json", [_entry("MCPAudit", "MCPAudit")])
    result = resolve("weekly-review", registry_path=reg)
    assert result.registry_present is True
    assert result.matched is False
    assert result.canonical_key is None


def test_reloads_when_registry_file_changes(tmp_path: Path) -> None:
    reg = _write_registry(tmp_path / "r.json", [_entry("MCPAudit", "MCPAudit")])
    assert resolve("Recall", registry_path=reg).matched is False
    # Auditor re-runs and rewrites the registry; force a distinct mtime so the
    # mtime-keyed cache reloads rather than serving the stale index.
    bumped = reg.stat().st_mtime_ns + 1_000_000_000
    _write_registry(reg, [_entry("MCPAudit", "MCPAudit"), _entry("Recall", "Recall")])
    import os

    os.utime(reg, ns=(bumped, bumped))
    assert resolve("Recall", registry_path=reg).canonical_key == "Recall"
