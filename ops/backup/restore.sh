#!/usr/bin/env bash
#
# Restore a backup into a named database, then reindex.
#
# The order matters and is the whole point of having this as a script rather than a runbook
# paragraph: Postgres first, object storage second, keyword index *rebuilt* last. Restoring a
# keyword index from a backup would reintroduce whatever it held at backup time, including
# chunks the registry has since tombstoned — canonical-only serving (INV-6) would be violated
# by the recovery procedure itself.
#
# Usage:
#   ops/backup/restore.sh /var/backups/kb/20260811T020000Z kb_restored
#
# The target database is created if absent. It is never the live one unless you name it.

set -euo pipefail

SOURCE="${1:?usage: restore.sh <backup-dir> <target-db>}"
TARGET_DB="${2:?usage: restore.sh <backup-dir> <target-db>}"

PG_CONTAINER="${KB_PG_DOCKER_CONTAINER:-}"
DB_HOST="${KB_DB_HOST:-localhost}"
DB_PORT="${KB_DB_PORT:-5432}"
DB_USER="${KB_DB_USER:-kb}"
export PGPASSWORD="${KB_DB_PASSWORD:-kb}"

[ -f "${SOURCE}/postgres.dump" ] || { echo "no postgres.dump in ${SOURCE}" >&2; exit 1; }

echo "restoring ${SOURCE} into ${TARGET_DB}"

# `--no-owner` so a restore into a recovery cluster does not fail on roles that only exist in
# production; `--exit-on-error` because a partially restored registry is worse than none.
#
# `TEMPLATE template0` matters and is not boilerplate: the ParadeDB image installs pg_search
# into template1, so a database created from the default template already contains the
# `paradedb` schema — and the dump, which also creates it, fails halfway through. The drill
# found this; a restore during an incident would have found it instead.
if [ -n "${PG_CONTAINER}" ]; then
    docker exec -e PGPASSWORD -i "${PG_CONTAINER}" \
        psql --username "${DB_USER}" --dbname postgres --quiet \
        --command "DROP DATABASE IF EXISTS ${TARGET_DB}"
    docker exec -e PGPASSWORD -i "${PG_CONTAINER}" \
        psql --username "${DB_USER}" --dbname postgres --quiet \
        --command "CREATE DATABASE ${TARGET_DB} TEMPLATE template0"
    docker exec -e PGPASSWORD -i "${PG_CONTAINER}" \
        pg_restore --username "${DB_USER}" --dbname "${TARGET_DB}" --no-owner --exit-on-error \
        < "${SOURCE}/postgres.dump"
else
    psql --host "${DB_HOST}" --port "${DB_PORT}" --username "${DB_USER}" --dbname postgres \
         --quiet --command "DROP DATABASE IF EXISTS ${TARGET_DB}"
    psql --host "${DB_HOST}" --port "${DB_PORT}" --username "${DB_USER}" --dbname postgres \
         --quiet --command "CREATE DATABASE ${TARGET_DB} TEMPLATE template0"
    pg_restore --host "${DB_HOST}" --port "${DB_PORT}" --username "${DB_USER}" \
               --dbname "${TARGET_DB}" --no-owner --exit-on-error --jobs 4 \
               "${SOURCE}/postgres.dump"
fi

echo "  postgres restored"

# --- Object storage --------------------------------------------------------------------
for bucket in "${KB_STORAGE_BUCKET_ORIGINALS:-kb-originals}" "${KB_STORAGE_BUCKET_DERIVED:-kb-derived}"; do
    [ -d "${SOURCE}/${bucket}" ] || continue
    if command -v mc >/dev/null 2>&1 && [ -n "${KB_MC_ALIAS:-}" ]; then
        mc mb --ignore-existing "${KB_MC_ALIAS}/${bucket}"
        mc mirror --overwrite "${SOURCE}/${bucket}" "${KB_MC_ALIAS}/${bucket}"
        echo "  ${bucket} restored"
    elif command -v aws >/dev/null 2>&1; then
        aws s3 sync "${SOURCE}/${bucket}" "s3://${bucket}"
        echo "  ${bucket} restored"
    else
        echo "  ${bucket}: SKIPPED — no mc alias or aws cli" >&2
    fi
done

# --- Keyword index ----------------------------------------------------------------------
#
# Rebuilt, never restored. With pg_search the BM25 index came back with the dump and there is
# nothing to do; the reindex script is a no-op there and says so, which is one of the reasons
# that engine was chosen (ADR-0021).
echo "  keyword index: rebuild with 'KB_DB_NAME=${TARGET_DB} make reindex'"

echo "restore complete into ${TARGET_DB}"
