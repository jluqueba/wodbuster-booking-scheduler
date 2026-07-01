#!/bin/sh
# Container entrypoint for the WodBuster worker.
#
# Runs Alembic migrations against the mounted SQLite database, then
# hands control to uvicorn. Alembic is idempotent: it will fast-path
# when the schema is already at head. Keeping the migration step here
# instead of a separate init container matches ADR-0002 (single
# always-on replica) and avoids a second container in the revision.
#
# Any argument passed to this script overrides the default uvicorn
# command. Container Apps sets no arguments in normal operation.

set -eu

echo "[entrypoint] alembic upgrade head"
alembic -c /app/alembic.ini upgrade head

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

exec uvicorn wodbuster_worker.app:app --host 0.0.0.0 --port 8000
