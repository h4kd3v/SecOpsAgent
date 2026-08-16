#!/bin/sh
set -e

echo "running migrations..."
alembic upgrade head

# One worker by default, deliberately.
#
# The workload is I/O-bound — the process spends its time waiting on the LLM
# gateway, SecOps and Postgres — so a single asyncio loop serves 20 analysts
# comfortably, and several pieces of state are per-process:
#
#   * the rate limiter, so N workers means N x the configured limit;
#   * the MCP session, so N workers open N sessions to Chronicle;
#   * the readiness probe cache.
#
# Raising this also multiplies DB connections: each worker opens its own pool
# of DB_POOL_SIZE + DB_MAX_OVERFLOW. The app checks that product against
# Postgres' max_connections at startup and refuses to pretend it fits.
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers "${UVICORN_WORKERS:-1}" \
    --timeout-keep-alive 75 \
    --proxy-headers \
    --forwarded-allow-ips '*'
