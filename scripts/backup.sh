#!/bin/sh
set -eu
. "$(dirname "$0")/common.sh"
[ "$#" -eq 1 ] || die 'Usage: ./scripts/backup.sh AGE_RECIPIENT_PUBLIC_KEY'
[ -f "$ENV_FILE" ] && [ -d .secrets ] || die 'Deployment configuration and secrets must exist before backup.'
RECIPIENT=$1
case "$RECIPIENT" in age1*|ssh-*) ;; *) die 'Supply an age recipient public key, never a private key.' ;; esac
umask 077
mkdir -p .backups
lock_maintenance
STAGE=$(mktemp -d "$PROJECT_ROOT/.backups/.stage.XXXXXX")
ARCHIVE="$PROJECT_ROOT/.backups/$PROJECT_NAME-$(date -u +%Y%m%dT%H%M%SZ).tar.age"
WRITERS=""
WRITER_IDS=""
cleanup() {
    result=$?
    trap - EXIT
    # Start exact existing containers; Compose start can re-run migration dependencies.
    if [ -n "$WRITER_IDS" ] && ! docker start $WRITER_IDS >/dev/null; then
        echo 'Some services did not resume; inspect container status.' >&2
        result=1
    fi
    case "$STAGE" in "$PROJECT_ROOT"/.backups/.stage.*) rm -rf -- "$STAGE" ;; esac
    unlock_maintenance
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM
docker build -q -t personal-agent-ops:local -f deploy/docker/ops.Dockerfile . >/dev/null
RUNNING=$(compose ps --services --status running)
for service in web api worker dispatcher hindsight litellm temporal; do
    if printf '%s\n' "$RUNNING" | grep -qx "$service"; then
        WRITERS="$WRITERS $service"
        WRITER_IDS="$WRITER_IDS $(compose ps -q "$service")"
    fi
done
echo 'Entering maintenance window: stopping writers while preserving databases and volumes.'
if [ -n "$WRITERS" ]; then compose stop -t 120 $WRITERS; fi
mkdir "$STAGE/databases" "$STAGE/config" "$STAGE/files"
cp "$ENV_FILE" "$STAGE/config/environment.env"
cp -R .secrets "$STAGE/config/secrets"
cp compose.yaml "$STAGE/config/compose.yaml"
for database in app temporal temporal_visibility litellm; do
    compose exec -T -u postgres postgres-core pg_dump -U postgres --format=custom "$database" > "$STAGE/databases/$database.dump"
done
compose exec -T -u postgres postgres-memory pg_dump -U postgres --format=custom hindsight > "$STAGE/databases/hindsight.dump"
docker run --rm -v "$STAGE/files:/backup" -v "${PROJECT_NAME}_attachments:/attachments:ro" -v "${PROJECT_NAME}_service-keys:/keys:ro" personal-agent-ops:local sh -c 'tar -C /attachments -cf /backup/attachments.tar . && tar -C /keys -cf /backup/service-keys.tar .'
printf 'format=1\nproject=%s\ncreated_utc=%s\n' "$PROJECT_NAME" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$STAGE/manifest.txt"
# age provides authenticated encryption. Plaintext staging is private and removed by trap.
tar -C "$STAGE" -cf "$STAGE/payload.tar" databases config files manifest.txt
docker run --rm -v "$STAGE/payload.tar:/payload:ro" personal-agent-ops:local age -r "$RECIPIENT" /payload > "$ARCHIVE.partial"
[ -s "$ARCHIVE.partial" ] || die 'Encrypted archive is empty.'
mv "$ARCHIVE.partial" "$ARCHIVE"
chmod 600 "$ARCHIVE"
echo "Encrypted backup saved: $ARCHIVE"
echo 'Previous running services will now resume. Test recovery in a separate empty deployment.'
