"""The LLM <-> MCP orchestration loop.

Deliberately free of FastAPI and HTTP concerns: it takes a context, yields
plain event dicts, and can be driven by a test harness with a fake LLM and a
fake MCP manager.

Invariant that keeps the proxy happy: every `tool_call` emitted in an assistant
message must have a matching `tool` message before the next completion. Writes
awaiting approval therefore *park* the turn rather than skipping ahead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AnonSession, Conversation, ToolInvocation
from app.services import llm, repository as repo
from app.services.mcp_manager import McpUnavailable, ToolSpec, mcp_manager
from app.services.tool_catalog import tool_catalog
from app.services.tool_policy import requires_approval

logger = logging.getLogger(__name__)
settings = get_settings()

MAX_PARALLEL_TOOLS = 4
RESULT_PREVIEW_CHARS = 400


@dataclass
class TurnContext:
    db: AsyncSession
    conversation: Conversation
    session: AnonSession


def _event(kind: str, **payload: Any) -> dict[str, Any]:
    return {"type": kind, **payload}


def _invocation_dto(inv: ToolInvocation) -> dict[str, Any]:
    return {
        "id": str(inv.id),
        "tool_call_id": inv.tool_call_id,
        "tool_name": inv.tool_name,
        "arguments": inv.arguments,
        "status": inv.status,
        "is_write": inv.is_write,
        "latency_ms": inv.latency_ms,
        "error": inv.error,
        "result_preview": (inv.result or {}).get("text", "")[:RESULT_PREVIEW_CHARS]
        if inv.result
        else None,
    }


async def run_turn(ctx: TurnContext, user_text: str) -> AsyncIterator[dict[str, Any]]:
    """Persist the analyst's message, then drive the loop."""
    message = await repo.add_message(ctx.db, ctx.conversation.id, "user", content=user_text)
    await repo.touch_conversation(ctx.db, ctx.conversation)
    await ctx.db.commit()
    yield _event("user_message", id=str(message.id), seq=message.seq)

    async for event in _drive(ctx):
        yield event


async def resume_turn(
    ctx: TurnContext, decisions: dict[uuid.UUID, str]
) -> AsyncIterator[dict[str, Any]]:
    """Apply approve/deny decisions to parked invocations, then continue."""
    pending = await repo.pending_invocations(ctx.db, ctx.conversation.id)
    if not pending:
        yield _event("error", message="No tool calls are awaiting approval.")
        return

    specs = {s.name: s for s in await _load_specs(ctx)}
    approved: list[tuple[ToolInvocation, ToolSpec | None]] = []

    for inv in pending:
        decision = decisions.get(inv.id, "deny")
        if decision == "approve":
            inv.status = "running"
            approved.append((inv, specs.get(inv.tool_name)))
        else:
            inv.status = "denied"
            inv.completed_at = datetime.now(UTC)
            await repo.add_message(
                ctx.db,
                ctx.conversation.id,
                "tool",
                tool_call_id=inv.tool_call_id,
                content=(
                    "The analyst declined this action. Do not retry it. "
                    "Continue with read-only analysis or ask what they would prefer."
                ),
            )
            await repo.audit(
                ctx.db,
                "tool.denied",
                session_id=ctx.session.id,
                detail={"tool": inv.tool_name, "invocation_id": str(inv.id)},
            )
            yield _event("tool_result", invocation=_invocation_dto(inv))
    await ctx.db.commit()

    async for event in _execute_batch(ctx, approved):
        yield event

    async for event in _drive(ctx):
        yield event


async def _load_specs(ctx: TurnContext) -> list[ToolSpec]:
    """Cached definitions. A turn does not need a live MCP session to *plan*
    tool calls — only to execute them."""
    catalog = await tool_catalog.get(ctx.db)
    return catalog.tools


