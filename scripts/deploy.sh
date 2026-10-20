#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"
[ -f "$ENV_FILE" ] || die 'Create .env from .env.example and configure it locally first.'
case "$ENV_FILE" in /*|*..*) die 'ENV_FILE must be a relative path within the repository.' ;; esac
umask 077
lock_maintenance
trap unlock_maintenance EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM
if docker volume inspect "${PROJECT_NAME}_core-data" >/dev/null 2>&1; then
    for name in core_postgres_password memory_postgres_password app_db_password temporal_db_password visibility_db_password litellm_db_password memory_db_password litellm_master_key litellm_salt_key; do
        [ -s ".secrets/$name" ] || die 'Persistent data exists but internal credentials are missing. Restore the original .secrets directory; refusing to regenerate credentials.'
    done
fi
docker run --rm --user "$(id -u):$(id -g)" -v "$PROJECT_ROOT:/workspace" -w /workspace "$PYTHON_IMAGE" python scripts/configure.py --env-file "$ENV_FILE"
compose config --quiet
echo 'Building application and retrieval database images.'
compose build
echo 'Stopping application writers before migrations; a migration failure keeps them stopped.'
compose stop -t 120 web api worker dispatcher hindsight litellm temporal
echo 'Starting persistent databases, migrations, model gateway, and local memory models.'
echo 'Initial memory model downloads may take several minutes; caches survive retries.'
# Re-run migrations/bootstrap on each deployment; preserve all volumes and secrets.
compose up -d postgres-core postgres-memory
compose run --rm db-init
compose run --rm app-migrate
compose run --rm temporal-schema
compose run --rm hindsight-migrate
compose up -d temporal litellm
compose run --rm temporal-namespace
compose run --rm gateway-bootstrap
compose run --rm app-bootstrap
compose up -d --wait --wait-timeout "$(cat .runtime/deploy-timeout)" hindsight api worker dispatcher web
./scripts/smoke-test.sh
