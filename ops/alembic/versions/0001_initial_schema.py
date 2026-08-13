"""Initial schema (plan section 4).

Revision ID: 0001
Create Date: 2026-08-10

Notes on the guards created here. The primary enforcement of the invariants is in application
code — the plan is explicit that INV-8 is a code guard, not configuration — but the ones that
can be expressed as constraints are *also* expressed here, because a bug in one service must
not be able to corrupt the registry for all of them:

* one canonical version per document          → partial unique index
* versions are immutable (INV-9)              → trigger rejecting content-column updates
* deletes blocked while under retention       → trigger requiring an explicit purge flag
* publish requires a clear PII state (INV-7)  → trigger on the canonical flag
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql as pg

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

EMBEDDING_DIM = 1024

# create_type=False: the types are created once, explicitly, in upgrade(); otherwise every
# CREATE TABLE that references one tries to create it again.
VISIBILITY = pg.ENUM("external", "internal_all", "restricted", name="visibility", create_type=False)
DOC_STATUS = pg.ENUM(
    "draft", "published", "archived", "expired", name="doc_status", create_type=False
)
DOC_CLASS = pg.ENUM(
    "regulatory",
    "internal_normative",
    "operational",
    "customer_facing",
    name="doc_class",
    create_type=False,
)
REF_TYPE = pg.ENUM(
    "cites",
    "amends",
    "abrogates",
    "implements",
    "consolidates",
    name="ref_type",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("CREATE EXTENSION IF NOT EXISTS ltree")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # Vietnamese search support: unaccent for diacritic-insensitive matching, pg_trgm for
    # fuzzy legal-number lookup ("41/2016/TT-NHNN" typed a dozen ways).
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    for enum in (VISIBILITY, DOC_STATUS, DOC_CLASS, REF_TYPE):
        enum.create(bind, checkfirst=True)

    op.create_table(
        "categories",
        sa.Column("path", sa.Text(), nullable=False),  # ltree, retyped below
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("default_visibility", VISIBILITY, nullable=False, server_default="internal_all"),
        sa.Column(
            "default_allowed_groups",
            pg.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("steward_group", sa.Text()),
        # [OPEN]-3: Compliance to specify. Default false = an unauthorized principal cannot
        # even learn the document exists (INV-10).
        sa.Column(
            "existence_disclosure", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.PrimaryKeyConstraint("path"),
    )
    op.execute("ALTER TABLE categories ALTER COLUMN path TYPE ltree USING path::ltree")
    op.execute("CREATE INDEX ix_categories_path_gist ON categories USING GIST (path)")

    op.create_table(
        "documents",
        sa.Column("id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("legal_number", sa.Text()),
        sa.Column("doc_class", DOC_CLASS, nullable=False),
        sa.Column("category_path", sa.Text(), nullable=False),
        sa.Column("department", sa.Text()),
        sa.Column("visibility", VISIBILITY, nullable=False),
        sa.Column(
            "allowed_groups",
            pg.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("status", DOC_STATUS, nullable=False, server_default="draft"),
        sa.Column("canonical_version_id", pg.UUID(as_uuid=True)),
        sa.Column("review_by", sa.Date()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        # coalesce is load-bearing: array_length('{}') is NULL, and a NULL check constraint
        # passes — the empty-group case is exactly the one this must catch.
        sa.CheckConstraint(
            "visibility <> 'restricted' OR coalesce(array_length(allowed_groups, 1), 0) >= 1",
            name="ck_documents_restricted_needs_groups",
        ),
    )
    op.execute("ALTER TABLE documents ALTER COLUMN category_path TYPE ltree USING category_path::ltree")
    op.create_foreign_key(
        "fk_documents_category", "documents", "categories", ["category_path"], ["path"],
        onupdate="CASCADE",
    )
    # One document per legal number; documents without one are unconstrained.
    op.execute(
        "CREATE UNIQUE INDEX uq_documents_legal_number ON documents (legal_number) "
        "WHERE legal_number IS NOT NULL"
    )
    op.execute("CREATE INDEX ix_documents_category_gist ON documents USING GIST (category_path)")
    op.create_index("ix_documents_status", "documents", ["status"])
    op.create_index("ix_documents_review_by", "documents", ["review_by"])
    op.execute(
        "CREATE INDEX ix_documents_legal_number_trgm ON documents "
        "USING GIN (legal_number gin_trgm_ops)"
    )

    op.create_table(
        "document_versions",
        sa.Column("id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("content_ref", sa.Text()),
        sa.Column("content_hash", sa.Text()),
        sa.Column("source_type", sa.Text(), nullable=False, server_default="upload"),
        sa.Column("author", sa.Text()),
        sa.Column("change_summary", sa.Text()),
        sa.Column("idp_report_ref", sa.Text()),
        sa.Column("pii_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("effective_from", sa.Date()),
        sa.Column("effective_to", sa.Date()),
        sa.Column("is_canonical", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("retention_until", sa.Date()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], name="fk_versions_document"),
        sa.CheckConstraint(
            "pii_status IN ('pending','clear','blocked','overridden')",
            name="ck_versions_pii_status",
        ),
        sa.CheckConstraint(
            "source_type IN ('upload','portal_edit','consolidation')",
            name="ck_versions_source_type",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
            name="ck_versions_effective_range",
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_versions_one_canonical ON document_versions (document_id) "
        "WHERE is_canonical"
    )
    op.create_index("ix_versions_document", "document_versions", ["document_id", "created_at"])
    op.create_index("ix_versions_pii_status", "document_versions", ["pii_status"])
    op.create_index("ix_versions_retention", "document_versions", ["retention_until"])

    # Deferrable: the publish transaction flips the canonical pointer and the version flag
    # together, in either order (INV-5).
    op.execute(
        "ALTER TABLE documents ADD CONSTRAINT fk_documents_canonical_version "
        "FOREIGN KEY (canonical_version_id) REFERENCES document_versions(id) "
        "DEFERRABLE INITIALLY DEFERRED"
    )

    op.create_table(
        "document_refs",
        sa.Column("id", pg.UUID(as_uuid=True), nullable=False),
        # INV-10: edges target documents, never versions — an amendment amends the
        # instrument, not one rendition of it.
        sa.Column("src_document_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("dst_document_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("ref_type", REF_TYPE, nullable=False),
        sa.Column("articles", pg.ARRAY(sa.Integer())),
        sa.Column("detected_by", sa.Text()),
        sa.Column("confirmed_by", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["src_document_id"], ["documents.id"], name="fk_refs_src"),
        sa.ForeignKeyConstraint(["dst_document_id"], ["documents.id"], name="fk_refs_dst"),
        sa.CheckConstraint("src_document_id <> dst_document_id", name="ck_refs_no_self_edge"),
    )
    op.create_index("ix_refs_src", "document_refs", ["src_document_id", "ref_type"])
    op.create_index("ix_refs_dst", "document_refs", ["dst_document_id", "ref_type"])
    op.execute(
        "CREATE UNIQUE INDEX uq_refs_edge ON document_refs "
        "(src_document_id, dst_document_id, ref_type)"
    )

    op.create_table(
        "chunks",
        sa.Column("id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("section_path", sa.Text()),
        sa.Column("citation_label", sa.Text()),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM)),
        # Denormalized ACL + facet columns: the filter must be applicable inside the query
        # with no join (INV-2, ADR-0003).
        sa.Column("visibility", VISIBILITY, nullable=False),
        sa.Column(
            "allowed_groups",
            pg.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("department", sa.Text()),
        sa.Column("category_path", sa.Text(), nullable=False),
        sa.Column("doc_class", DOC_CLASS, nullable=False),
        sa.Column("doc_status", DOC_STATUS, nullable=False),
        sa.Column("effective_from", sa.Date()),
        sa.Column("effective_to", sa.Date()),
        sa.Column("tombstoned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], name="fk_chunks_document"),
        sa.ForeignKeyConstraint(
            ["version_id"], ["document_versions.id"], name="fk_chunks_version"
        ),
        sa.CheckConstraint(
            "visibility <> 'restricted' OR coalesce(array_length(allowed_groups, 1), 0) >= 1",
            name="ck_chunks_restricted_needs_groups",
        ),
    )
    op.execute("ALTER TABLE chunks ALTER COLUMN category_path TYPE ltree USING category_path::ltree")
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    # The ACL predicate's shape: visibility+status first, then the group overlap.
    op.create_index("ix_chunks_acl", "chunks", ["visibility", "doc_status"])
    op.execute("CREATE INDEX ix_chunks_allowed_groups ON chunks USING GIN (allowed_groups)")
    op.create_index("ix_chunks_department", "chunks", ["department"])
    op.create_index("ix_chunks_effective", "chunks", ["effective_from", "effective_to"])
    op.execute("CREATE INDEX ix_chunks_category_gist ON chunks USING GIST (category_path)")
    op.create_index("ix_chunks_version", "chunks", ["version_id"])
    op.create_index("ix_chunks_document", "chunks", ["document_id"])
    # Serving lookups only ever touch live chunks.
    op.execute("CREATE INDEX ix_chunks_live ON chunks (document_id) WHERE NOT tombstoned")

    op.create_table(
        "review_tasks",
        sa.Column("id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("task_type", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="open"),
        sa.Column("assignee_group", sa.Text()),
        sa.Column("payload", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("decision", sa.Text()),
        sa.Column("decided_by", sa.Text()),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["version_id"], ["document_versions.id"], name="fk_tasks_version"
        ),
        sa.CheckConstraint(
            "task_type IN ('idp_review','identity_review','merge_review','impact_review',"
            "'pii_override')",
            name="ck_tasks_type",
        ),
        sa.CheckConstraint(
            "state IN ('open','claimed','decided','cancelled')", name="ck_tasks_state"
        ),
        # Four-eyes and PII overrides are only meaningful with an attributable decider.
        sa.CheckConstraint(
            "state <> 'decided' OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_tasks_decided_attribution",
        ),
    )
    op.create_index("ix_tasks_queue", "review_tasks", ["assignee_group", "state", "task_type"])
    op.create_index("ix_tasks_version", "review_tasks", ["version_id"])

    op.create_table(
        "graph_serving",
        sa.Column("src_document_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("dst_document_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("ref_type", REF_TYPE, nullable=False),
        sa.Column("dst_canonical_version_id", pg.UUID(as_uuid=True)),
        sa.Column("dst_summary", sa.Text()),
        # The target's ACL travels with the edge so expansion is filtered by the same
        # predicate as the main query (INV-10).
        sa.Column("dst_visibility", VISIBILITY, nullable=False),
        sa.Column(
            "dst_allowed_groups",
            pg.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("dst_effective_from", sa.Date()),
        sa.Column("articles", pg.ARRAY(sa.Integer())),
        sa.PrimaryKeyConstraint("src_document_id", "dst_document_id", "ref_type"),
    )
    op.create_index("ix_graph_serving_dst", "graph_serving", ["dst_document_id"])
    op.execute(
        "CREATE INDEX ix_graph_serving_groups ON graph_serving USING GIN (dst_allowed_groups)"
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("on_behalf_of", sa.Text()),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("object_ref", pg.JSONB()),
        sa.Column("resolved_filter", pg.JSONB()),
        sa.Column("detail", pg.JSONB()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_ts", "audit_log", ["ts"])
    op.create_index("ix_audit_actor_ts", "audit_log", ["actor", "ts"])
    op.create_index("ix_audit_action_ts", "audit_log", ["action", "ts"])
    op.execute("CREATE INDEX ix_audit_object ON audit_log USING GIN (object_ref jsonb_path_ops)")

    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("topic", sa.String(length=128), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.PrimaryKeyConstraint("id"),
    )
    # The indexer's claim query: oldest unprocessed first.
    op.execute(
        "CREATE INDEX ix_outbox_unprocessed ON outbox (id) WHERE processed_at IS NULL"
    )

    _create_guards()


def _create_guards() -> None:
    # INV-9: versions are immutable. Only the mutable-by-design columns may change; content,
    # authorship and provenance may not.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION kb_versions_immutable() RETURNS trigger AS $$
        BEGIN
            IF NEW.document_id IS DISTINCT FROM OLD.document_id
               OR NEW.content_ref IS DISTINCT FROM OLD.content_ref
               OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
               OR NEW.source_type IS DISTINCT FROM OLD.source_type
               OR NEW.author IS DISTINCT FROM OLD.author
               OR NEW.idp_report_ref IS DISTINCT FROM OLD.idp_report_ref
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'document_versions rows are immutable (INV-9): %', OLD.id
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_versions_immutable BEFORE UPDATE ON document_versions "
        "FOR EACH ROW EXECUTE FUNCTION kb_versions_immutable()"
    )

    # INV-9: deletion requires an elapsed retention date *and* an explicit purge flag set by
    # the purge job, so no ordinary code path can delete a version by accident.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION kb_versions_retention_guard() RETURNS trigger AS $$
        BEGIN
            IF coalesce(current_setting('kb.purge_authorized', true), 'off') <> 'on' THEN
                RAISE EXCEPTION 'version deletion requires an authorized purge (INV-9): %',
                    OLD.id USING ERRCODE = 'restrict_violation';
            END IF;
            IF OLD.retention_until IS NULL OR OLD.retention_until > CURRENT_DATE THEN
                RAISE EXCEPTION 'version % is under retention hold until %', OLD.id,
                    OLD.retention_until USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_versions_retention BEFORE DELETE ON document_versions "
        "FOR EACH ROW EXECUTE FUNCTION kb_versions_retention_guard()"
    )

    # INV-7 backstop: nothing becomes canonical while PII is pending or blocked. The publish
    # service checks this first; this makes the failure impossible rather than unlikely.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION kb_pii_publish_guard() RETURNS trigger AS $$
        BEGIN
            IF NEW.is_canonical AND NEW.pii_status NOT IN ('clear', 'overridden') THEN
                RAISE EXCEPTION 'version % cannot be canonical with pii_status=% (INV-7)',
                    NEW.id, NEW.pii_status USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_versions_pii_guard BEFORE INSERT OR UPDATE ON document_versions "
        "FOR EACH ROW EXECUTE FUNCTION kb_pii_publish_guard()"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION kb_touch_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_documents_touch BEFORE UPDATE ON documents "
        "FOR EACH ROW EXECUTE FUNCTION kb_touch_updated_at()"
    )


def downgrade() -> None:
    for trigger, table in (
        ("trg_versions_immutable", "document_versions"),
        ("trg_versions_retention", "document_versions"),
        ("trg_versions_pii_guard", "document_versions"),
        ("trg_documents_touch", "documents"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    for fn in (
        "kb_versions_immutable",
        "kb_versions_retention_guard",
        "kb_pii_publish_guard",
        "kb_touch_updated_at",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {fn}()")

    op.execute("ALTER TABLE documents DROP CONSTRAINT IF EXISTS fk_documents_canonical_version")
    for table in (
        "outbox",
        "audit_log",
        "graph_serving",
        "review_tasks",
        "chunks",
        "document_refs",
        "document_versions",
        "documents",
        "categories",
    ):
        op.drop_table(table)

    bind = op.get_bind()
    for enum in (REF_TYPE, DOC_CLASS, DOC_STATUS, VISIBILITY):
        enum.drop(bind, checkfirst=True)
