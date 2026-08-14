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
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast, overload

from mcp.server.fastmcp.exceptions import ToolError

from bridge_db import clock, config
from bridge_db.audit import log_audit
from bridge_db.models import PRINCIPAL_IDS, SourceTrust

logger = logging.getLogger("bridge_db.auth")

_VALID_MODES = frozenset({"off", "warn", "enforce"})
PRINCIPALS_VERSION = 2
GRANT_TTL_DAYS = 90

_SCOPES_BY_CALLER: dict[str, frozenset[str]] = {
    "cc": frozenset(
        {
            "log_activity",
            "save_snapshot",
            "get_snapshot_capacity",
            "acknowledge_snapshot_refusal",
            "record_cost",
            "record_disposition",
            "pick_up_handoff",
            "clear_handoff",
            "sync_from_file",
            "export_bridge_markdown",
            "seal_recovery_batch",
        }
    ),
    "codex": frozenset(
        {
            "log_activity",
            "save_snapshot",
            "get_snapshot_capacity",
            "acknowledge_snapshot_refusal",
            "record_cost",
            "record_disposition",
            "pick_up_handoff",
            "clear_handoff",
            "export_bridge_markdown",
            "seal_recovery_batch",
        }
    ),
    "claude_ai": frozenset(
        {
            "update_section",
            "create_handoff",
            "export_bridge_markdown",
        }
    ),
    "notion_os": frozenset(
        {
            "log_activity",
            "record_cost",
            "record_disposition",
            "export_bridge_markdown",
        }
    ),
    "personal_ops": frozenset(
        {
            "log_activity",
            "record_cost",
            "record_disposition",
            "export_bridge_markdown",
        }
    ),
    # Hermes consumes read tools only. Keeping this explicit and empty makes
    # enrollment possible without granting any caller-bearing write surface.
    "hermes": frozenset(),
}


@dataclass(frozen=True)
class PrincipalGrant:
    caller: str
    issued_at: datetime
    expires_at: datetime
    generation: int
    scopes: frozenset[str]


def scopes_for_caller(caller: str) -> list[str]:
    """Return the closed default action scope for a known caller."""
    return sorted(_SCOPES_BY_CALLER.get(caller, frozenset()))


def _parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def auth_mode() -> str:
    """Normalize config.AUTH_MODE; anything unrecognized fails closed to 'enforce'."""
    mode = config.AUTH_MODE.strip().lower()
    return mode if mode in _VALID_MODES else "enforce"


