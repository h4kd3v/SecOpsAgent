from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip, current_session
from app.config import get_settings
from app.core.security import (
    COOKIE_NAME,
    create_session_token,
    generate_label,
    read_session_token,
)
from app.db.models import AnonSession
from app.db.session import get_db
from app.schemas import SessionOut

router = APIRouter(prefix="/api/session", tags=["session"])
settings = get_settings()


@router.post("", response_model=SessionOut)
async def open_session(
    request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> SessionOut:
    """Get-or-create the anonymous session. The frontend calls this on load.

    No registration, no password: the cookie *is* the identity, and it exists
    only so a visitor's sidebar shows their own conversations rather than
    everybody's.
    """
    token = request.cookies.get(COOKIE_NAME)
    existing_id = read_session_token(token) if token else None
    session = await db.get(AnonSession, existing_id) if existing_id else None

    if session is None:
        session = AnonSession(
            label=generate_label(),
            user_agent=(request.headers.get("user-agent") or "")[:400] or None,
            source_ip=client_ip(request),
        )
        db.add(session)
        await db.flush()
    else:
        session.last_seen_at = func.now()

    await db.commit()
    await db.refresh(session)

    response.set_cookie(
        COOKIE_NAME,
        create_session_token(session.id),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.anon_session_days * 86400,
        path="/",
    )
    return SessionOut(id=session.id, label=session.label, created_at=session.created_at)


@router.get("", response_model=SessionOut)
async def whoami(
    session: AnonSession = Depends(current_session), db: AsyncSession = Depends(get_db)
) -> SessionOut:
    await db.execute(
        update(AnonSession).where(AnonSession.id == session.id).values(last_seen_at=func.now())
    )
    await db.commit()
    return SessionOut(id=session.id, label=session.label, created_at=session.created_at)
