"""Shared types: CallerID, ownership maps, error helpers."""

from typing import Literal

# The three systems that share the bridge
CallerID = Literal["cc", "codex", "claude_ai"]

# Systems that own activity/snapshot/cost records (not claude_ai — it uses context_sections)
SystemID = Literal["cc", "codex"]

# Which section names belong to which owner
# Keys are the section_name values stored in context_sections
SECTION_OWNERS: dict[str, CallerID] = {
    "career": "claude_ai",
    "speaking": "claude_ai",
    "research": "claude_ai",
    "capabilities": "claude_ai",
}

# Callers allowed to log activity per source column value
# activity_log.source maps directly from caller
ACTIVITY_ALLOWED_CALLERS: set[CallerID] = {"cc", "codex", "claude_ai"}

# Callers allowed to save snapshots per system
# system_snapshots.system maps from caller, but only cc/codex own snapshots
SNAPSHOT_SYSTEM_MAP: dict[CallerID, SystemID] = {
    "cc": "cc",
    "codex": "codex",
}

# Callers allowed to record costs
COST_SYSTEM_MAP: dict[CallerID, SystemID] = {
    "cc": "cc",
    "codex": "codex",
}


def ownership_error(caller: str, section: str, owner: str) -> str:
    return (
        f"Ownership violation: caller '{caller}' cannot write to section '{section}' "
        f"owned by '{owner}'"
    )


def snapshot_ownership_error(caller: str) -> str:
    return f"Caller '{caller}' cannot save snapshots. Only 'cc' and 'codex' own snapshot data."


def cost_ownership_error(caller: str) -> str:
    return f"Caller '{caller}' cannot record costs. Only 'cc' and 'codex' own cost records."
