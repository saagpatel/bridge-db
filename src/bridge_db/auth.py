"""Channel-derived principal identity: enrollment store, binding, caller checks.

Every identity and label-authority decision lives here. The server binds one
principal per stdio connection at startup (from BRIDGE_DB_PRINCIPAL_TOKEN);
write tools call require_caller() to cross-check the claimed caller against
that binding, and minting tools call clamp_source_trust() to block operator
label self-promotion. Behavior is governed by config.AUTH_MODE.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, cast

from mcp.server.fastmcp.exceptions import ToolError

from bridge_db import config
from bridge_db.audit import log_audit
from bridge_db.models import SourceTrust

logger = logging.getLogger("bridge_db.auth")

_VALID_MODES = frozenset({"off", "warn", "enforce"})


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def auth_mode() -> str:
    """Normalize config.AUTH_MODE; anything unrecognized fails closed to 'enforce'."""
    mode = config.AUTH_MODE.strip().lower()
    return mode if mode in _VALID_MODES else "enforce"


def load_principals(path: Path) -> dict[str, str]:
    """Read the enrollment file into a sha256(token) -> caller map.

    Missing or malformed file -> {} (nothing binds, enforce mode denies writes).
    A vanished file (deleted between calls) is treated as missing and returns {}.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        logger.warning("principals file unreadable: %s", path)
        return {}
    try:
        raw = cast(object, json.loads(text))
        if not isinstance(raw, dict):
            return {}
        entries = cast(dict[str, object], raw).get("principals")
        if not isinstance(entries, dict):
            return {}
        result: dict[str, str] = {}
        for caller, entry in cast(dict[str, object], entries).items():
            if not isinstance(entry, dict):
                continue
            token_hash = cast(dict[str, object], entry).get("token_sha256")
            if isinstance(token_hash, str):
                result[token_hash] = caller
        return result
    except (json.JSONDecodeError, TypeError):
        logger.warning("principals file unreadable: %s", path)
        return {}


def resolve_principal(token: str | None, principals: dict[str, str]) -> str | None:
    if not token:
        return None
    return principals.get(hash_token(token))


def get_principal(ctx: Any) -> str | None:
    """Read the connection-bound principal off the lifespan context. None-safe."""
    try:
        return getattr(ctx.request_context.lifespan_context, "principal", None)
    except AttributeError:
        return None


def require_caller(ctx: Any, caller: str, tool: str) -> None:
    """Cross-check the claimed caller against the connection-bound principal.

    off: no-op. warn: allow but audit mismatches. enforce: reject mismatches
    and unbound connections. Match is always silent.
    """
    mode = auth_mode()
    if mode == "off":
        return
    principal = get_principal(ctx)
    if principal == caller:
        return
    detail = f"tool={tool} principal={principal or 'unbound'} caller={caller} mode={mode}"
    log_audit("auth.mismatch", caller, None, ok=False, detail=detail)
    logger.warning("auth mismatch: %s", detail)
    if mode == "warn":
        return
    if principal is None:
        raise ToolError(
            "Unauthenticated connection: no BRIDGE_DB_PRINCIPAL_TOKEN bound. "
            "Enroll with `python -m bridge_db --enroll <caller>` and set the "
            "token in this client's MCP spawn env."
        )
    raise ToolError(f"Caller mismatch: connection bound to '{principal}', cannot act as '{caller}'")


def clamp_source_trust(
    requested: SourceTrust | None, caller: str, tool: str
) -> tuple[SourceTrust | None, bool]:
    """Block MCP-side minting of the 'operator' label.

    Returns (stored_value, clamped). Active in warn and enforce modes; 'off'
    preserves legacy behavior so the rollback lever stays total. Operator
    labels are minted only via the TTY-gated CLI (--promote-section) or
    pre-existing rows.
    """
    if auth_mode() == "off" or requested != "operator":
        return requested, False
    log_audit(
        "auth.trust_clamped",
        caller,
        None,
        ok=False,
        detail=f"tool={tool} requested=operator stored=agent",
    )
    logger.warning("source_trust clamp: tool=%s caller=%s operator->agent", tool, caller)
    return "agent", True
