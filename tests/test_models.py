"""Tests for shared bridge model constants."""

from bridge_db.models import (
    ACTIVITY_SOURCES,
    CALLER_IDS,
    COST_SYSTEM_MAP,
    NOTIFICATION_SOURCE_ALIASES,
    PRINCIPAL_IDS,
    SYSTEM_IDS,
)


def test_activity_sources_track_caller_ids() -> None:
    assert frozenset(CALLER_IDS) == ACTIVITY_SOURCES


def test_principal_ids_include_read_only_hermes_without_write_caller_authority() -> None:
    assert "hermes" in PRINCIPAL_IDS
    assert "hermes" not in CALLER_IDS


def test_system_ids_track_cost_system_map() -> None:
    assert set(SYSTEM_IDS) == set(COST_SYSTEM_MAP.values())


def test_notification_source_aliases_cover_every_caller() -> None:
    assert set(NOTIFICATION_SOURCE_ALIASES) == set(CALLER_IDS)
    assert NOTIFICATION_SOURCE_ALIASES["notion_os"] == "notion-os"
    assert NOTIFICATION_SOURCE_ALIASES["personal_ops"] == "personal-ops"
