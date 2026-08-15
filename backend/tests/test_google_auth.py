"""The token lifecycle: one credential file, twenty analysts, a 60-minute TTL.

These are the failure modes that matter — a stampede of refreshes when the
token expires, and a token being used past its expiry.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.config import get_settings
from app.services.google_auth import GoogleTokenManager

settings = get_settings()


class FakeManager(GoogleTokenManager):
    """Replaces only the blocking Google call, keeping the real caching,
    locking and expiry logic under test."""

    def __init__(self, lifetime: float = 3600.0) -> None:
        super().__init__()
        self.lifetime = lifetime
        self.refreshes = 0

    def _refresh_blocking(self) -> tuple[str, float]:
        self.refreshes += 1
        time.sleep(0.02)  # a real token exchange is a network round trip
        return f"token-{self.refreshes}", time.time() + self.lifetime


async def test_token_is_cached_between_calls():
    manager = FakeManager()
    first = await manager.token()
    second = await manager.token()

    assert first == second == "token-1"
    assert manager.refreshes == 1


async def test_concurrent_callers_trigger_exactly_one_refresh():
    """Twenty analysts hitting a cold cache at once must not fan out into
    twenty token exchanges against Google."""
    manager = FakeManager()

    tokens = await asyncio.gather(*(manager.token() for _ in range(20)))

    assert manager.refreshes == 1
    assert set(tokens) == {"token-1"}


async def test_token_refreshes_before_it_expires():
    # Lifetime shorter than the configured skew: every call is inside the
    # refresh window, so a stale token can never be handed out.
    manager = FakeManager(lifetime=settings.google_token_refresh_skew - 1)

    await manager.token()
    second = await manager.token()

    assert manager.refreshes == 2
    assert second == "token-2"


async def test_seconds_remaining_reports_the_live_ttl():
    manager = FakeManager(lifetime=3600)
    assert manager.seconds_remaining == 0  # nothing fetched yet

    await manager.token()

    assert 3500 < manager.seconds_remaining <= 3600


async def test_warm_reports_failure_instead_of_raising():
    class Broken(GoogleTokenManager):
        def _refresh_blocking(self):
            raise RuntimeError("sa.json is not valid JSON")

    # Startup must log and continue, not crash the whole app.
    assert await Broken().warm() is False


def test_secops_headers_are_present_on_every_call():
    """The MCP server rejects any request missing these three; Authorization
    is added separately by the auth flow because it rotates."""
    headers = settings.secops_headers

    assert set(headers) == {"Project-Id", "Region", "Customer-Id"}
    assert all(headers.values())


@pytest.mark.asyncio
async def test_auth_flow_stamps_a_fresh_bearer():
    from app.services.google_auth import GoogleBearerAuth

    manager = FakeManager()
    auth = GoogleBearerAuth(manager)

    class Req:
        headers: dict[str, str] = {}

    request = Req()
    flow = auth.async_auth_flow(request)
    await flow.__anext__()

    assert request.headers["Authorization"] == "Bearer token-1"
