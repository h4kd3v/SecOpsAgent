#!/bin/sh
set -e

echo "running migrations..."
alembic upgrade head

# 20 analysts on an I/O-bound workload: 2 workers is plenty and keeps the
# in-process rate limiter and MCP session pool coherent. If you raise this,
# move both to Redis first.
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers "${UVICORN_WORKERS:-2}" \
    --timeout-keep-alive 75 \
    --proxy-headers \
    --forwarded-allow-ips '*'
