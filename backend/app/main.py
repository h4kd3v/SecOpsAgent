from __future__ import annotations

import logging
import sys
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api import chat, conversations, events, health, session
from app.config import get_settings
from app.db.session import SessionMaker, engine
from app.services.events import event_bus
from app.services.google_auth import token_manager
from app.services.mcp_manager import mcp_manager

settings = get_settings()


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            '{"ts":"%(asctime)s","level":"%(levelname)s",'
            '"logger":"%(name)s","msg":"%(message)s"}'
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())


async def sweep_empty_conversations() -> None:
    """Clear conversations that never received a message.

    The UI no longer creates a conversation until the first message is sent,
    so this only ever removes debris — earlier empty rows, or one left behind
    by a first turn that failed between create and send.
    """
    if settings.empty_conversation_ttl_hours <= 0:
        return
    from app.services import repository as repo

    async with SessionMaker() as db:
        try:
            removed = await repo.delete_empty_conversations(
                db, settings.empty_conversation_ttl_hours
            )
        except Exception:  # noqa: BLE001 - never block startup on housekeeping
            logging.getLogger(__name__).warning("empty-conversation sweep failed", exc_info=True)
            return
    if removed:
        logging.getLogger(__name__).info("removed %d empty conversations", removed)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log = logging.getLogger(__name__)

    await event_bus.start()
    await sweep_empty_conversations()

    if settings.demo_mode:
        from app.services import demo

        demo.install()
    else:
        missing = settings.missing_required()
        if missing:
            log.error(
                "missing required configuration: %s - set these in .env, or run "
                "with DEMO_MODE=true to explore the UI without credentials",
                ", ".join(missing),
            )

        # Fail loudly at boot if sa.json is missing or malformed, rather than
        # on the first analyst's first question.
        if not await token_manager.warm():
            log.error("starting without a valid Google token; MCP calls will fail")

        await mcp_manager.start()

    yield

    if not settings.demo_mode:
        await mcp_manager.aclose()
    await event_bus.aclose()
    await engine.dispose()


app = FastAPI(
    title="SecOps Agent",
    version="0.1.0",
    lifespan=lifespan,
    # The API is same-origin behind nginx; no need to publish docs in prod.
    docs_url="/api/docs" if settings.app_env == "dev" else None,
    openapi_url="/api/openapi.json" if settings.app_env == "dev" else None,
)

if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


app.include_router(health.router)
app.include_router(session.router)
app.include_router(conversations.router)
app.include_router(chat.router)
app.include_router(events.router)
