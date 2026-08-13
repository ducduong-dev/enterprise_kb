#!/bin/bash
# Dev convenience: Temporal and Keycloak get their own databases in the same instance.
# In production these are separate clusters with separate credentials.
set -euo pipefail

for db in $(echo "${POSTGRES_MULTIPLE_DATABASES:-}" | tr ',' ' '); do
  echo "creating database ${db}"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-SQL
    SELECT 'CREATE DATABASE ${db}'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${db}')\gexec
    GRANT ALL PRIVILEGES ON DATABASE ${db} TO ${POSTGRES_USER};
SQL
done
