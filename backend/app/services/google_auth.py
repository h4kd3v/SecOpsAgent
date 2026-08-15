"""Shared Google service-account credential for the SecOps MCP server.

One `sa.json`, one token, all analysts. Google mints tokens with a 60-minute
lifetime, so the interesting part is the refresh lifecycle:

*   **Proactive.** Refreshed `GOOGLE_TOKEN_REFRESH_SKEW` seconds before expiry,
    so no request ever races the boundary.
*   **Single-flight.** Twenty analysts hitting an expired token must produce
    one refresh, not twenty. A lock plus a re-check inside it guarantees that.
*   **Per-request injection.** The MCP transport session outlives any single
    token, so `Authorization` is stamped on each HTTP request by an httpx auth
    flow rather than frozen into the session's headers at connect time.

`google-auth` does the JWT signing and the token exchange; it is synchronous,
so refreshes run in a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# The MCP SDK ships its own httpx fork; the auth class has to come from the
# same module the transport will use.
try:
    import httpx2 as _httpx
except ImportError:  # pragma: no cover - older SDKs
    import httpx as _httpx  # type: ignore[no-redef]


class GoogleTokenError(RuntimeError):
    pass


class GoogleTokenManager:
    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._credentials: Any = None

    # -- credential loading -------------------------------------------------

    def _load_credentials(self) -> Any:
        """Blocking; called from a worker thread."""
        from google.oauth2 import service_account

        if settings.google_token_type == "id_token":
            if not settings.google_id_token_audience:
                raise GoogleTokenError(
                    "GOOGLE_ID_TOKEN_AUDIENCE is required when GOOGLE_TOKEN_TYPE=id_token"
                )
            return service_account.IDTokenCredentials.from_service_account_file(
                settings.google_sa_file,
                target_audience=settings.google_id_token_audience,
            )
        return service_account.Credentials.from_service_account_file(
            settings.google_sa_file, scopes=settings.scope_list
        )

    def _refresh_blocking(self) -> tuple[str, float]:
        from google.auth.transport.requests import Request

        if self._credentials is None:
            self._credentials = self._load_credentials()

        self._credentials.refresh(Request())
        token = self._credentials.token
        if not token:
            raise GoogleTokenError("Google returned an empty token")

        expiry = getattr(self._credentials, "expiry", None)
        if expiry is None:
            # Google's documented default when the library doesn't report one.
            expires_at = time.time() + 3600
        else:
            # google-auth stores a naive UTC datetime.
            from datetime import UTC

            expires_at = expiry.replace(tzinfo=UTC).timestamp()
        return token, expires_at

    # -- public API ---------------------------------------------------------

    def _fresh_enough(self) -> bool:
        return (
            self._token is not None
            and time.time() < self._expires_at - settings.google_token_refresh_skew
        )

    async def token(self) -> str:
        if self._fresh_enough():
            return self._token  # type: ignore[return-value]

        async with self._lock:
            # Someone else may have refreshed while we waited for the lock.
            if self._fresh_enough():
                return self._token  # type: ignore[return-value]

            token, expires_at = await asyncio.to_thread(self._refresh_blocking)
            self._token = token
            self._expires_at = expires_at
            logger.info(
                "google token refreshed",
                extra={
                    "token_type": settings.google_token_type,
                    "valid_for_s": int(expires_at - time.time()),
                },
            )
            return token

    async def warm(self) -> bool:
        """Fetch a token at startup so a bad sa.json fails loudly and early."""
        try:
            await self.token()
            return True
        except Exception:  # noqa: BLE001
            logger.exception("could not obtain a Google token from %s", settings.google_sa_file)
            return False

    @property
    def seconds_remaining(self) -> int:
        return max(0, int(self._expires_at - time.time())) if self._token else 0


token_manager = GoogleTokenManager()


class GoogleBearerAuth(_httpx.Auth):
    """Stamps a currently-valid bearer on every outgoing MCP request.

    Doing it here rather than in the session's static headers is what lets a
    transport session live longer than the 60-minute token inside it.
    """

    requires_request_body = False

    def __init__(self, manager: GoogleTokenManager | None = None) -> None:
        self._manager = manager or token_manager

    async def async_auth_flow(self, request):  # type: ignore[no-untyped-def]
        try:
            token = await self._manager.token()
        except Exception as exc:  # noqa: BLE001
            raise GoogleTokenError(
                f"could not mint a Google token from {settings.google_sa_file}: {exc}. "
                "Set MCP_AUTH_MODE=static_token with MCP_STATIC_TOKEN, or "
                "MCP_AUTH_MODE=none if your MCP server needs no bearer."
            ) from exc
        request.headers["Authorization"] = f"Bearer {token}"
        yield request

    def sync_auth_flow(self, request):  # type: ignore[no-untyped-def]
        raise RuntimeError("SecOps Agent only makes async MCP calls")


class StaticBearerAuth(_httpx.Auth):
    """A bearer supplied verbatim, e.g. `gcloud auth print-access-token`.

    Nothing refreshes it, so it dies with the token — fine for a connectivity
    test, wrong for a deployment.
    """

    requires_request_body = False

    def __init__(self, token: str) -> None:
        self._token = token

    async def async_auth_flow(self, request):  # type: ignore[no-untyped-def]
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request

    def sync_auth_flow(self, request):  # type: ignore[no-untyped-def]
        raise RuntimeError("SecOps Agent only makes async MCP calls")


def build_mcp_auth():
    """Pick the Authorization strategy, or None to send no bearer at all."""
    if settings.mcp_auth_mode == "none":
        logger.warning("MCP_AUTH_MODE=none: requests carry no Authorization header")
        return None
    if settings.mcp_auth_mode == "static_token":
        if not settings.mcp_static_token:
            raise GoogleTokenError("MCP_AUTH_MODE=static_token but MCP_STATIC_TOKEN is empty")
        return StaticBearerAuth(settings.mcp_static_token)
    return GoogleBearerAuth()
