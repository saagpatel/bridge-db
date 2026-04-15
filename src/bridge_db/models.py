"""Shared types: CallerID, ownership maps, error helpers."""

from typing import Literal

# All systems that can interact with the bridge
CallerID = Literal["cc", "codex", "claude_ai", "notion_os", "personal_ops"]

# Systems that own activity/snapshot/cost records
# claude_ai uses context_sections; notion_os/personal_ops log activity and costs
SystemID = Literal["cc", "codex", "notion_os", "personal_ops"]

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
ACTIVITY_ALLOWED_CALLERS: set[CallerID] = {"cc", "codex", "claude_ai", "notion_os", "personal_ops"}

# Callers allowed to save snapshots per system
# Only cc/codex own full state snapshots; notion_os/personal_ops use activity log instead
SNAPSHOT_SYSTEM_MAP: dict[str, SystemID] = {
    "cc": "cc",
    "codex": "codex",
}

# Callers allowed to record costs (maps caller → system column value)
COST_SYSTEM_MAP: dict[str, SystemID] = {
    "cc": "cc",
    "codex": "codex",
    "notion_os": "notion_os",
    "personal_ops": "personal_ops",
}


def ownership_error(caller: str, section: str, owner: str) -> str:
    return (
        f"Ownership violation: caller '{caller}' cannot write to section '{section}' "
        f"owned by '{owner}'"
    )


def snapshot_ownership_error(caller: str) -> str:
    return f"Caller '{caller}' cannot save snapshots. Only 'cc' and 'codex' own snapshot data."


def cost_ownership_error(caller: str) -> str:
    allowed = ", ".join(f"'{k}'" for k in COST_SYSTEM_MAP)
    return f"Caller '{caller}' cannot record costs. Allowed callers: {allowed}."
