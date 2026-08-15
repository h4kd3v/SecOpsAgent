"""Connectivity check for the two external services.

    docker compose exec backend python -m app.diagnose

Answers "is it me or is it them" without going through the UI: what config is
loaded, whether the LLM proxy answers, and whether the MCP server completes a
handshake and returns a tool list. Prints the root cause when it doesn't.
"""

from __future__ import annotations

import asyncio
import sys

from app.config import get_settings

settings = get_settings()

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def bad(msg: str) -> None:
    print(f"  {RED}✗{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}!{RESET} {msg}")


def redact(value: str) -> str:
    if not value:
        return f"{DIM}(unset){RESET}"
    return f"{value[:6]}…{value[-4:]}" if len(value) > 12 else "…"


def section(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


async def check_llm() -> bool:
    section("LLM proxy")
    print(f"  url        {settings.llm_proxy_url or DIM + '(unset)' + RESET}")
    print(f"  key        {redact(settings.llm_api_key)}")
    print(f"  model      {settings.llm_model_name or DIM + '(unset)' + RESET}")

    if not settings.llm_proxy_url or not settings.llm_api_key:
        bad("not configured")
        return False

    from app.services import llm

    try:
        models = await llm.get_client().models.list()
        names = [m.id for m in models.data][:5]
        ok(f"reachable, {len(models.data)} models (e.g. {', '.join(names)})")
    except Exception as exc:  # noqa: BLE001
        # Listing models is often blocked even when completions work fine.
        warn(f"models.list failed: {type(exc).__name__}: {exc}")
        warn("this alone is not fatal - trying a real completion instead")

    try:
        result = await llm.get_client().chat.completions.create(
            model=settings.llm_model_name,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=5,
        )
        ok(f"completion works, model served: {result.model}")
        if result.usage:
            ok(f"usage reported: {result.usage.total_tokens} tokens")
        else:
            warn("no usage reported; the UI will show an estimate")
        return True
    except Exception as exc:  # noqa: BLE001
        bad(f"completion failed: {type(exc).__name__}: {exc}")
        return False


async def check_mcp() -> bool:
    section("SecOps MCP server")
    print(f"  url        {settings.mcp_server_url or DIM + '(unset)' + RESET}")
    print(f"  transport  {settings.mcp_transport}")
    print(f"  auth mode  {settings.mcp_auth_mode}")

    headers = settings.secops_headers
    for name in ("Project-Id", "Region", "Customer-Id"):
        if name in headers:
            print(f"  {name:<11}{headers[name]}")
        else:
            print(f"  {name:<11}{DIM}(unset - not sent){RESET}")

    if not settings.mcp_server_url:
        bad("MCP_SERVER_URL is unset")
        return False

    if settings.mcp_auth_mode == "service_account":
        from app.services.google_auth import token_manager

        if await token_manager.warm():
            ok(f"Google token minted, valid for {token_manager.seconds_remaining}s")
        else:
            bad(f"could not mint a token from {settings.google_sa_file}")
            warn("try MCP_AUTH_MODE=static_token (with MCP_STATIC_TOKEN), or =none")
            return False
    elif settings.mcp_auth_mode == "static_token":
        if settings.mcp_static_token:
            ok(f"static bearer {redact(settings.mcp_static_token)}")
        else:
            bad("MCP_AUTH_MODE=static_token but MCP_STATIC_TOKEN is empty")
            return False
    else:
        warn("no Authorization header will be sent")

    from app.services.mcp_manager import mcp_manager

    try:
        tools = await mcp_manager.list_tools()
    except Exception as exc:  # noqa: BLE001
        bad(f"handshake or tools/list failed: {mcp_manager.last_error or exc}")
        print()
        warn("common causes:")
        warn("  401/403       - the server does want a bearer; set MCP_AUTH_MODE")
        warn("  400           - missing Project-Id / Region / Customer-Id headers")
        warn("  404           - wrong path; streamable HTTP usually ends in /mcp")
        warn("  timeout/DNS   - not reachable from inside the container")
        warn("  session/proto - try MCP_TRANSPORT=sse")
        return False
    finally:
        await mcp_manager.aclose()

    ok(f"connected, {len(tools)} tools")
    print()
    writes = 0
    for tool in tools:
        kind = f"{GREEN}read {RESET}" if tool.read_only else f"{YELLOW}WRITE{RESET}"
        writes += 0 if tool.read_only else 1
        print(f"    {kind}  {tool.name}")
        if tool.description and tool.description != tool.name:
            print(f"           {DIM}{tool.description[:96]}{RESET}")
    print()
    ok(f"{len(tools) - writes} read-only, {writes} requiring approval")
    warn("anything classified WRITE that is really read-only: widen "
         "TOOL_READONLY_PATTERNS (unannotated tools fail closed)")
    return True


async def main() -> int:
    print("SecOps Agent - connectivity check")
    if settings.demo_mode:
        warn("DEMO_MODE is on; the checks below hit the real services anyway")

    missing = settings.missing_required()
    if missing:
        section("Configuration")
        bad(f"missing required: {', '.join(missing)}")
    recommended = settings.missing_recommended()
    if recommended:
        section("Configuration")
        warn(f"unset (SecOps rejects calls without these): {', '.join(recommended)}")

    llm_ok = await check_llm()
    mcp_ok = await check_mcp()

    section("Result")
    print(f"  LLM proxy   {'OK' if llm_ok else 'FAILED'}")
    print(f"  MCP server  {'OK' if mcp_ok else 'FAILED'}")
    return 0 if (llm_ok and mcp_ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
