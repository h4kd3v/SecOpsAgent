"""Persistent tool-definition cache.

Tool definitions change on a release cadence, not per request. These cover the
behaviours that make caching them safe: the TTL, surviving a restart, serving
stale data rather than failing during an outage, and re-applying filtering and
classification on read so config changes don't need a refetch.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="needs a throwaway Postgres"
)

from sqlalchemy import select, update  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models import Base, McpToolCatalog  # noqa: E402
from app.db.session import SessionMaker, engine  # noqa: E402
from app.services import tool_catalog as catalog_module  # noqa: E402
from app.services.mcp_manager import McpUnavailable, ToolSpec  # noqa: E402
from app.services.tool_catalog import ToolCatalog  # noqa: E402

settings = get_settings()

READ = ToolSpec("udm_search", "Search UDM", {"type": "object"}, True, read_only_hint=True)
WRITE = ToolSpec("update_case", "Update a case", {"type": "object"}, False, read_only_hint=False)


class FakeMcp:
    def __init__(self, specs=None, fail=False) -> None:
        self.specs = specs if specs is not None else [READ, WRITE]
        self.fail = fail
        self.fetches = 0
        self.last_error = "MCPError: Not Found"

    async def list_tools(self):
        self.fetches += 1
        if self.fail:
            raise McpUnavailable("MCPError: Not Found")
        return self.specs


@pytest.fixture
async def db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with SessionMaker() as session:
        yield session
    await engine.dispose()


async def _age_cache(db, hours: float) -> None:
    await db.execute(
        update(McpToolCatalog).values(
            fetched_at=datetime.now(UTC) - timedelta(hours=hours)
        )
    )
    await db.commit()


async def test_first_call_fetches_and_persists(db, monkeypatch):
    fake = FakeMcp()
    monkeypatch.setattr(catalog_module, "mcp_manager", fake)

    result = await ToolCatalog().get(db)

    assert result.source == "live"
    assert fake.fetches == 1
    assert {t.name for t in result.tools} == {"udm_search", "update_case"}

    row = (await db.execute(select(McpToolCatalog))).scalar_one()
    assert row.tool_count == 2
    assert row.server_url == settings.mcp_server_url


async def test_second_call_within_ttl_does_not_hit_the_server(db, monkeypatch):
    fake = FakeMcp()
    monkeypatch.setattr(catalog_module, "mcp_manager", fake)
    catalog = ToolCatalog()

    await catalog.get(db)
    result = await catalog.get(db)

    assert result.source == "cache"
    assert fake.fetches == 1, "the cached copy should have been served"


async def test_cache_survives_a_restart(db, monkeypatch):
    """A fresh ToolCatalog instance - as after a container restart - must
    still find the persisted copy."""
    fake = FakeMcp()
    monkeypatch.setattr(catalog_module, "mcp_manager", fake)

    await ToolCatalog().get(db)
    result = await ToolCatalog().get(db)  # new instance, no in-memory state

    assert result.source == "cache"
    assert fake.fetches == 1


async def test_expired_cache_is_refetched(db, monkeypatch):
    fake = FakeMcp()
    monkeypatch.setattr(catalog_module, "mcp_manager", fake)
    catalog = ToolCatalog()

    await catalog.get(db)
    await _age_cache(db, settings.tool_cache_ttl_hours + 1)
    result = await catalog.get(db)

    assert result.source == "live"
    assert fake.fetches == 2


async def test_force_refresh_bypasses_a_fresh_cache(db, monkeypatch):
    fake = FakeMcp()
    monkeypatch.setattr(catalog_module, "mcp_manager", fake)
    catalog = ToolCatalog()

    await catalog.get(db)
    result = await catalog.get(db, force=True)

    assert result.source == "live"
    assert fake.fetches == 2


async def test_outage_serves_stale_rather_than_failing(db, monkeypatch):
    """The whole point: a brief SecOps outage must not stop analysts working."""
    fake = FakeMcp()
    monkeypatch.setattr(catalog_module, "mcp_manager", fake)
    catalog = ToolCatalog()
    await catalog.get(db)

    await _age_cache(db, settings.tool_cache_ttl_hours + 1)
    fake.fail = True
    result = await catalog.get(db)

    assert result.source == "stale"
    assert result.stale is True
    assert result.error
    assert {t.name for t in result.tools} == {"udm_search", "update_case"}


async def test_outage_with_no_cache_at_all_still_raises(db, monkeypatch):
    monkeypatch.setattr(catalog_module, "mcp_manager", FakeMcp(fail=True))

    with pytest.raises(McpUnavailable):
        await ToolCatalog().get(db)


async def test_allowlist_applies_on_read_without_a_refetch(db, monkeypatch):
    """Filtering must not be baked into the cache, or changing TOOL_ALLOWLIST
    would mean waiting out the TTL."""
    fake = FakeMcp()
    monkeypatch.setattr(catalog_module, "mcp_manager", fake)
    catalog = ToolCatalog()
    await catalog.get(db)

    monkeypatch.setattr(settings, "tool_allowlist", "udm_search")
    result = await catalog.get(db)

    assert [t.name for t in result.tools] == ["udm_search"]
    assert fake.fetches == 1, "filtering must not trigger a refetch"


async def test_classification_is_recomputed_on_read(db, monkeypatch):
    """An unannotated tool is classified by name regex. Changing the regex
    must take effect immediately."""
    unannotated = ToolSpec("frobnicate_thing", "does a thing", {}, False)
    fake = FakeMcp([unannotated])
    monkeypatch.setattr(catalog_module, "mcp_manager", fake)
    catalog = ToolCatalog()

    first = await catalog.get(db)
    assert first.tools[0].read_only is False  # fails closed

    monkeypatch.setattr(settings, "tool_readonly_patterns", "^frobnicate_")
    second = await catalog.get(db)

    assert second.tools[0].read_only is True
    assert fake.fetches == 1


async def test_server_annotations_survive_the_round_trip(db, monkeypatch):
    """Chronicle annotates its tools; that must not be lost by caching."""
    fake = FakeMcp()
    monkeypatch.setattr(catalog_module, "mcp_manager", fake)
    catalog = ToolCatalog()
    await catalog.get(db)

    cached = await catalog.get(db)
    by_name = {t.name: t for t in cached.tools}

    assert by_name["udm_search"].read_only is True
    assert by_name["update_case"].read_only is False


async def test_repointing_the_server_url_invalidates_the_cache(db, monkeypatch):
    fake = FakeMcp()
    monkeypatch.setattr(catalog_module, "mcp_manager", fake)
    catalog = ToolCatalog()
    await catalog.get(db)

    monkeypatch.setattr(settings, "mcp_server_url", "https://other-server.example/mcp")
    result = await catalog.get(db)

    assert result.source == "live"
    assert fake.fetches == 2, "a different server must not serve the old catalogue"


async def test_disabling_the_cache_always_refetches(db, monkeypatch):
    fake = FakeMcp()
    monkeypatch.setattr(catalog_module, "mcp_manager", fake)
    monkeypatch.setattr(settings, "tool_cache_enabled", False)
    catalog = ToolCatalog()

    await catalog.get(db)
    await catalog.get(db)

    assert fake.fetches == 2
