"""Configuration: paths, constants, env var overrides."""

import os
from pathlib import Path

# Database location (XDG convention; override via BRIDGE_DB_PATH)
DB_PATH: Path = Path(
    os.environ.get(
        "BRIDGE_DB_PATH",
        str(Path.home() / ".local" / "share" / "bridge-db" / "bridge.db"),
    )
)

# Bridge markdown file (export target for DB state and fallback read path for file-based clients)
BRIDGE_FILE_PATH: Path = Path(
    os.environ.get(
        "BRIDGE_FILE_PATH",
        str(Path.home() / ".claude" / "projects" / "-Users-d" / "memory" / "claude_ai_context.md"),
    )
)

# Logging level (stderr only — stdout is the MCP JSON-RPC channel)
LOG_LEVEL: str = os.environ.get("BRIDGE_DB_LOG_LEVEL", "INFO").upper()

# Retention limits
ACTIVITY_RETENTION_PER_SOURCE: int = 50
SNAPSHOT_RETENTION_PER_SYSTEM: int = 10

# Audit log (append-only JSONL, co-located with the DB)
AUDIT_LOG_PATH: Path = Path(
    os.environ.get(
        "BRIDGE_DB_AUDIT_LOG_PATH",
        str(DB_PATH.parent / "audit.jsonl"),
    )
)