def load_principal_grants(path: Path) -> dict[str, PrincipalGrant]:
    """Read a v2 enrollment file into a sha256(token) -> scoped grant map.

    Missing, legacy-v1, or malformed files fail closed to no grants.
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
        raw_dict = cast(dict[str, object], raw)
        if raw_dict.get("version") != PRINCIPALS_VERSION:
            logger.warning("unsupported principals registry version: %r", raw_dict.get("version"))
            return {}
        entries = raw_dict.get("principals")
        if not isinstance(entries, dict):
            return {}
        result: dict[str, PrincipalGrant] = {}
        for caller, entry in cast(dict[str, object], entries).items():
            if caller not in PRINCIPAL_IDS or not isinstance(entry, dict):
                continue
            entry_dict = cast(dict[str, object], entry)
            token_hash = entry_dict.get("token_sha256")
            issued_at = _parse_utc_timestamp(entry_dict.get("issued_at"))
            expires_at = _parse_utc_timestamp(entry_dict.get("expires_at"))
            generation = entry_dict.get("generation")
            scopes_value = entry_dict.get("scopes")
            allowed_scopes = _SCOPES_BY_CALLER[caller]
            if (
                not isinstance(token_hash, str)
                or len(token_hash) != 64
                or any(char not in "0123456789abcdef" for char in token_hash)
                or issued_at is None
                or expires_at is None
                or not isinstance(generation, int)
                or isinstance(generation, bool)
                or generation < 1
                or not isinstance(scopes_value, list)
            ):
                continue
            scopes = cast(list[object], scopes_value)
            if not all(isinstance(scope, str) for scope in scopes):
                continue
            scope_set = frozenset(cast(list[str], scopes))
            if (not scope_set and allowed_scopes) or not scope_set.issubset(
                allowed_scopes
            ):
                continue
            if token_hash in result:
                logger.warning("duplicate token hash in principals registry")
                return {}
            result[token_hash] = PrincipalGrant(
                caller=caller,
                issued_at=issued_at,
                expires_at=expires_at,
                generation=generation,
                scopes=scope_set,
            )
        return result
    except (json.JSONDecodeError, TypeError):
        logger.warning("principals file unreadable: %s", path)
        return {}


def load_principals(path: Path) -> dict[str, str]:
    """Compatibility view of valid v2 grants as sha256(token) -> caller."""
    return {
        token_hash: grant.caller
        for token_hash, grant in load_principal_grants(path).items()
    }


def resolve_grant(
    token: str | None, grants: dict[str, PrincipalGrant]
) -> PrincipalGrant | None:
    if not token:
        return None
    return grants.get(hash_token(token))


def resolve_principal(token: str | None, principals: dict[str, str]) -> str | None:
    if not token:
        return None
    return principals.get(hash_token(token))


def _bound_grant_state(
    ctx: Any, tool: str | None = None
) -> tuple[PrincipalGrant | None, str | None, str | None]:
    try:
        lifespan = ctx.request_context.lifespan_context
        principal = getattr(lifespan, "principal", None)
        credential_hash = getattr(lifespan, "credential_hash", None)
        credential_generation = getattr(lifespan, "credential_generation", None)
    except AttributeError:
        return None, "unbound", None
    if credential_hash is None:
        return None, None, principal
    grant = load_principal_grants(config.PRINCIPALS_PATH).get(credential_hash)
    if grant is None:
        return None, "not_enrolled", principal
    if grant.caller != principal:
        return None, "principal_changed", principal
    if credential_generation != grant.generation:
        return None, "generation_changed", principal
    if clock.now() >= grant.expires_at:
        return None, "expired", principal
    if tool is not None and tool not in grant.scopes:
        return None, "out_of_scope", principal
    return grant, None, principal


def get_principal(ctx: Any) -> str | None:
    """Return the still-valid bound principal. None-safe and fail-closed."""
    grant, reason, cached = _bound_grant_state(ctx)
    if reason is None:
        return grant.caller if grant is not None else cached
    return None


def _reject_invalid_credential(ctx: Any, caller: str, tool: str) -> None:
    _grant, reason, cached = _bound_grant_state(ctx, tool)
    if reason is None or (reason == "unbound" and cached is None):
        return
    detail = f"tool={tool} principal={cached or 'unbound'} caller={caller} reason={reason}"
    event = "auth.revoked" if reason == "not_enrolled" else "auth.denied"
    log_audit(event, cached, None, ok=False, detail=detail)
    logger.warning("credential refused: %s", detail)
    messages = {
        "expired": "Bound credential has expired; enroll and bind a new credential",
        "out_of_scope": f"Bound credential is not scoped for tool '{tool}'",
        "generation_changed": "Bound credential generation changed; restart with the current grant",
        "principal_changed": "Bound credential principal changed; restart with the current grant",
        "not_enrolled": "Bound credential is no longer enrolled; restart and bind a current token",
    }
    raise ToolError(messages.get(reason, "Bound credential is invalid"))


def require_bound_caller(ctx: Any, caller: str, tool: str) -> None:
    """Require an exact channel-bound principal regardless of rollout mode.

    Sensitive sinks use this instead of ``require_caller`` so the global
    compatibility dial cannot disable their identity boundary. Mismatch audit
    attribution comes from the bound principal, never the request's claim.
    """
    _reject_invalid_credential(ctx, caller, tool)
    principal = get_principal(ctx)
    if principal == caller:
        return
    detail = f"tool={tool} principal={principal or 'unbound'} caller={caller} mode=strict"
    log_audit("auth.mismatch", principal, None, ok=False, detail=detail)
    logger.warning("auth mismatch: %s", detail)
    if principal is None:
        raise ToolError(
            "Unauthenticated connection: no BRIDGE_DB_PRINCIPAL_TOKEN bound. "
            "Enroll with `python -m bridge_db --enroll <caller>` and set the "
            "token in this client's MCP spawn env."
        )
    raise ToolError(f"Caller mismatch: connection bound to '{principal}', cannot act as '{caller}'")


def require_bound_principal(ctx: Any, tool: str) -> str:
    """Require and return a live enrolled principal for a caller-less mutation.

    A small number of tools derive their actor solely from the channel binding
    instead of accepting a caller claim. They still fail closed regardless of
    the rollout dial and revalidate credential enrollment before mutation.
    """
    _reject_invalid_credential(ctx, "implicit", tool)
    principal = get_principal(ctx)
    if principal is not None:
        return principal
    detail = f"tool={tool} principal=unbound decision=unbound mode=strict"
    log_audit("auth.mismatch", None, None, ok=False, detail=detail)
    logger.warning("auth mismatch: %s", detail)
    raise ToolError(
        "Unauthenticated connection: no BRIDGE_DB_PRINCIPAL_TOKEN bound. "
        "Enroll a principal and set the token in this client's MCP spawn env."
    )


def require_cli_principal(tool: str) -> str:
    """Require a live scoped principal for a non-MCP lifecycle command.

    The identity comes only from ``BRIDGE_DB_PRINCIPAL_TOKEN`` and the current
    v2 registry. The global auth rollout mode never weakens this boundary.
    """
    raw_token = os.environ.get("BRIDGE_DB_PRINCIPAL_TOKEN")
    token = raw_token.strip() if raw_token is not None else None
    grant = resolve_grant(token, load_principal_grants(config.PRINCIPALS_PATH))
    reason: str | None = None
    if not token:
        reason = "unbound"
    elif grant is None:
        reason = "not_enrolled"
    elif clock.now() >= grant.expires_at:
        reason = "expired"
    elif tool not in grant.scopes:
        reason = "out_of_scope"
    if reason is None and grant is not None:
        return grant.caller
    if reason is None:
        reason = "invalid"

    principal = grant.caller if grant is not None else None
    detail = f"tool={tool} principal={principal or 'unbound'} reason={reason}"
    log_audit("auth.denied", principal, None, ok=False, detail=detail)
    messages = {
        "unbound": (
            "No channel credential is bound; set BRIDGE_DB_PRINCIPAL_TOKEN "
            "to a current scoped grant"
        ),
        "not_enrolled": "The bound credential is not enrolled",
        "expired": "The bound credential has expired",
        "out_of_scope": f"The bound credential is not scoped for '{tool}'",
        "invalid": "The bound credential is invalid",
    }
    raise PermissionError(messages[reason])


def require_caller(ctx: Any, caller: str, tool: str) -> None:
    """Cross-check the claimed caller against the connection-bound principal.

    off: no-op. warn: allow but audit mismatches. enforce: reject mismatches
    and unbound connections. Match is always silent.
    """
    _reject_invalid_credential(ctx, caller, tool)
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


@overload
def clamp_source_trust(
    requested: SourceTrust, caller: str, tool: str, *, strict: bool = False
) -> tuple[SourceTrust, bool]: ...


@overload
def clamp_source_trust(
    requested: None, caller: str, tool: str, *, strict: bool = False
) -> tuple[None, bool]: ...


def clamp_source_trust(
    requested: SourceTrust | None,
    caller: str,
    tool: str,
    *,
    strict: bool = False,
) -> tuple[SourceTrust | None, bool]:
    """Block MCP-side minting of the 'operator' label.

    Returns (stored_value, clamped). Active in warn and enforce modes; 'off'
    preserves legacy behavior unless ``strict`` is true for a sensitive sink.
    Operator labels are minted only via a TTY-gated CLI or pre-existing rows
    wherever strict clamping applies.
    """
    if (auth_mode() == "off" and not strict) or requested != "operator":
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
