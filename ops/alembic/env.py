"""Alembic environment.

The URL comes from `KB_DB_*` settings, never from alembic.ini, so migrations run against the
same database the services use in every environment.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from kb_common.config import get_settings
from kb_schemas.orm import metadata as target_metadata
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().db.url.replace("%", "%%"))


#: Objects the database owns rather than the model: created by an extension or by a
#: deployment step, and therefore not drift when `alembic check` finds them.
#:
#: The keyword engine's install (`ops/pg_search/install.sql`, ADR-0021) adds a BM25 index and
#: two generated columns that deliberately live outside the migration chain — a deployment
#: without that engine must not carry them. Without this list, `alembic check` on a ParadeDB
#: node would report them as changes to revert, and the first person to believe it would drop
#: the production keyword index.
ENGINE_OWNED_INDEXES = frozenset({"chunks_bm25"})
ENGINE_OWNED_COLUMNS = frozenset({"text_folded", "citation_label_folded"})


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    # Tables the extensions bring with them (PostGIS's `spatial_ref_sys`, pg_search's
    # bookkeeping). Anything reflected that the model has never heard of is not ours.
    if type_ == "table" and reflected and name not in target_metadata.tables:
        return False
    if type_ == "index" and name in ENGINE_OWNED_INDEXES:
        return False
    if type_ == "column" and name in ENGINE_OWNED_COLUMNS:
        return False
    # Alembic cannot see index expressions or partial predicates well enough to autogenerate
    # them; the ones we hand-wrote are owned by the migration, not the model.
    if type_ == "index" and name and name.startswith(("ix_", "uq_")):
        return not reflected or compare_to is not None
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
