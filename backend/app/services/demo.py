"""Simulated LLM proxy and SecOps MCP server, for running with no credentials.

Only the two external services are faked. Postgres, the migrations, the agent
loop, SSE streaming, the approval gate, the audit trail and the live sidebar
are all the real code paths — so this is a genuine smoke test of the stack,
not just a UI mock.

Enabled with `DEMO_MODE=true`. Never enable it against a real deployment: it
answers with invented data, which is the one thing this app otherwise refuses
to do.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.services.mcp_manager import ToolResult, ToolSpec

logger = logging.getLogger(__name__)

DEMO_MODEL = "anthropic/claude-opus-4-6-20260101"

# Deliberately shaped like real SecOps tooling, including one write tool so the
# approval gate is visible without touching anything.
DEMO_TOOLS = [
    ToolSpec(
        name="search_udm_events",
        description="Search UDM events over a time window.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "hours": {"type": "integer", "default": 24},
            },
            "required": ["query"],
        },
        read_only=True,
    ),
    ToolSpec(
        name="list_alerts",
        description="List open alerts, most recent first.",
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
        },
        read_only=True,
    ),
    ToolSpec(
        name="close_case",
        description="Close a case and set its disposition.",
        input_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "disposition": {"type": "string"},
            },
            "required": ["case_id"],
        },
        read_only=False,
    ),
]

_EVENTS = """metadata.event_timestamp   principal.ip     target.ip        action
2026-08-15T09:14:22Z       10.14.7.22       203.0.113.45     ALLOW
2026-08-15T09:14:58Z       10.14.7.22       203.0.113.45     ALLOW
2026-08-15T09:17:03Z       10.14.7.22       203.0.113.45     BLOCK
2026-08-15T09:22:41Z       10.14.7.22       198.51.100.9     ALLOW

4 events matched (demo data)."""

_ALERTS = """id        rule                          severity  status
AL-4471   Suspicious PowerShell Encoded  HIGH      OPEN
AL-4468   Impossible Travel              MEDIUM    OPEN
AL-4460   Multiple Failed Logons         LOW       OPEN

3 open alerts (demo data)."""


class DemoMcpManager:
    """Stands in for `mcp_manager`, same surface, no network."""

    async def start(self) -> None:
        return None

    async def list_tools(self) -> list[ToolSpec]:
        return DEMO_TOOLS

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        await asyncio.sleep(0.4)  # so tool cards visibly transition to "ok"
        if name == "list_alerts":
            text = _ALERTS
        elif name == "close_case":
            case = arguments.get("case_id", "UNKNOWN")
            text = f"Case {case} closed. (demo — nothing was actually changed)"
        else:
            text = _EVENTS
        return ToolResult(ok=True, text=text, raw={"text": text}, latency_ms=400)

    async def healthy(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return (message.get("content") or "").lower()
    return ""


def _already_ran_tools(messages: list[dict[str, Any]]) -> bool:
    """True once a tool reply exists after the newest user message — i.e. this
    is the second pass of the loop and it's time to answer."""
    for message in reversed(messages):
        if message.get("role") == "user":
            return False
        if message.get("role") == "tool":
            return True
    return False


def _pick_tool(text: str) -> tuple[str, dict[str, Any]] | None:
    if any(word in text for word in ("close", "resolve", "disposition")):
        return "close_case", {"case_id": "AL-4471", "disposition": "true_positive"}
    if "alert" in text or "case" in text:
        return "list_alerts", {"limit": 10}
    if not text.strip():
        return None
    return "search_udm_events", {"query": text[:120], "hours": 24}


_ANSWER = """Here's what the data shows.

**Finding:** `10.14.7.22` made 4 outbound connections to `203.0.113.45` in the \
last 24 hours. One was blocked at 09:17; the rest were allowed.

| Time (UTC) | Destination | Action |
|---|---|---|
| 09:14:22 | 203.0.113.45 | ALLOW |
| 09:14:58 | 203.0.113.45 | ALLOW |
| 09:17:03 | 203.0.113.45 | BLOCK |
| 09:22:41 | 198.51.100.9 | ALLOW |

The pattern is consistent with periodic beaconing, though four events over a \
24-hour window is thin evidence on its own. I'd widen the window to 7 days \
before drawing a conclusion.

> This is **demo mode** — the LLM and the SecOps MCP server are simulated, and \
every value above is invented. Configure `LLM_PROXY_URL` and `MCP_SERVER_URL` \
to talk to the real thing.
"""


async def stream_completion(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, on_token: Any
):
    """Drop-in replacement for `llm.stream_completion`.

    First pass asks for a tool, second pass streams prose — the same two-phase
    shape a real model produces, so the tool cards and the approval gate both
    get exercised.
    """
    from app.services.llm import StreamedTurn

    turn = StreamedTurn()
    # A realistic-looking id so the usage footer renders as it will in prod.
    turn.model = DEMO_MODEL
    text = _last_user_text(messages)

    if not _already_ran_tools(messages):
        chosen = _pick_tool(text)
        if chosen is not None:
            name, arguments = chosen
            await asyncio.sleep(0.3)
            turn.tool_calls = [
                {
                    "id": "demo_call_1",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ]
            turn.usage = {
                "prompt_tokens": 620,
                "completion_tokens": 48,
                "total_tokens": 668,
            }
            return turn

    # Stream word by word with a small delay, so streaming is actually visible
    # rather than arriving as one instant blob.
    for word in _ANSWER.split(" "):
        chunk = word + " "
        turn.content += chunk
        await on_token(chunk)
        await asyncio.sleep(0.012)

    turn.finish_reason = "stop"
    turn.usage = {
        "prompt_tokens": 1180,
        "completion_tokens": 214,
        "total_tokens": 1394,
    }
    return turn


async def generate_title(first_user_message: str, _first_reply: str) -> str:
    words = first_user_message.split()[:6]
    return " ".join(words).strip("?.,")[:60] or "Demo investigation"


def install() -> None:
    """Swap the two external services out, in place.

    `from x import y` binds the name in the *importing* module, so patching
    `mcp_manager` in one place leaves every other importer still pointing at
    the real client. Every binding site has to be rebound.
    """
    from app.api import conversations
    from app.services import agent_loop, llm
    from app.services import mcp_manager as mcp_module

    llm.stream_completion = stream_completion  # type: ignore[assignment]
    llm.generate_title = generate_title  # type: ignore[assignment]

    fake = DemoMcpManager()
    mcp_module.mcp_manager = fake  # type: ignore[assignment]
    agent_loop.mcp_manager = fake  # type: ignore[assignment]
    conversations.mcp_manager = fake  # type: ignore[assignment]

    logger.warning(
        "DEMO MODE: the LLM and the SecOps MCP server are simulated. "
        "Every answer is invented. Do not use against real data."
    )
