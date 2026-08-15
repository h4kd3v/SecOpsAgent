"""Auth strategy selection for the MCP transport.

The failure that motivated these: `tools/list` could not even be attempted
without a Google service-account key, because the auth flow minted a token
before every request — including the handshake. A server that needs no bearer,
or one being smoke-tested with a pasted `gcloud` token, was unreachable.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.services.google_auth import (
    GoogleBearerAuth,
    GoogleTokenError,
    StaticBearerAuth,
    build_mcp_auth,
)

settings = get_settings()


class _Request:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}


async def _run_flow(auth) -> _Request:
    request = _Request()
    flow = auth.async_auth_flow(request)
    await flow.__anext__()
    return request


def test_none_mode_sends_no_bearer(monkeypatch):
    monkeypatch.setattr(settings, "mcp_auth_mode", "none")
    assert build_mcp_auth() is None


async def test_static_token_mode_sends_it_verbatim(monkeypatch):
    monkeypatch.setattr(settings, "mcp_auth_mode", "static_token")
    monkeypatch.setattr(settings, "mcp_static_token", "ya29.test-token")

    auth = build_mcp_auth()
    assert isinstance(auth, StaticBearerAuth)
    assert (await _run_flow(auth)).headers["Authorization"] == "Bearer ya29.test-token"


def test_static_token_mode_without_a_token_fails_loudly(monkeypatch):
    monkeypatch.setattr(settings, "mcp_auth_mode", "static_token")
    monkeypatch.setattr(settings, "mcp_static_token", "")

    with pytest.raises(GoogleTokenError, match="MCP_STATIC_TOKEN"):
        build_mcp_auth()


def test_service_account_is_the_default(monkeypatch):
    monkeypatch.setattr(settings, "mcp_auth_mode", "service_account")
    assert isinstance(build_mcp_auth(), GoogleBearerAuth)


async def test_missing_sa_file_explains_the_alternatives(monkeypatch):
    """The old error was a bare FileNotFoundError buried in a TaskGroup."""
    monkeypatch.setattr(settings, "mcp_auth_mode", "service_account")
    monkeypatch.setattr(settings, "google_sa_file", "/definitely/not/here.json")

    with pytest.raises(GoogleTokenError) as caught:
        await _run_flow(build_mcp_auth())

    message = str(caught.value)
    assert "MCP_AUTH_MODE=static_token" in message
    assert "MCP_AUTH_MODE=none" in message


def test_blank_secops_headers_are_omitted_not_sent_empty(monkeypatch):
    monkeypatch.setattr(settings, "secops_project_id", "proj-1")
    monkeypatch.setattr(settings, "secops_region", "")
    monkeypatch.setattr(settings, "secops_customer_id", "")

    headers = settings.secops_headers
    assert headers == {"Project-Id": "proj-1"}


def test_all_four_headers_present_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "secops_project_id", "proj-1")
    monkeypatch.setattr(settings, "secops_region", "us")
    monkeypatch.setattr(settings, "secops_customer_id", "cust-1")

    assert settings.secops_headers == {
        "Project-Id": "proj-1",
        "Region": "us",
        "Customer-Id": "cust-1",
    }


def test_secops_ids_are_recommended_not_required(monkeypatch):
    """They only warn, so the app can be pointed at a non-SecOps MCP server
    for a smoke test."""
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "llm_proxy_url", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    monkeypatch.setattr(settings, "llm_model_name", "gpt-4o")
    monkeypatch.setattr(settings, "mcp_server_url", "https://mcp.example/mcp")
    monkeypatch.setattr(settings, "secops_project_id", "")
    monkeypatch.setattr(settings, "secops_region", "")
    monkeypatch.setattr(settings, "secops_customer_id", "")
    monkeypatch.setattr(settings, "mcp_auth_mode", "none")

    assert settings.missing_required() == []
    assert "SECOPS_PROJECT_ID" in settings.missing_recommended()


def test_dev_without_a_secret_key_uses_a_stable_fallback(monkeypatch):
    """A per-process random key would break multi-worker deployments: worker A
    signs a cookie worker B rejects. The fallback must be deterministic."""
    from app.core import security

    monkeypatch.setattr(settings, "secret_key", "")
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "app_env", "dev")

    assert security._secret() == security._secret()


def test_prod_without_a_secret_key_refuses_to_sign(monkeypatch):
    from app.core import security

    monkeypatch.setattr(settings, "secret_key", "")
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "app_env", "prod")

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        security._secret()
