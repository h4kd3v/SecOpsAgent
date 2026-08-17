"""`.env.example` is the only documentation most operators read.

A setting that exists in code but never appears in the template is one nobody
knows to set — which is how LLM_MODEL_PRICING shipped invisible. A key in the
template that no longer exists in code is worse: it looks configurable and is
silently ignored.
"""

from __future__ import annotations

import pathlib
import re

from app.config import Settings

EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / ".env.example"

# Read by docker-compose to initialise the database container, not by the app.
# They belong in the template and will never be Settings fields.
COMPOSE_ONLY = {"postgres_user", "postgres_password", "postgres_db"}

# Settings that are deliberately absent from the template: secrets with no
# sensible placeholder, or knobs only meaningful in tests and demos.
NOT_DOCUMENTED = {
    "app_env",
    "database_url",
    "demo_mode",
    "log_level",
    "google_id_token_audience",
    "tool_cache_enabled",
    "tool_denylist",
    "tool_readonly_patterns",
    "tool_description_max_chars",
    "mcp_session_max_age",
    "anon_session_days",
    "google_token_refresh_skew",
    "google_scopes",
    "google_token_type",
    "google_sa_file",
    "llm_max_output_tokens",
}


def _keys_in_example() -> set[str]:
    keys = set()
    for line in EXAMPLE.read_text().splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", line.strip())
        if match:
            keys.add(match.group(1).lower())
    return keys


def test_every_documented_key_is_a_real_setting():
    """A key nobody reads is harmless; a key that looks configurable and is
    ignored costs someone an afternoon."""
    fields = set(Settings.model_fields)
    unknown = _keys_in_example() - fields - COMPOSE_ONLY
    assert not unknown, f".env.example documents settings that do not exist: {sorted(unknown)}"


def test_settings_worth_configuring_appear_in_the_template():
    fields = set(Settings.model_fields) - NOT_DOCUMENTED
    missing = fields - _keys_in_example()
    assert not missing, (
        f"settings missing from .env.example, so nobody knows they exist: {sorted(missing)}"
    )


def test_the_pricing_example_actually_parses():
    """The format is fiddly enough that a wrong example is worse than none."""
    settings = Settings(llm_model_pricing="gpt-4.1=2.00/8.00,claude-opus-5=5.00/25.00")
    assert settings.pricing_table == {
        "gpt-4.1": (2.00, 8.00),
        "claude-opus-5": (5.00, 25.00),
    }
