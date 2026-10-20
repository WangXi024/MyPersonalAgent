#!/bin/sh
set -eu
for database in temporal temporal_visibility; do
    if [ "$database" = temporal ]; then
        export SQL_PASSWORD="$(cat /run/secrets/temporal_db_password)"
        schema=temporal
    else
        export SQL_PASSWORD="$(cat /run/secrets/visibility_db_password)"
        schema=visibility
    fi
    # The pinned tool's setup is idempotent and never downgrades an existing schema.
    temporal-sql-tool --plugin postgres12 --ep postgres-core -p 5432 -u "$database" --db "$database" setup-schema -v 0.0
    temporal-sql-tool --plugin postgres12 --ep postgres-core -p 5432 -u "$database" --db "$database" update-schema -d "/etc/temporal/schema/postgresql/v12/$schema/versioned"
done
