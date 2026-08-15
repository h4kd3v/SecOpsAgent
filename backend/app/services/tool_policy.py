"""Decides which MCP tools may run unattended.

Order of trust:
  1. The MCP server's own annotations (`read_only_hint` / `destructive_hint`).
  2. Operator-configured name regexes.
  3. Fail closed — unknown tools are treated as writes and need approval.

With anonymous access there is no role hierarchy, so the approval gate is a
confirmation step rather than an authorisation boundary: any analyst may
approve, but nothing state-changing runs without a human clicking approve.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings

settings = get_settings()


def _hint(annotations: Any, *names: str) -> Any:
    """Annotation field names differ between MCP SDK 1.x and 2.0."""
    for name in names:
        value = getattr(annotations, name, None)
        if value is not None:
            return value
    return None


def classify(name: str, read_only_hint: Any = None, destructive_hint: Any = None) -> bool:
    """True if the tool is safe to auto-execute.

    Takes plain values rather than an SDK object so a cached catalogue row can
    be re-classified on read — which means changing TOOL_READONLY_PATTERNS
    takes effect immediately, with no refetch from the MCP server.
    """
    if destructive_hint:
        return False
    if read_only_hint is not None:
        return bool(read_only_hint)
    return any(rx.search(name or "") for rx in settings.readonly_regexes)


def extract_hints(tool: Any) -> tuple[Any, Any]:
    """Pull the annotation hints off an MCP SDK Tool object."""
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return None, None
    return (
        _hint(annotations, "read_only_hint", "readOnlyHint"),
        _hint(annotations, "destructive_hint", "destructiveHint"),
    )


def classify_tool(tool: Any) -> bool:
    read_only_hint, destructive_hint = extract_hints(tool)
    return classify(getattr(tool, "name", "") or "", read_only_hint, destructive_hint)


def requires_approval(read_only: bool) -> bool:
    return (not read_only) and settings.require_approval_for_write
