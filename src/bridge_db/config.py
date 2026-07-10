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


# Bridge markdown file (export target for DB state and fallback read path for file-based clients).
# The default path uses Claude Code's home-dir encoding convention: each `/` in the absolute home
# path becomes `-` (e.g. /home/alice → -home-alice), producing a unique projects/ subdirectory.
# Override via BRIDGE_FILE_PATH if your home dir encodes differently or you keep the file elsewhere.
def _default_bridge_file_path() -> Path:
    home = Path.home()
    # Encode the home path the same way Claude Code does: strip leading slash, replace / with -
    encoded_home = str(home).lstrip("/").replace("/", "-")
    return (
        home
        / ".claude"
        / "projects"
        / f"-{encoded_home}"
        / "memory"
        / "claude_ai_context.md"
    )


BRIDGE_FILE_PATH: Path = Path(
    os.environ.get(
        "BRIDGE_FILE_PATH",
        str(_default_bridge_file_path()),
    )
)

# Logging level (stderr only — stdout is the MCP JSON-RPC channel)
LOG_LEVEL: str = os.environ.get("BRIDGE_DB_LOG_LEVEL", "INFO").upper()

# Retention limits
ACTIVITY_RETENTION_PER_SOURCE: int = 50
SNAPSHOT_RETENTION_PER_SYSTEM: int = 10

# Tags whose rows are permanently exempt from activity retention (BD-INV-1).
# Matched case-insensitively. Do not add other systems' tag names here —
# LEDGER is the universal opt-in for durable entries.
LEDGER_PROTECTED_TAGS: frozenset[str] = frozenset({"SHIPPED", "LEDGER"})

# WAL file size at which `health` surfaces a soft warning. CLAUDE.md notes
# that WAL over "a few MB" is worth a checkpoint. 10 MiB is a comfortable
# default — high enough not to flap on normal workloads, low enough to catch
# genuine bloat before it becomes a problem.
WAL_SIZE_WARN_BYTES: int = 10 * 1024 * 1024

# Audit log (append-only JSONL, co-located with the DB)
AUDIT_LOG_PATH: Path = Path(
    os.environ.get(
        "BRIDGE_DB_AUDIT_LOG_PATH",
        str(DB_PATH.parent / "audit.jsonl"),
    )
)

# Canonical project-identity registry emitted by GithubRepoAuditor. Read-only
# consumer input for resolving project_name -> canonical key on write. If absent,
# resolution is a no-op (pass-through) and logging behaviour is unchanged.
PROJECT_REGISTRY_PATH: Path = Path(
    os.environ.get(
        "BRIDGE_DB_PROJECT_REGISTRY_PATH",
        str(
            Path.home()
            / "Projects"
            / "GithubRepoAuditor"
            / "output"
            / "project-registry.json"
        ),
    )
)

# Local policy for SHIPPED events that are valid cross-system/meta receipts but
# should not be reconciled to a Notion Local Portfolio project row.
META_SHIPPED_EVENTS_PATH: Path = Path(
    os.environ.get(
        "BRIDGE_DB_META_SHIPPED_EVENTS_PATH",
        str(
            Path(__file__).resolve().parents[2] / "config" / "meta-shipped-events.json"
        ),
    )
)

# Principal enrollment store: maps sha256(token) -> caller id. Operator-managed
# via `python -m bridge_db --enroll <caller>`; mode 0600. Override for tests.
PRINCIPALS_PATH: Path = Path(
    os.environ.get(
        "BRIDGE_DB_PRINCIPALS_PATH",
        str(DB_PATH.parent / "principals.json"),
    )
)

# Auth rollout dial: 'off' (legacy, no checks), 'warn' (allow + audit mismatches),
# 'enforce' (reject mismatches and unbound writes). Unrecognized values are
# treated as 'enforce' by auth.auth_mode() — fail closed.
AUTH_MODE: str = os.environ.get("BRIDGE_DB_AUTH_MODE", "off")

# Context-section CAS rollout dial: 'warn' keeps legacy existing-row blind writes
# compatible but marks/audits them; 'enforce' rejects existing-row blind writes.
CONTEXT_CAS_MODE: str = os.environ.get("BRIDGE_DB_CONTEXT_CAS_MODE", "warn")
