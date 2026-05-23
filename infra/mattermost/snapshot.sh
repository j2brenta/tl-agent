#!/usr/bin/env bash
# pg_dump / pg_restore the Mattermost postgres so evals can reset state
# between scenarios deterministically.

set -euo pipefail

ACTION="${1:-dump}"
CONTAINER="${MM_DB_CONTAINER:-tla-mm-db}"
USER="${MM_DB_USER:-mmuser}"
DB="${MM_DB_NAME:-mattermost}"
FIXTURE="$(dirname "$0")/fixtures/mattermost.dump"

mkdir -p "$(dirname "$FIXTURE")"

case "$ACTION" in
    dump)
        echo "==> pg_dump → $FIXTURE"
        docker exec "$CONTAINER" pg_dump -U "$USER" -d "$DB" -Fc > "$FIXTURE"
        ;;
    restore)
        echo "==> pg_restore from $FIXTURE"
        # Drop public schema + recreate, then restore.
        docker exec -i "$CONTAINER" psql -U "$USER" -d "$DB" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
        docker exec -i "$CONTAINER" pg_restore -U "$USER" -d "$DB" --no-owner --clean --if-exists < "$FIXTURE"
        ;;
    *)
        echo "usage: $0 {dump|restore}" >&2
        exit 1
        ;;
esac
