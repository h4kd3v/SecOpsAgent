#!/usr/bin/env bash
#
# Replace the chat database with a dump made by backup-db.sh.
#
# Destructive on purpose and by announcement: this overwrites every
# conversation currently in the database. An untested restore is not a backup,
# so run it once against a throwaway copy before you need it for real.
#
# Usage:
#   ./scripts/restore-db.sh backups/secops-chat-20260817-100000.sql.gz
#
set -euo pipefail

cd "$(dirname "$0")/.."

FILE="${1:-}"
USER_NAME="${POSTGRES_USER:-secops}"
DB_NAME="${POSTGRES_DB:-secops_chat}"

if [ -z "$FILE" ] || [ ! -f "$FILE" ]; then
    echo "usage: $0 <dump.sql.gz>" >&2
    echo >&2
    echo "available:" >&2
    ls -1t backups/*.sql.gz 2>/dev/null | head -10 >&2 || echo "  (none in ./backups)" >&2
    exit 1
fi

if ! gzip -t "$FILE" 2>/dev/null; then
    echo "$FILE is not a readable gzip file" >&2
    exit 1
fi

if ! docker compose ps --status running --format '{{.Service}}' | grep -qx postgres; then
    echo "postgres is not running; start it with 'docker compose up -d postgres'" >&2
    exit 1
fi

CURRENT=$(docker compose exec -T postgres psql -U "$USER_NAME" -d "$DB_NAME" \
    -t -A -c "SELECT count(*) FROM conversations" 2>/dev/null | tr -d '[:space:]' || echo "?")

echo "This will REPLACE the current database."
echo "  target      : $DB_NAME"
echo "  it now holds: ${CURRENT} conversations"
echo "  restoring   : $FILE"
read -r -p "Type 'restore' to continue: " CONFIRM
[ "$CONFIRM" = "restore" ] || { echo "aborted"; exit 1; }

# Stop the app first: restoring under a live backend means it holds sessions
# and rows that are about to be dropped underneath it.
echo "stopping backend ..."
docker compose stop backend >/dev/null

echo "restoring ..."
gunzip -c "$FILE" | docker compose exec -T postgres psql \
    --username "$USER_NAME" --dbname "$DB_NAME" --quiet --set ON_ERROR_STOP=on

echo "starting backend ..."
docker compose up -d backend >/dev/null

RESTORED=$(docker compose exec -T postgres psql -U "$USER_NAME" -d "$DB_NAME" \
    -t -A -c "SELECT count(*) FROM conversations" 2>/dev/null | tr -d '[:space:]')
echo "done - ${RESTORED} conversations restored"
