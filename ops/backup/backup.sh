#!/usr/bin/env bash
#
# Take a consistent backup of everything the platform cannot rebuild.
#
# Three stores, and only two of them are irreplaceable:
#
#   1. Postgres  — the registry, versions, chunks, edges, audit log. Irreplaceable.
#   2. Object storage — the original uploads and the derived KBDocs. Irreplaceable: an
#      original PDF that only exists in MinIO is the bank's record of what it received.
#   3. The keyword index — rebuildable from Postgres, and therefore *not* backed up. Restoring
#      a stale index alongside a current database would put the two out of step in the one
#      direction nothing detects (INV-6). It is reindexed after a restore instead.
#
# Usage:
#   ops/backup/backup.sh /var/backups/kb            # nightly
#   KB_BACKUP_KEEP=14 ops/backup/backup.sh /backups # with retention
#
# Environment: KB_DB_HOST/PORT/USER/PASSWORD/NAME, and for object storage either
# `mc` configured with an alias in KB_MC_ALIAS, or AWS_* credentials for `aws s3 sync`.
#
# On a workstation where Postgres runs in Docker and the client tools are not installed, set
# KB_PG_DOCKER_CONTAINER to the container name: the dump is then produced inside it and
# streamed out. Same commands, same output — so the drill exercises the real script rather
# than a laptop-only variant of it.

set -euo pipefail

DEST="${1:-./backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${DEST}/${STAMP}"
KEEP="${KB_BACKUP_KEEP:-14}"

PG_CONTAINER="${KB_PG_DOCKER_CONTAINER:-}"
DB_HOST="${KB_DB_HOST:-localhost}"
DB_PORT="${KB_DB_PORT:-5432}"
DB_USER="${KB_DB_USER:-kb}"
DB_NAME="${KB_DB_NAME:-kb}"
BUCKET_ORIGINALS="${KB_STORAGE_BUCKET_ORIGINALS:-kb-originals}"
BUCKET_DERIVED="${KB_STORAGE_BUCKET_DERIVED:-kb-derived}"

mkdir -p "${TARGET}"
echo "backing up to ${TARGET}"

# --- Postgres --------------------------------------------------------------------------
#
# Custom format, compressed, with the large objects. `--serializable-deferrable` so the dump
# is a single point in time even while publishes are committing: a backup that captured a
# document's new chunks but not its canonical pointer would restore into a state the
# publish transaction is designed to make impossible (INV-5).
DUMP_ARGS=(--username "${DB_USER}" --dbname "${DB_NAME}"
           --format=custom --compress=6 --serializable-deferrable)
if [ -n "${PG_CONTAINER}" ]; then
    docker exec -e PGPASSWORD="${KB_DB_PASSWORD:-kb}" -i "${PG_CONTAINER}" \
        pg_dump "${DUMP_ARGS[@]}" > "${TARGET}/postgres.dump"
else
    PGPASSWORD="${KB_DB_PASSWORD:-kb}" pg_dump \
        --host "${DB_HOST}" --port "${DB_PORT}" "${DUMP_ARGS[@]}" \
        --file "${TARGET}/postgres.dump"
fi

echo "  postgres: $(du -h "${TARGET}/postgres.dump" | cut -f1)"

# --- Object storage --------------------------------------------------------------------
if command -v mc >/dev/null 2>&1 && [ -n "${KB_MC_ALIAS:-}" ]; then
    mc mirror --overwrite "${KB_MC_ALIAS}/${BUCKET_ORIGINALS}" "${TARGET}/${BUCKET_ORIGINALS}"
    mc mirror --overwrite "${KB_MC_ALIAS}/${BUCKET_DERIVED}" "${TARGET}/${BUCKET_DERIVED}"
elif command -v aws >/dev/null 2>&1; then
    aws s3 sync "s3://${BUCKET_ORIGINALS}" "${TARGET}/${BUCKET_ORIGINALS}"
    aws s3 sync "s3://${BUCKET_DERIVED}" "${TARGET}/${BUCKET_DERIVED}"
else
    echo "  object storage: SKIPPED — no mc alias or aws cli" >&2
    echo "skipped" > "${TARGET}/object-storage.SKIPPED"
fi

# --- Manifest --------------------------------------------------------------------------
#
# What a restore is checked against. Counts, not checksums: the restore drill compares these
# against the restored database, and a mismatch names which table lost rows.
MANIFEST_SQL="
        SELECT 'documents,' || count(*) FROM documents
        UNION ALL SELECT 'document_versions,' || count(*) FROM document_versions
        UNION ALL SELECT 'chunks,' || count(*) FROM chunks
        UNION ALL SELECT 'canonical,' || count(*) FROM documents WHERE canonical_version_id IS NOT NULL
        UNION ALL SELECT 'document_refs,' || count(*) FROM document_refs
        UNION ALL SELECT 'review_tasks,' || count(*) FROM review_tasks
        UNION ALL SELECT 'audit_log,' || count(*) FROM audit_log
        UNION ALL SELECT 'outbox_unprocessed,' || count(*) FROM outbox WHERE processed_at IS NULL
"
if [ -n "${PG_CONTAINER}" ]; then
    docker exec -e PGPASSWORD="${KB_DB_PASSWORD:-kb}" -i "${PG_CONTAINER}" \
        psql --username "${DB_USER}" --dbname "${DB_NAME}" \
        --tuples-only --no-align --quiet --command "${MANIFEST_SQL}" > "${TARGET}/manifest.csv"
else
    PGPASSWORD="${KB_DB_PASSWORD:-kb}" psql \
        --host "${DB_HOST}" --port "${DB_PORT}" --username "${DB_USER}" --dbname "${DB_NAME}" \
        --tuples-only --no-align --quiet --command "${MANIFEST_SQL}" > "${TARGET}/manifest.csv"
fi

{
    echo "taken_at=${STAMP}"
    echo "database=${DB_NAME}@${DB_HOST}:${DB_PORT}"
    echo "pg_dump_version=$(if [ -n "${PG_CONTAINER}" ]; then docker exec "${PG_CONTAINER}" pg_dump --version; else pg_dump --version; fi)"
} > "${TARGET}/metadata.txt"

cat "${TARGET}/manifest.csv" | sed 's/^/  /'

# --- Retention -------------------------------------------------------------------------
#
# Deliberately count-based, not age-based: a backup job that has been failing for a month
# should not also delete the last good backup because it is old.
mapfile -t OLD < <(ls -1d "${DEST}"/*/ 2>/dev/null | sort -r | tail -n +$((KEEP + 1)))
for dir in "${OLD[@]:-}"; do
    [ -n "${dir}" ] || continue
    echo "  pruning ${dir}"
    rm -rf "${dir}"
done

echo "backup complete: ${TARGET}"
