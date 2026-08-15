"""Long-lived client session against the remote SecOps MCP server.

One shared service-account identity serves every analyst, so there is exactly
one session (plus whatever replaces it after a recycle or a failure).

Why an actor task instead of `async with streamable_http_client(...)` inside
the request handler:

1.  Handshake + `tools/list` on every chat message is pure latency tax.
2.  The MCP SDK is anyio-based. Entering its context in one task and letting
    that task get cancelled (which is exactly what happens when a browser
    disconnects mid-SSE) raises "Attempted to exit cancel scope in a different
    task". Pinning the session to one owning task and talking to it over a
    queue removes that entire class of bug.

Every request carries the four headers SecOps requires: `Authorization`
(injected per-request by `GoogleBearerAuth`, because it rotates hourly),
plus the static `Project-Id`, `Region` and `Customer-Id`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.shared._httpx_utils import create_mcp_http_client

try:  # MCP SDK >= 2.0
    from mcp.client.streamable_http import streamable_http_client
except ImportError:  # MCP SDK 1.x
    from mcp.client.streamable_http import (  # type: ignore[no-redef]
        streamablehttp_client as streamable_http_client,
    )

from app.config import get_settings
from app.services.google_auth import build_mcp_auth

logger = logging.getLogger(__name__)
settings = get_settings()


def sdk_attr(obj: Any, *names: str, default: Any = None) -> Any:
    """Read the first attribute that exists.

    MCP SDK 2.0 renamed its model fields to snake_case (`input_schema`,
    `is_error`, `read_only_hint`); 1.x used camelCase. Reading through this
    keeps the app working against either, and against servers that omit
    optional fields entirely.
    """
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


class McpUnavailable(RuntimeError):
    pass


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    # The server's own annotations, kept verbatim so a cached row can be
    # re-classified when the operator changes TOOL_READONLY_PATTERNS.
    read_only_hint: Any = None
    destructive_hint: Any = None

    def to_cache(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "read_only_hint": self.read_only_hint,
            "destructive_hint": self.destructive_hint,
        }

    @classmethod
    def from_cache(cls, row: dict[str, Any]) -> "ToolSpec":
        from app.services.tool_policy import classify

        name = row.get("name", "")
        read_only_hint = row.get("read_only_hint")
        destructive_hint = row.get("destructive_hint")
        return cls(
            name=name,
            description=row.get("description") or name,
            input_schema=row.get("input_schema") or {},
            read_only=classify(name, read_only_hint, destructive_hint),
            read_only_hint=read_only_hint,
            destructive_hint=destructive_hint,
        )

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description[: settings.tool_description_max_chars],
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
        }


@dataclass(slots=True)
class ToolResult:
    ok: bool
    text: str
    raw: dict[str, Any]
    latency_ms: int


@dataclass
class _Command:
    kind: str  # "call" | "close"
    future: asyncio.Future
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)


class _SessionActor:
    def __init__(self) -> None:
        self.tools: list[ToolSpec] = []
        self.started_at = time.monotonic()
        self._queue: asyncio.Queue[_Command] = asyncio.Queue()
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._task: asyncio.Task | None = None
        self._dead = False

    @property
    def dead(self) -> bool:
        return self._dead or (self._task is not None and self._task.done())

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.started_at > settings.mcp_session_max_age

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="mcp-session")
        await self._ready  # raises if the handshake failed

    def _transport(self):
        auth = build_mcp_auth()
        headers = settings.secops_headers
        logger.info(
            "connecting to MCP",
            extra={
                "url": settings.mcp_server_url,
                "transport": settings.mcp_transport,
                "auth_mode": settings.mcp_auth_mode,
                "headers": sorted(headers),
            },
        )
        if settings.mcp_transport == "sse":
            return sse_client(settings.mcp_server_url, headers=headers, auth=auth)
        # SDK 2.0 takes headers and auth via a pre-built httpx client.
        return streamable_http_client(
            settings.mcp_server_url,
            http_client=create_mcp_http_client(headers=headers, auth=auth),
        )

    async def _run(self) -> None:
        try:
            async with self._transport() as streams:
                read_stream, write_stream = streams[0], streams[1]
                async with ClientSession(read_stream, write_stream) as session:
                    await asyncio.wait_for(session.initialize(), timeout=30)
                    self.tools = await self._load_tools(session)
                    logger.info("mcp session ready", extra={"tools": len(self.tools)})
                    if not self._ready.done():
                        self._ready.set_result(None)
                    await self._serve(session)
        except Exception as exc:  # noqa: BLE001 - surfaced to callers below
            # Flatten here, not at the call site: wrapping in McpUnavailable
            # discards the chain, and "unhandled errors in a TaskGroup
            # (1 sub-exception)" tells nobody what to fix.
            detail = _root_cause(exc)
            logger.error("mcp session failed: %s", detail, exc_info=True)
            self._dead = True
            if not self._ready.done():
                self._ready.set_exception(McpUnavailable(detail))
            self._drain(exc)
        finally:
            self._dead = True

    async def _load_tools(self, session: ClientSession) -> list[ToolSpec]:
        from app.services.tool_policy import classify_tool, extract_hints

        listed = await session.list_tools()
        allowed = settings.allowed_tools
        specs: list[ToolSpec] = []
        skipped = 0
        for tool in listed.tools:
            if tool.name in settings.denied_tools:
                skipped += 1
                continue
            if allowed and tool.name not in allowed:
                skipped += 1
                continue
            read_only_hint, destructive_hint = extract_hints(tool)
            specs.append(
                ToolSpec(
                    name=tool.name,
                    description=tool.description or tool.name,
                    input_schema=sdk_attr(tool, "input_schema", "inputSchema") or {},
                    read_only=classify_tool(tool),
                    read_only_hint=read_only_hint,
                    destructive_hint=destructive_hint,
                )
            )

        schema_chars = sum(len(str(spec.to_openai())) for spec in specs)
        logger.info(
            "loaded MCP tools",
            extra={
                "exposed": len(specs),
                "filtered_out": skipped,
                "schema_chars": schema_chars,
            },
        )
        if schema_chars > 120_000:
            logger.warning(
                "tool schemas are ~%d tokens and are sent on EVERY completion; "
                "set TOOL_ALLOWLIST to the tools your analysts actually need",
                schema_chars // 4,
            )
        return specs

    async def _serve(self, session: ClientSession) -> None:
        while True:
            cmd = await self._queue.get()
            if cmd.kind == "close":
                if not cmd.future.done():
                    cmd.future.set_result(None)
                return
            started = time.perf_counter()
            try:
                raw = await asyncio.wait_for(
                    session.call_tool(cmd.name, cmd.arguments),
                    timeout=settings.mcp_tool_timeout,
                )
                if not cmd.future.done():
                    cmd.future.set_result((raw, _ms(started)))
            except asyncio.TimeoutError:
                if not cmd.future.done():
                    cmd.future.set_exception(
                        TimeoutError(f"{cmd.name} exceeded {settings.mcp_tool_timeout}s")
                    )
            except Exception as exc:  # noqa: BLE001
                # A transport-level error means the session is suspect; tear it
                # down so the next caller gets a fresh one.
                if not cmd.future.done():
                    cmd.future.set_exception(exc)
                if _is_fatal(exc):
                    raise

    def _drain(self, exc: BaseException) -> None:
        detail = _root_cause(exc)
        while not self._queue.empty():
            cmd = self._queue.get_nowait()
            if not cmd.future.done():
                cmd.future.set_exception(McpUnavailable(detail))

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if self.dead:
            raise McpUnavailable("session closed")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._queue.put(_Command("call", fut, name, arguments))
        raw, latency = await fut
        return _to_result(raw, latency)

    async def aclose(self) -> None:
        if self._task is None or self._task.done():
            return
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._queue.put(_Command("close", fut))
        with contextlib.suppress(Exception):
            await asyncio.wait_for(fut, timeout=5)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task


def _root_cause(exc: BaseException, depth: int = 6) -> str:
    """Unwrap TaskGroup/ExceptionGroup nesting to the message a human can act
    on. The MCP SDK wraps transport failures several layers deep, and
    "unhandled errors in a TaskGroup (1 sub-exception)" helps nobody."""
    seen: list[str] = []
    current: BaseException | None = exc
    for _ in range(depth):
        if current is None:
            break
        inner = getattr(current, "exceptions", None)
        if inner:
            current = inner[0]
            continue
        text = f"{type(current).__name__}: {current}".strip()
        if text and text not in seen:
            seen.append(text)
        current = current.__cause__ or current.__context__
    return " <- ".join(seen) or str(exc)


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _is_fatal(exc: BaseException) -> bool:
    return isinstance(exc, (ConnectionError, EOFError, BrokenPipeError, OSError))


def _to_result(raw: Any, latency_ms: int) -> ToolResult:
    """Flatten an MCP CallToolResult into text for the LLM + JSON for the DB."""
    parts: list[str] = []
    for block in getattr(raw, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        elif getattr(block, "type", "") == "image":
            parts.append("[image omitted]")
        else:
            parts.append(str(block))

    structured = sdk_attr(raw, "structured_content", "structuredContent")
    if not parts and structured is not None:
        parts.append(str(structured))

    is_error = bool(sdk_attr(raw, "is_error", "isError", default=False))
    text = "\n".join(parts).strip() or ("(tool returned no content)" if not is_error else "error")
    return ToolResult(
        ok=not is_error,
        text=text,
        raw={"is_error": is_error, "text": text, "structured_content": _jsonable(structured)},
        latency_ms=latency_ms,
    )


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


class McpManager:
    """Owns the shared MCP session and hands out cached tool schemas."""

    def __init__(self) -> None:
        self._actor: _SessionActor | None = None
        self._lock = asyncio.Lock()
        # Kept so /api/tools and /api/health/ready can say what actually broke
        # rather than a generic "unavailable".
        self.last_error: str | None = None

    async def start(self) -> None:
        try:
            await self._session()
        except McpUnavailable:
            # Don't block startup on a flaky MCP server; retry lazily.
            logger.warning("MCP unavailable at startup, will retry on first use")

    async def _session(self) -> _SessionActor:
        async with self._lock:
            actor = self._actor
            if actor is not None and not actor.dead and not actor.expired:
                return actor
            if actor is not None:
                reason = "expired" if actor.expired else "dead"
                logger.info("recycling mcp session (%s)", reason)
                await actor.aclose()
            actor = _SessionActor()
            try:
                await actor.start()
            except McpUnavailable as exc:
                self.last_error = str(exc)  # already flattened by the actor
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = _root_cause(exc)
                raise
            self.last_error = None
            self._actor = actor
            return actor

    async def list_tools(self) -> list[ToolSpec]:
        actor = await self._session()
        return actor.tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        actor = await self._session()
        try:
            return await actor.call(name, arguments)
        except McpUnavailable:
            # One transparent reconnect, then let the caller record the failure
            # as a tool error the model can read and explain.
            actor = await self._session()
            return await actor.call(name, arguments)

    async def healthy(self) -> bool:
        try:
            return bool(await self.list_tools())
        except Exception:  # noqa: BLE001
            return False

    async def aclose(self) -> None:
        if self._actor is not None:
            await self._actor.aclose()
            self._actor = None


mcp_manager = McpManager()
