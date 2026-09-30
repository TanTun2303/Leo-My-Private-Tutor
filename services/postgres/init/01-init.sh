#!/usr/bin/env bash
# Runs once on an empty data volume (docker-entrypoint-initdb.d).
# Creates the openwebui and leo_memory databases, their owner roles, the
# grant-less leo_reconcile login role, and enables pgvector in both databases.
set -euo pipefail

: "${OPENWEBUI_DB_PASSWORD:?}" "${LEO_MEMORY_DB_PASSWORD:?}" "${LEO_RECONCILE_DB_PASSWORD:?}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v owui_pw="$OPENWEBUI_DB_PASSWORD" \
  -v mem_pw="$LEO_MEMORY_DB_PASSWORD" \
  -v rec_pw="$LEO_RECONCILE_DB_PASSWORD" <<'SQL'
CREATE ROLE openwebui     LOGIN PASSWORD :'owui_pw';
CREATE ROLE leo_memory    LOGIN PASSWORD :'mem_pw';
CREATE ROLE leo_reconcile LOGIN PASSWORD :'rec_pw';

CREATE DATABASE openwebui  OWNER openwebui;
CREATE DATABASE leo_memory OWNER leo_memory;

REVOKE ALL ON DATABASE openwebui  FROM PUBLIC;
REVOKE ALL ON DATABASE leo_memory FROM PUBLIC;
GRANT CONNECT ON DATABASE openwebui TO leo_reconcile;

\connect openwebui
CREATE EXTENSION IF NOT EXISTS vector;
\connect leo_memory
CREATE EXTENSION IF NOT EXISTS vector;
SQL
