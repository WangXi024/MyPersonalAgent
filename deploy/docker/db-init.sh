#!/bin/sh
set -eu
export PGPASSWORD="$(cat /run/secrets/core_postgres_password)"
export APP_DB_PASSWORD="$(cat /run/secrets/app_db_password)"
export TEMPORAL_DB_PASSWORD="$(cat /run/secrets/temporal_db_password)"
export VISIBILITY_DB_PASSWORD="$(cat /run/secrets/visibility_db_password)"
export LITELLM_DB_PASSWORD="$(cat /run/secrets/litellm_db_password)"
psql -h postgres-core -U postgres -d postgres -v ON_ERROR_STOP=1 <<'SQL'
\getenv app_password APP_DB_PASSWORD
\getenv temporal_password TEMPORAL_DB_PASSWORD
\getenv visibility_password VISIBILITY_DB_PASSWORD
\getenv litellm_password LITELLM_DB_PASSWORD
SELECT format('CREATE ROLE app LOGIN PASSWORD %L', :'app_password') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='app') \gexec
SELECT format('CREATE ROLE temporal LOGIN PASSWORD %L', :'temporal_password') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='temporal') \gexec
SELECT format('CREATE ROLE temporal_visibility LOGIN PASSWORD %L', :'visibility_password') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='temporal_visibility') \gexec
SELECT format('CREATE ROLE litellm LOGIN PASSWORD %L', :'litellm_password') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='litellm') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', name, name) FROM (VALUES ('app'), ('temporal'), ('temporal_visibility'), ('litellm')) names(name) WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname=name) \gexec
REVOKE CONNECT ON DATABASE app, temporal, temporal_visibility, litellm FROM PUBLIC;
SQL
export PGPASSWORD="$(cat /run/secrets/memory_postgres_password)"
export MEMORY_DB_PASSWORD="$(cat /run/secrets/memory_db_password)"
psql -h postgres-memory -U postgres -d postgres -v ON_ERROR_STOP=1 <<'SQL'
\getenv memory_password MEMORY_DB_PASSWORD
SELECT format('CREATE ROLE hindsight LOGIN PASSWORD %L', :'memory_password') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='hindsight') \gexec
SELECT 'CREATE DATABASE hindsight OWNER hindsight' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='hindsight') \gexec
REVOKE CONNECT ON DATABASE hindsight FROM PUBLIC;
SQL
psql -h postgres-memory -U postgres -d hindsight -v ON_ERROR_STOP=1 <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgroonga;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL
