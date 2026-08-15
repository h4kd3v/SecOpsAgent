"""Settings are read at import time, so give the tests a valid environment
before any app module loads."""

import os

# Integration tests run only when a throwaway Postgres is pointed at here.
if "TEST_DATABASE_URL" in os.environ:
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

os.environ.setdefault("SECRET_KEY", "test-secret-key-not-used-anywhere")
os.environ.setdefault("LLM_PROXY_URL", "http://llm.invalid/v1")
os.environ.setdefault("LLM_API_KEY", "test")
os.environ.setdefault("LLM_MODEL_NAME", "test-model")
os.environ.setdefault("MCP_SERVER_URL", "http://mcp.invalid/mcp")
os.environ.setdefault("SECOPS_PROJECT_ID", "test-project")
os.environ.setdefault("SECOPS_REGION", "us")
os.environ.setdefault("SECOPS_CUSTOMER_ID", "test-customer")
os.environ.setdefault("GOOGLE_SA_FILE", "/nonexistent/sa.json")
os.environ.setdefault(
    "TOOL_READONLY_PATTERNS",
    r"^(get|list|search|lookup|fetch|query|read|describe|show|find|count)_",
)
