#!/bin/sh
set -eu
export POSTGRES_PWD="$(cat /run/secrets/temporal_db_password)"
export VISIBILITY_POSTGRES_PWD="$(cat /run/secrets/visibility_db_password)"
exec /etc/temporal/entrypoint.sh
