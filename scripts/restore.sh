#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"
case "$ENV_FILE" in /*|*..*) die 'ENV_FILE must be a relative path within the repository.' ;; esac
[ "$#" -eq 3 ] && [ "$3" = --new-deployment ] || die 'Usage (empty checkout only): COMPOSE_PROJECT_NAME=new-name ./scripts/restore.sh /absolute/backup.tar.age /absolute/age-identity --new-deployment'
ARCHIVE=$1
IDENTITY=$2
case "$ARCHIVE:$IDENTITY" in /*:/*) ;; *) die 'Backup and identity paths must be absolute.' ;; esac
[ -f "$ARCHIVE" ] && [ -f "$IDENTITY" ] || die 'Backup or identity file is missing.'
[ ! -e .secrets ] && [ ! -e "$ENV_FILE" ] || die 'Refusing restore: this checkout already contains secrets or environment configuration. Use a new checkout.'
case "$PROJECT_NAME" in *[!a-z0-9_-]*|'') die 'Invalid target project name.' ;; esac
[ -z "$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT_NAME")" ] || die 'Refusing restore: target project already has containers.'
for volume in core-data memory-data attachments model-cache service-keys; do
    if docker volume inspect "${PROJECT_NAME}_$volume" >/dev/null 2>&1; then die 'Refusing restore: target project already has volumes.'; fi
done
umask 077
mkdir -p .backups
lock_maintenance
STAGE=$(mktemp -d "$PROJECT_ROOT/.backups/.restore.XXXXXX")
cleanup() { case "$STAGE" in "$PROJECT_ROOT"/.backups/.restore.*) rm -rf -- "$STAGE" ;; esac; unlock_maintenance; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM
docker build -q -t personal-agent-ops:local -f deploy/docker/ops.Dockerfile . >/dev/null
# Decrypt before extracting; age authenticates the entire archive before any restore begins.
docker run --rm -v "$ARCHIVE:/archive:ro" -v "$IDENTITY:/identity:ro" personal-agent-ops:local age --decrypt -i /identity /archive > "$STAGE/payload.tar"
docker run --rm --user "$(id -u):$(id -g)" -v "$STAGE:/restore" "$PYTHON_IMAGE" python -c "import tarfile; t=tarfile.open('/restore/payload.tar'); t.extractall('/restore/data', filter='data')"
[ -f "$STAGE/data/manifest.txt" ] || die 'Backup manifest missing.'
grep -qx 'format=1' "$STAGE/data/manifest.txt" || die 'Unsupported backup format.'
for database in app temporal temporal_visibility litellm hindsight; do
    [ -s "$STAGE/data/databases/$database.dump" ] || die 'Backup is missing a required database dump.'
done
for secret in core_postgres_password memory_postgres_password app_db_password temporal_db_password visibility_db_password litellm_db_password memory_db_password litellm_master_key litellm_salt_key; do
    [ -s "$STAGE/data/config/secrets/$secret" ] || die 'Backup is missing an internal credential; refusing to generate a replacement.'
done
for file in config/environment.env files/attachments.tar files/service-keys.tar; do
    [ -s "$STAGE/data/$file" ] || die 'Backup is missing a required configuration or volume archive.'
done
cp "$STAGE/data/config/environment.env" "$ENV_FILE"
cp -R "$STAGE/data/config/secrets" .secrets
chmod 700 .secrets
# Recovery restores data without contacting models; deployment rechecks live-provider settings.
docker run --rm --user "$(id -u):$(id -g)" -v "$PROJECT_ROOT:/workspace" -w /workspace "$PYTHON_IMAGE" python scripts/configure.py --env-file "$ENV_FILE" --infrastructure-only
compose build postgres-memory
compose up -d --wait postgres-core postgres-memory
compose run --rm db-init
for database in app temporal temporal_visibility litellm; do
    compose exec -T -u postgres postgres-core pg_restore -U postgres --exit-on-error -d "$database" < "$STAGE/data/databases/$database.dump"
done
compose exec -T -u postgres postgres-memory pg_restore -U postgres --exit-on-error -d hindsight < "$STAGE/data/databases/hindsight.dump"
for volume in attachments service-keys; do
    docker volume create --label "com.docker.compose.project=$PROJECT_NAME" --label "com.docker.compose.volume=$volume" "${PROJECT_NAME}_$volume" >/dev/null
done
docker run --rm -v "$STAGE/data/files:/backup:ro" -v "${PROJECT_NAME}_attachments:/attachments" -v "${PROJECT_NAME}_service-keys:/keys" personal-agent-ops:local sh -c 'tar -C /attachments -xf /backup/attachments.tar && tar -C /keys -xf /backup/service-keys.tar'
echo "Restored data into new project: $PROJECT_NAME"
echo 'Services remain stopped except databases. Review restored public URL/TLS and provider budget before ./scripts/deploy.sh.'
echo 'Restore verification is not complete until infrastructure and learning-history checks pass.'
