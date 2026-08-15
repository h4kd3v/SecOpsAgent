"""Anonymous session cookies.

There is no registration and no password. A visitor is identified only by a
signed cookie holding an opaque session id, which is what scopes their sidebar
to their own conversations. The signature stops one browser from claiming
another's history by editing a cookie value.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt

from app.config import get_settings

settings = get_settings()

ALGORITHM = "HS256"
COOKIE_NAME = "secops_visitor"


# Used only in demo mode and in dev when no key is configured.
#
# Deliberately a fixed constant rather than a per-process random value: a
# random key would differ between uvicorn workers, so a cookie signed by one
# worker would be rejected by the next request that landed on another.
_INSECURE_DEV_SECRET = "insecure-local-development-key-do-not-use-in-production"
_warned = False


def _secret() -> str:
    global _warned
    if settings.secret_key:
        return settings.secret_key
    if settings.demo_mode or settings.app_env == "dev":
        if not _warned:
            _warned = True
            logging.getLogger(__name__).warning(
                "no SECRET_KEY set - signing session cookies with a public "
                "development key. Anyone can forge a session. Set SECRET_KEY "
                "(openssl rand -hex 32) before exposing this to anyone."
            )
        return _INSECURE_DEV_SECRET
    # In prod a missing key must not silently produce forgeable cookies — any
    # browser could then claim any other visitor's chat history.
    raise RuntimeError("SECRET_KEY is required to sign session cookies")


def create_session_token(session_id: uuid.UUID) -> str:
    now = datetime.now(UTC)
    payload = {
        "sid": str(session_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=settings.anon_session_days)).timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM)


def read_session_token(token: str) -> uuid.UUID | None:
    try:
        payload = jwt.decode(token, _secret(), algorithms=[ALGORITHM])
        return uuid.UUID(payload["sid"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


def generate_label() -> str:
    """A short stable handle, so audit rows read 'Analyst 4f2a' rather than a
    bare UUID. Not an identity claim — just something a human can refer to."""
    return f"Analyst {secrets.token_hex(2)}"
