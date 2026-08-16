from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore"
    )

    app_env: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"
    # Simulates the LLM proxy and the SecOps MCP server in-process so the app
    # runs with no credentials at all. Everything else is real: Postgres,
    # migrations, streaming, the approval gate, the live sidebar.
    demo_mode: bool = False
    # Signs the anonymous session cookie. Rotating it orphans every visitor's
    # chat history (the rows survive; nobody can reach them).
    secret_key: str = ""
    database_url: str = "postgresql+asyncpg://secops:secops@localhost:5432/secops_chat"

    # --- LiteLLM proxy -----------------------------------------------------
    llm_proxy_url: str = ""
    llm_api_key: str = ""
    llm_model_name: str = ""
    # Human-readable label shown under each answer, e.g. "Claude Opus 4.6".
    # Empty = show whatever model id the gateway reports, which is the honest
    # default but is often an internal alias.
    llm_model_display_name: str = ""
    llm_request_timeout: float = 180.0
    llm_max_tool_iterations: int = 10
    llm_max_context_messages: int = 40
    # Approximate character budget for replayed history. Trimming by message
    # count alone is useless when a single SecOps result is half a megabyte.
    llm_max_context_chars: int = 400_000
    # 0 = let the proxy decide. Set only if your proxy's default is too low;
    # a low cap truncates answers mid-sentence and looks exactly like a
    # streaming bug (watch for finish_reason="length" in the UI warning).
    llm_max_output_tokens: int = 0
    # Per-million-token rates, as `model=input/output` entries separated by
    # commas: "gpt-4.1=2.00/8.00,claude-opus-5=5.00/25.00". A model with no
    # entry records no cost rather than a wrong one. `*` is a fallback for
    # everything unlisted. Rates are stored on each turn as they were applied,
    # so changing them here never rewrites what past turns cost.
    llm_model_pricing: str = ""
    # 0 = pass MCP results to the model in full, which is the default: the
    # model decides what matters, and it can't reason about data it never saw.
    tool_result_max_chars: int = 0

    # --- Remote SecOps MCP -------------------------------------------------
    mcp_server_url: str = ""
    mcp_transport: Literal["streamable_http", "sse"] = "streamable_http"
    # How the Authorization header is produced:
    #   service_account - mint a Google token from sa.json (production)
    #   static_token    - use MCP_STATIC_TOKEN verbatim, e.g. the output of
    #                     `gcloud auth print-access-token`. Handy for a
    #                     60-minute connectivity test with no key file.
    #   none            - send no Authorization header at all
    mcp_auth_mode: Literal["service_account", "static_token", "none"] = "service_account"
    mcp_static_token: str = ""
    mcp_tool_timeout: float = 90.0
    # Recycled well inside the 60-minute token lifetime so a server that pins
    # a transport session to the credential it was opened with never goes stale.
    mcp_session_max_age: float = 2700.0

    # Sent on every MCP request, alongside the Authorization bearer.
    secops_project_id: str = ""
    secops_region: str = ""
    secops_customer_id: str = ""

    # --- Google service account -------------------------------------------
    google_sa_file: str = "/run/secrets/sa.json"
    # access_token: OAuth2 bearer scoped to cloud-platform (Chronicle/SecOps APIs).
    # id_token: signed JWT for the target audience (Cloud Run-hosted MCP servers).
    google_token_type: Literal["access_token", "id_token"] = "access_token"
    google_scopes: str = "https://www.googleapis.com/auth/cloud-platform"
    google_id_token_audience: str = ""
    # Refresh this many seconds before the 60-minute token actually expires.
    google_token_refresh_skew: float = 300.0

    # --- Tool safety -------------------------------------------------------
    tool_readonly_patterns: str = (
        r"^(get|list|search|lookup|fetch|query|read|describe|show|find|count)_"
    )
    tool_denylist: str = ""
    # Comma-separated tool names. Empty = expose everything the server offers.
    # The real SecOps MCP server exposes ~70 tools whose schemas come to ~79k
    # tokens on EVERY completion, so narrowing this is a large cost saving.
    tool_allowlist: str = ""
    # Chronicle ships multi-kilobyte descriptions with response templates
    # embedded; the useful part is at the top.
    tool_description_max_chars: int = 1024
    # Tool definitions change rarely, so they're persisted and re-fetched only
    # when this old. Survives restarts and MCP session recycles, and lets the
    # app keep working through a brief SecOps outage.
    tool_cache_ttl_hours: float = 24.0
    tool_cache_enabled: bool = True
    require_approval_for_write: bool = True

    # --- Anonymous sessions -------------------------------------------------
    # How long a visitor's cookie (and therefore their sidebar history) lasts.
    anon_session_days: int = 90
    # A conversation with no messages is an orphan — the UI only creates one
    # when the first message is sent, so any empty row older than this is
    # debris from a failed first turn. 0 disables the sweep.
    empty_conversation_ttl_hours: int = 24
    # One shared workspace: every analyst sees every conversation in their
    # sidebar and can contribute to any of them. This is a deliberate trade —
    # a shift working one incident together beats twenty private transcripts —
    # but it does mean there is no privacy between analysts. Set false to go
    # back to per-browser history.
    shared_workspace: bool = True
    rate_limit_messages_per_minute: int = 20
    cors_origins: str = ""

    # --- Database pool ------------------------------------------------------
    # Sized against concurrent *turns*, not requests: each in-flight turn holds
    # a request-scoped connection plus one for the streaming response for its
    # whole duration, so ~20 analysts answering at once need ~40. Default
    # Postgres allows 100 connections, so 40+20 leaves room for psql, the event
    # bus listener and a second backend replica.
    db_pool_size: int = 40
    db_max_overflow: int = 20
    # Fail fast rather than making an analyst stare at a dead composer for
    # thirty seconds before the turn errors anyway.
    db_pool_timeout: float = 10.0

    @field_validator("cors_origins")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def pricing_table(self) -> dict[str, tuple[float, float]]:
        """model -> (input $/1M, output $/1M)."""
        table: dict[str, tuple[float, float]] = {}
        for entry in self.llm_model_pricing.split(","):
            entry = entry.strip()
            if not entry or "=" not in entry:
                continue
            name, _, rates = entry.partition("=")
            given, _, output = rates.partition("/")
            try:
                table[name.strip()] = (float(given), float(output or given))
            except ValueError:
                continue
        return table

    @property
    def scope_list(self) -> list[str]:
        return [s.strip() for s in self.google_scopes.split(",") if s.strip()]

    @property
    def readonly_regexes(self) -> list[re.Pattern[str]]:
        return [
            re.compile(p.strip(), re.I)
            for p in self.tool_readonly_patterns.split("|||")
            if p.strip()
        ]

    @property
    def denied_tools(self) -> set[str]:
        return {t.strip() for t in self.tool_denylist.split(",") if t.strip()}

    @property
    def allowed_tools(self) -> set[str]:
        return {t.strip() for t in self.tool_allowlist.split(",") if t.strip()}

    @property
    def cookie_secure(self) -> bool:
        return self.app_env == "prod"

    def missing_required(self) -> list[str]:
        """Config the app cannot work without once demo mode is off.

        Reported as one readable list at startup rather than as a pydantic
        traceback on import, so a half-filled .env tells you exactly what is
        missing instead of dying on the first field."""
        if self.demo_mode:
            return []
        required = {
            "LLM_PROXY_URL": self.llm_proxy_url,
            "LLM_API_KEY": self.llm_api_key,
            "LLM_MODEL_NAME": self.llm_model_name,
            "MCP_SERVER_URL": self.mcp_server_url,
        }
        if self.app_env == "prod":
            required["SECRET_KEY"] = self.secret_key
        return sorted(name for name, value in required.items() if not value)

    def missing_recommended(self) -> list[str]:
        """Not fatal, but the real SecOps MCP server rejects calls without
        them. Absent when pointing at some other MCP server for a smoke test,
        which is why these only warn."""
        if self.demo_mode:
            return []
        recommended = {
            "SECOPS_PROJECT_ID": self.secops_project_id,
            "SECOPS_REGION": self.secops_region,
            "SECOPS_CUSTOMER_ID": self.secops_customer_id,
        }
        if self.mcp_auth_mode == "static_token" and not self.mcp_static_token:
            recommended["MCP_STATIC_TOKEN"] = ""
        return sorted(name for name, value in recommended.items() if not value)

    @property
    def secops_headers(self) -> dict[str, str]:
        """The three static headers the SecOps MCP server requires on every
        call. `Authorization` is added per-request by the token manager, since
        it rotates hourly and the transport session outlives it."""
        # Empty values are omitted rather than sent blank: a header present
        # but empty is more likely to confuse a server than one that's absent.
        headers = {
            "Project-Id": self.secops_project_id,
            "Region": self.secops_region,
            "Customer-Id": self.secops_customer_id,
        }
        return {k: v for k, v in headers.items() if v}


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
