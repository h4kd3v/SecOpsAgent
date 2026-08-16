from __future__ import annotations

import time

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.config import get_settings
from app.db.session import SessionMaker
from app.services import llm
from app.services.events import event_bus
from app.services.google_auth import token_manager
from app.services.mcp_manager import mcp_manager

router = APIRouter(prefix="/api/health", tags=["health"])
settings = get_settings()

# The MCP and LLM probes are outbound network calls. This endpoint is
# unauthenticated by design — load balancers cannot hold a session cookie — so
# without a cache anyone able to reach it could make the backend hammer the
# SecOps server and the LLM gateway at request rate. Dependency state does not
# change meaningfully inside this window.
_PROBE_TTL_SECONDS = 10.0
_probe_cache: tuple[float, dict[str, object]] | None = None


async def _probe_dependencies() -> dict[str, object]:
    global _probe_cache
    now = time.monotonic()
    if _probe_cache is not None and now - _probe_cache[0] < _PROBE_TTL_SECONDS:
        return _probe_cache[1]

    probes: dict[str, object] = {
        "mcp": await mcp_manager.healthy(),
        "llm_proxy": await llm.ping(),
        # Surfaces token rotation on a dashboard: this should sawtooth between
        # ~3600 and the configured refresh skew, never sit at 0.
        "google_token_seconds_remaining": token_manager.seconds_remaining,
    }
    _probe_cache = (now, probes)
    return probes


@router.get("/live")
async def live() -> dict[str, str]:
    """Process is up. Used by the container healthcheck — deliberately does
    not touch Postgres or MCP, so a flaky dependency can't restart-loop us."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(response: Response) -> dict[str, object]:
    """Dependency check for load balancers and on-call dashboards."""
    checks: dict[str, object] = {}

    try:
        async with SessionMaker() as db:
            await db.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception as exc:  # noqa: BLE001
        checks["database"] = False
        checks["database_error"] = str(exc)[:200]

    checks["event_bus"] = event_bus.connected

    if settings.demo_mode:
        checks["demo_mode"] = True
        checks["mcp"] = "simulated"
        checks["llm_proxy"] = "simulated"
    else:
        checks.update(await _probe_dependencies())
        missing = settings.missing_required()
        if missing:
            checks["missing_config"] = missing

    healthy = bool(checks["database"])  # MCP/LLM degrade the app, the DB kills it
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "degraded", "checks": checks}
