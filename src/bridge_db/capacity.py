"""Shared reject-before-write capacity checks with stable error codes."""

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

from mcp.server.fastmcp.exceptions import ToolError


def utf8_size(value: str | None) -> int:
    return len((value or "").encode("utf-8"))


def require_utf8_bytes(value: str | None, maximum: int, code: str) -> int:
    size = utf8_size(value)
    if size > maximum:
        raise ToolError(f"{code}: maximum={maximum} actual={size}")
    return size


def require_combined_bytes(sizes: list[int], maximum: int, code: str) -> None:
    total = sum(sizes)
    if total > maximum:
        raise ToolError(f"{code}: maximum={maximum} actual={total}")


def encode_bounded_json(
    value: Mapping[str, Any],
    *,
    maximum_bytes: int,
    maximum_depth: int,
    maximum_nodes: int,
    code_prefix: str,
) -> str:
    """Validate a JSON-compatible mapping iteratively, then serialize it."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > maximum_depth:
            raise ToolError(
                f"{code_prefix}.json_depth_exceeded: "
                f"maximum={maximum_depth} actual_at_least={depth}"
            )
        if nodes > maximum_nodes:
            raise ToolError(
                f"{code_prefix}.json_nodes_exceeded: "
                f"maximum={maximum_nodes} actual_at_least={nodes}"
            )
        if isinstance(current, Mapping):
            mapping = cast(Mapping[object, Any], current)
            stack.extend((item, depth + 1) for item in mapping.values())
        elif isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            sequence = cast(Sequence[Any], current)
            stack.extend((item, depth + 1) for item in sequence)

    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    size = utf8_size(encoded)
    if size > maximum_bytes:
        raise ToolError(
            f"{code_prefix}.json_utf8_bytes_exceeded: "
            f"maximum={maximum_bytes} actual={size}"
        )
    return encoded
