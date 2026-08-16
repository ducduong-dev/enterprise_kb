"""The compiled predicate must carry the whole filter into the index query (INV-2)."""

from __future__ import annotations

from datetime import date

import pytest
from kb_authz.compile import compile_sql, compile_sql_expired
from kb_authz.filters import FilterBuilder
from kb_authz.fixtures import (
    EXTERNAL_BOT,
    GROUP_RETAIL,
    USER_COMPLIANCE_OFFICER,
    USER_NO_GROUPS,
    USER_RETAIL_STAFF,
)
from kb_schemas.api import Facets
from kb_schemas.enums import RetrievalMode

TODAY = date(2026, 8, 10)


@pytest.fixture
def builder() -> FilterBuilder:
    return FilterBuilder(today=TODAY)


def test_sql_binds_every_value_as_a_parameter(builder: FilterBuilder) -> None:
    where, params = compile_sql(
        builder.build(USER_RETAIL_STAFF, facets=Facets(category="regulations.sbv"))
    )
    # No user-controlled or ACL value may appear literally in the SQL text.
    assert GROUP_RETAIL not in where
    assert "regulations.sbv" not in where
    assert params["acl_group_scope"] == [GROUP_RETAIL]
    assert params["acl_category_0"] == "regulations.sbv"


def test_sql_excludes_tombstoned_chunks(builder: FilterBuilder) -> None:
    where, _ = compile_sql(builder.base(USER_RETAIL_STAFF))
    assert "tombstoned = FALSE" in where


def test_sql_requires_group_overlap_for_restricted(builder: FilterBuilder) -> None:
    where, _ = compile_sql(builder.base(USER_RETAIL_STAFF))
    assert "visibility = 'restricted' AND c.allowed_groups && " in where


def test_sql_omits_restricted_branch_when_the_principal_has_no_groups(
    builder: FilterBuilder,
) -> None:
    where, params = compile_sql(builder.base(USER_NO_GROUPS))
    assert "restricted" not in where
    assert "acl_group_scope" not in params


def test_external_bot_sql_is_published_external_only(builder: FilterBuilder) -> None:
    _, params = compile_sql(builder.base(EXTERNAL_BOT))
    assert params["acl_visibilities"] == ["external"]
    assert params["acl_statuses"] == ["published"]


def test_sql_applies_effectivity_at_the_evaluation_date(builder: FilterBuilder) -> None:
    where, params = compile_sql(
        builder.build(
            USER_COMPLIANCE_OFFICER, mode=RetrievalMode.AS_OF, as_of_date=date(2019, 5, 1)
        )
    )
    assert params["acl_effective_on"] == date(2019, 5, 1)
    assert "effective_from <= :acl_effective_on" in where
    assert "effective_to >= :acl_effective_on" in where


def test_sql_alias_and_prefix_are_configurable(builder: FilterBuilder) -> None:
    where, params = compile_sql(builder.base(USER_RETAIL_STAFF), alias="ch", prefix="f1")
    assert "ch.visibility" in where
    assert all(key.startswith("f1_") for key in params)


# ------------------------------------------------- the expired-match probe (M9a, ADR-0030)


def test_the_expired_compiler_keeps_every_access_clause(builder: FilterBuilder) -> None:
    """It exists to turn silence into "that rule ceased on 31/12". It must not become a way to
    ask the corpus a question with the ACL relaxed (INV-2)."""
    resolved = builder.build(USER_RETAIL_STAFF, facets=Facets(category="regulations.sbv"))
    live, live_params = compile_sql(resolved)
    expired, expired_params = compile_sql_expired(resolved)

    assert expired_params == live_params, "same filter, same bound values"
    for clause in live.split(" AND "):
        if "effective_to" in clause or "effective_from" in clause:
            continue
        assert clause in expired, f"the probe dropped an access clause: {clause}"


def test_the_expired_compiler_cannot_return_a_live_chunk(builder: FilterBuilder) -> None:
    """The safety property, and the reason this is its own compiler rather than a flag: a bug
    in the caller can at worst show a reader something that stopped applying — never something
    current, and never something they may not see."""
    where, _ = compile_sql_expired(builder.base(USER_RETAIL_STAFF))

    assert "effective_to IS NOT NULL" in where
    assert "effective_to < :acl_effective_on" in where
    assert "effective_to IS NULL OR" not in where, (
        "a chunk with no end date has not expired and must not be namable as expired"
    )


def test_the_expired_compiler_still_excludes_the_future(builder: FilterBuilder) -> None:
    """A rule that has not started yet is not a rule that ended."""
    where, _ = compile_sql_expired(builder.base(USER_RETAIL_STAFF))
    assert "effective_from IS NULL OR c.effective_from <= :acl_effective_on" in where


def test_a_restricted_document_stays_unnamable_when_it_expires(builder: FilterBuilder) -> None:
    """Expiry does not declassify. A principal with no groups must not learn that a restricted
    instrument existed by being told it ceased."""
    where, params = compile_sql_expired(builder.base(USER_NO_GROUPS))
    assert "allowed_groups" not in where or params.get("acl_group_scope") == []