async def _drive(ctx: TurnContext) -> AsyncIterator[dict[str, Any]]:
    degraded = False
    try:
        specs = await _load_specs(ctx)
    except McpUnavailable as exc:
        # Don't kill the turn. A tool-less assistant is still useful, and
        # refusing outright makes it impossible to test the LLM leg on its own
        # while MCP is still being wired up. The model is told it has no tools
        # so it says so rather than inventing SecOps data.
        logger.error("MCP unavailable, continuing without tools: %s", exc)
        specs = []
        degraded = True
        yield _event(
            "warning",
            message=(
                f"The SecOps MCP server is unreachable ({exc}). Answering without "
                "tools — nothing below is grounded in live SecOps data."
            ),
        )

    by_name = {s.name: s for s in specs}
    schemas = [s.to_openai() for s in specs]

    for _iteration in range(settings.llm_max_tool_iterations):
        history = await repo.load_history(ctx.db, ctx.conversation.id)
        wire = await llm.build_messages(repo.to_wire(history), settings.llm_max_context_messages)
        if degraded:
            wire.append(
                {
                    "role": "system",
                    "content": (
                        "The SecOps platform is currently unreachable and you have NO "
                        "tools available. Say so plainly and do not invent hostnames, "
                        "IPs, alert counts, case IDs or timestamps. You may still "
                        "answer general security questions from your own knowledge."
                    ),
                }
            )

        assistant = await repo.add_message(
            ctx.db,
            ctx.conversation.id,
            "assistant",
            content="",
            status="streaming",
            model=settings.llm_model_name,
        )
        await ctx.db.commit()
        yield _event("message_start", id=str(assistant.id), seq=assistant.seq)

        # Tokens are pushed onto a queue by the LLM callback so this generator
        # can forward them while the completion is still running.
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def on_token(text: str) -> None:
            await queue.put(text)

        task = asyncio.create_task(llm.stream_completion(wire, schemas, on_token))
        task.add_done_callback(lambda _: queue.put_nowait(None))

        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield _event("token", text=chunk)
            turn = await task
        except Exception as exc:  # noqa: BLE001
            logger.exception("completion failed")
            explanation = llm.describe_error(exc)
            assistant.status = "error"
            # Short marker only. The full explanation rides on the error event;
            # writing it into content too meant the analyst read the same wall
            # of provider JSON twice.
            assistant.content = None
            await ctx.db.commit()
            yield _event("error", message=explanation)
            return
        finally:
            # Also runs on GeneratorExit when the browser disconnects — without
            # this the completion keeps streaming into a queue nobody reads.
            if not task.done():
                task.cancel()

        assistant.content = turn.content or None
        assistant.tool_calls = turn.tool_calls or None
        assistant.status = "complete"
        # Record what the gateway actually served, not the alias we asked for.
        assistant.model = turn.model or settings.llm_model_name or None
        assistant.token_usage = turn.usage or llm.estimate_usage(
            wire, turn.content, turn.tool_calls
        )
        await repo.touch_conversation(ctx.db, ctx.conversation)
        await ctx.db.commit()

        if turn.finish_reason == "length":
            # An answer cut off mid-sentence looks exactly like a broken
            # stream. Say which one it actually is.
            logger.warning("completion hit the output token limit")
            yield _event(
                "warning",
                message=(
                    "The model stopped at its output token limit, so this answer is "
                    "incomplete. Raise the proxy's max_tokens for this model (or "
                    "LLM_MAX_OUTPUT_TOKENS if you set it), or ask for a shorter answer."
                ),
            )

        if not turn.tool_calls:
            async for event in _maybe_title(ctx, turn.content):
                yield event
            yield _event(
                "done",
                id=str(assistant.id),
                model=assistant.model,
                usage=assistant.token_usage,
            )
            return

        # --- turn the model's requests into audited invocation rows ---------
        auto: list[tuple[ToolInvocation, ToolSpec | None]] = []
        parked: list[ToolInvocation] = []

        for call in turn.tool_calls:
            name = call["function"]["name"]
            spec = by_name.get(name)
            arguments, parse_error = _parse_args(call["function"]["arguments"])

            inv = ToolInvocation(
                conversation_id=ctx.conversation.id,
                message_id=assistant.id,
                tool_call_id=call["id"] or f"call_{uuid.uuid4().hex[:12]}",
                tool_name=name,
                arguments=arguments,
                is_write=not (spec.read_only if spec else False),
                session_id=ctx.session.id,
                status="running",
            )
            ctx.db.add(inv)
            await ctx.db.flush()

            if spec is None:
                await _fail(ctx, inv, f"Unknown tool '{name}'. It is not exposed to you.")
                yield _event("tool_result", invocation=_invocation_dto(inv))
                continue
            if parse_error:
                await _fail(ctx, inv, f"Arguments were not valid JSON: {parse_error}")
                yield _event("tool_result", invocation=_invocation_dto(inv))
                continue

            if requires_approval(spec.read_only):
                inv.status = "pending_approval"
                parked.append(inv)
                yield _event("tool_call", invocation=_invocation_dto(inv), awaiting_approval=True)
            else:
                auto.append((inv, spec))
                yield _event("tool_call", invocation=_invocation_dto(inv), awaiting_approval=False)

        await ctx.db.commit()

        async for event in _execute_batch(ctx, auto):
            yield event

        if parked:
            await repo.audit(
                ctx.db,
                "tool.approval_requested",
                session_id=ctx.session.id,
                detail={"tools": [p.tool_name for p in parked]},
            )
            await ctx.db.commit()
            yield _event(
                "approval_required", invocations=[_invocation_dto(p) for p in parked]
            )
            return  # the turn resumes via POST /approvals

    yield _event(
        "error",
        message=(
            f"Stopped after {settings.llm_max_tool_iterations} tool rounds without a final "
            "answer. Try narrowing the question."
        ),
    )


