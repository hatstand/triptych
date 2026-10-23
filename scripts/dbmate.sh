#!/usr/bin/env bash
# Run dbmate against the compose Postgres as the current user, so db/schema.sql stays yours.
#   scripts/dbmate.sh new add_trip_notes   # create a migration to hand-write
#   scripts/dbmate.sh up                   # apply + dump db/schema.sql
#   scripts/dbmate.sh rollback | status | dump
set -euo pipefail
cd "$(dirname "$0")/.."
exec docker compose run --rm --no-deps --user "$(id -u):$(id -g)" migrate "$@"
