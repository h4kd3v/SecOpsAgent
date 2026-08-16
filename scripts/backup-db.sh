#!/usr/bin/env bash
#
# Dump the chat database to ./backups/.
#
# The Docker volume already survives restarts, crashes and `docker compose
# down`. What it does not survive is `docker compose down -v`, `docker volume
# prune`, a rebuilt machine, or a failed disk — and none of those announce
# themselves as "you are about to delete every investigation". A dump is the
# only copy that can be moved off this host.
#
# Usage:
#   ./scripts/backup-db.sh                 # timestamped dump into ./backups
#   ./scripts/backup-db.sh /mnt/nas        # somewhere that isn't this disk
#
set -euo pipefail

cd "$(dirname "$0")/.."

DEST="${1:-backups}"
USER_NAME="${POSTGRES_USER:-secops}"
DB_NAME="${POSTGRES_DB:-secops_chat}"
STAMP="$(date +%Y%m%d-%H%M%S)"
FILE="$DEST/secops-chat-$STAMP.sql.gz"

mkdir -p "$DEST"

if ! docker compose ps --status running --format '{{.Service}}' | grep -qx postgres; then
    echo "postgres is not running; start it with 'docker compose up -d postgres'" >&2
    exit 1
fi

echo "dumping $DB_NAME ..."
# --clean --if-exists so the dump can be replayed over an existing database
# without hand-dropping it first; that is the path people actually take at 3am.
docker compose exec -T postgres pg_dump \
    --username "$USER_NAME" \
    --dbname "$DB_NAME" \
    --clean --if-exists --no-owner --no-privileges \
    | gzip -9 > "$FILE"

# A dump that silently produced nothing is worse than no dump: it looks like a
# backup in the listing and restores an empty database.
if [ ! -s "$FILE" ]; then
    echo "dump is empty - refusing to keep it" >&2
    rm -f "$FILE"
    exit 1
fi

if ! gzip -t "$FILE" 2>/dev/null; then
    echo "dump failed its integrity check - refusing to keep it" >&2
    rm -f "$FILE"
    exit 1
fi

CONVERSATIONS=$(docker compose exec -T postgres psql -U "$USER_NAME" -d "$DB_NAME" \
    -t -A -c "SELECT count(*) FROM conversations" 2>/dev/null | tr -d '[:space:]')
MESSAGES=$(docker compose exec -T postgres psql -U "$USER_NAME" -d "$DB_NAME" \
    -t -A -c "SELECT count(*) FROM messages" 2>/dev/null | tr -d '[:space:]')

echo "wrote $FILE ($(du -h "$FILE" | cut -f1)) - ${CONVERSATIONS} conversations, ${MESSAGES} messages"
echo "restore with: ./scripts/restore-db.sh $FILE"