async def _execute_batch(
    ctx: TurnContext, batch: list[tuple[ToolInvocation, ToolSpec | None]]
) -> AsyncIterator[dict[str, Any]]:
    if not batch:
        return

    semaphore = asyncio.Semaphore(MAX_PARALLEL_TOOLS)

    async def run(inv: ToolInvocation) -> tuple[ToolInvocation, Any, str | None]:
        async with semaphore:
            try:
                return inv, await mcp_manager.call_tool(inv.tool_name, inv.arguments), None
            except TimeoutError as exc:
                return inv, None, f"timeout: {exc}"
            except Exception as exc:  # noqa: BLE001
                logger.exception("tool call failed: %s", inv.tool_name)
                return inv, None, str(exc)

    for coro in asyncio.as_completed([run(inv) for inv, _ in batch]):
        inv, result, error = await coro
        inv.completed_at = datetime.now(UTC)

        if error is not None:
            inv.status = "timeout" if error.startswith("timeout") else "failed"
            inv.error = error
            content = f"Tool execution failed: {error}"
        else:
            inv.status = "succeeded" if result.ok else "failed"
            inv.result = result.raw
            inv.latency_ms = result.latency_ms
            content = _tool_content_for_model(result.text)
            if not result.ok:
                inv.error = result.text[:2000]

        await repo.add_message(
            ctx.db, ctx.conversation.id, "tool", tool_call_id=inv.tool_call_id, content=content
        )
        await repo.audit(
            ctx.db,
            "tool.executed",
            session_id=ctx.session.id,
            detail={
                "tool": inv.tool_name,
                "status": inv.status,
                "is_write": inv.is_write,
                "latency_ms": inv.latency_ms,
            },
        )
        await ctx.db.commit()
        yield _event("tool_result", invocation=_invocation_dto(inv))


def _tool_content_for_model(text: str) -> str:
    """What the model actually receives back from an MCP call.

    Full fidelity by default (`TOOL_RESULT_MAX_CHARS=0`). The model is the one
    deciding which tools to call and what the results mean, and it cannot
    reason about rows it was never shown — a silently clipped result is how you
    get confident answers drawn from half the data.

    Operators who need a hard ceiling can set one, and when it bites the model
    is told plainly, so it re-queries with a narrower filter instead of
    treating a partial page as the whole answer.
    """
    limit = settings.tool_result_max_chars
    if limit <= 0 or len(text) <= limit:
        return text
    dropped = len(text) - limit
    return (
        text[:limit]
        + f"\n\n[TRUNCATED: {dropped} more characters were returned by the tool but "
        "withheld to protect the context window. This is a partial result — do not "
        "treat it as complete. Re-run the query with a narrower time range, a tighter "
        "filter, or a smaller page size to see the rest.]"
    )


async def _fail(ctx: TurnContext, inv: ToolInvocation, reason: str) -> None:
    inv.status = "failed"
    inv.error = reason
    inv.completed_at = datetime.now(UTC)
    await repo.add_message(
        ctx.db, ctx.conversation.id, "tool", tool_call_id=inv.tool_call_id, content=reason
    )
    await ctx.db.commit()


def _parse_args(raw: str) -> tuple[dict[str, Any], str | None]:
    if not raw or not raw.strip():
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"_raw": raw[:2000]}, str(exc)
    if not isinstance(parsed, dict):
        return {"_raw": parsed}, "expected a JSON object"
    return parsed, None


async def _maybe_title(ctx: TurnContext, reply: str) -> AsyncIterator[dict[str, Any]]:
    if ctx.conversation.title != "New conversation":
        return
    history = await repo.load_history(ctx.db, ctx.conversation.id)
    first_user = next((m.content for m in history if m.role == "user" and m.content), "")
    if not first_user:
        return
    title = await llm.generate_title(first_user, reply or "")
    ctx.conversation.title = title
    # Push the new title to the sidebar in every tab this visitor has open.
    await repo.notify_conversation(ctx.db, ctx.conversation, "conversation_updated")
    await ctx.db.commit()
    yield _event("title", title=title)
