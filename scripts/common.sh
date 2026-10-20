#!/bin/sh
set -eu
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"
ENV_FILE=${ENV_FILE:-.env}
PROJECT_NAME=${COMPOSE_PROJECT_NAME:-personal-agent}
PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
die() { echo "$*" >&2; exit 1; }
lock_maintenance() {
    mkdir -p .runtime
    mkdir .runtime/maintenance.lock 2>/dev/null || die 'Another maintenance operation is active. If it crashed, inspect stopped services before manually removing .runtime/maintenance.lock.'
}
unlock_maintenance() { rmdir .runtime/maintenance.lock; }
docker info >/dev/null 2>&1 || die 'Docker Engine is not running or not accessible.'
docker compose version >/dev/null 2>&1 || die 'Docker Compose V2 or newer is required.'
compose() {
    if [ -f .runtime/compose.override.json ]; then
        docker compose --project-name "$PROJECT_NAME" --env-file "$ENV_FILE" -f compose.yaml -f .runtime/compose.override.json "$@"
    else
        docker compose --project-name "$PROJECT_NAME" --env-file "$ENV_FILE" -f compose.yaml "$@"
    fi
}
