"""Persistent cache of the MCP server's tool definitions.

Tool definitions change on the SecOps team's release cadence, not per request,
so re-fetching them on every session recycle is pure waste. They're stored in
Postgres and re-fetched only once the cache passes `TOOL_CACHE_TTL_HOURS`.

Three properties worth stating, because they're what make this safe:

*   **Survives restarts and session recycles.** The MCP transport session is
    rebuilt every 45 minutes for token rotation; the catalogue isn't.
*   **Stale beats nothing.** If a refresh fails and a cached copy exists, the
    stale copy is served with `stale=True` rather than failing the request. A
    brief SecOps outage stops nobody from working.
*   **Filtering and classification happen on read.** Only the server's raw
    definitions are cached, so changing `TOOL_ALLOWLIST`, `TOOL_DENYLIST` or
    `TOOL_READONLY_PATTERNS` takes effect immediately, with no refetch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import McpToolCatalog
from app.services.mcp_manager import McpUnavailable, ToolSpec, mcp_manager

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class Catalog:
    tools: list[ToolSpec]
    fetched_at: datetime | None
    source: str  # "cache" | "live" | "stale"
    stale: bool = False
    error: str | None = None

    @property
    def age_seconds(self) -> int | None:
        if self.fetched_at is None:
            return None
        return int((datetime.now(UTC) - self.fetched_at).total_seconds())


def _visible(specs: list[ToolSpec]) -> list[ToolSpec]:
    """Apply allow/deny filtering at read time, not at fetch time."""
    denied = settings.denied_tools
    allowed = settings.allowed_tools
    out = [s for s in specs if s.name not in denied]
    if allowed:
        out = [s for s in out if s.name in allowed]
    return out


class ToolCatalog:
    async def _row(self, db: AsyncSession) -> McpToolCatalog | None:
        result = await db.execute(
            select(McpToolCatalog).where(McpToolCatalog.server_url == settings.mcp_server_url)
        )
        return result.scalar_one_or_none()

    def _fresh(self, row: McpToolCatalog | None) -> bool:
        if row is None or not settings.tool_cache_enabled:
            return False
        cutoff = datetime.now(UTC) - timedelta(hours=settings.tool_cache_ttl_hours)
        fetched = row.fetched_at
        if fetched.tzinfo is None:  # defensive: asyncpg should return aware
            fetched = fetched.replace(tzinfo=UTC)
        return fetched > cutoff

    async def _store(self, db: AsyncSession, specs: list[ToolSpec]) -> datetime:
        now = datetime.now(UTC)
        payload = [spec.to_cache() for spec in specs]
        # Upsert: two workers can refresh concurrently after a restart, and the
        # unique constraint on server_url would otherwise make one of them fail.
        await db.execute(
            insert(McpToolCatalog)
            .values(
                server_url=settings.mcp_server_url,
                tools=payload,
                tool_count=len(payload),
                fetched_at=now,
            )
            .on_conflict_do_update(
                index_elements=[McpToolCatalog.server_url],
                set_={"tools": payload, "tool_count": len(payload), "fetched_at": now},
            )
        )
        await db.commit()
        return now

    async def get(self, db: AsyncSession, *, force: bool = False) -> Catalog:
        row = await self._row(db)

        if not force and self._fresh(row):
            assert row is not None
            specs = [ToolSpec.from_cache(t) for t in row.tools]
            return Catalog(_visible(specs), row.fetched_at, "cache")

        try:
            specs = await mcp_manager.list_tools()
            fetched_at = await self._store(db, specs)
            logger.info(
                "refreshed MCP tool catalogue",
                extra={"tools": len(specs), "ttl_hours": settings.tool_cache_ttl_hours},
            )
            return Catalog(_visible(specs), fetched_at, "live")
        except Exception as exc:  # noqa: BLE001 - any failure serves stale
            detail = mcp_manager.last_error or str(exc)
            if row is not None:
                # Expired but present. Serving it beats failing the request:
                # tool definitions do not change during a brief outage.
                logger.warning("MCP refresh failed, serving stale catalogue: %s", detail)
                specs = [ToolSpec.from_cache(t) for t in row.tools]
                return Catalog(_visible(specs), row.fetched_at, "stale", stale=True, error=detail)
            logger.error("MCP unreachable and no cached catalogue: %s", detail)
            raise McpUnavailable(detail) from exc


tool_catalog = ToolCatalog()
