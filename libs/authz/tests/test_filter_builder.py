"""FilterBuilder unit tests — M0 acceptance criterion.

Coverage contract for this file: every principal *kind*, every fixture principal, and every
way a request could try to widen the server-side filter (INV-2/3/4).
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta

import pytest
from kb_authz.filters import FilterBuilder, ResolvedFilter
from kb_authz.fixtures import (
    ALL_PRINCIPALS,
    CANARY_GROUPS,
    EXTERNAL_BOT,
    GROUP_LEGAL,
    GROUP_RETAIL,
    INTERNAL_BOT_OBO_RETAIL,
    INTERNAL_BOT_SOLO,
    SERVICE_EVAL_HARNESS,
    SERVICE_INDEXER,
    USER_ADMIN,
    USER_COMPLIANCE_OFFICER,
    USER_LEGAL_COUNSEL,
    USER_NO_GROUPS,
    USER_RETAIL_STAFF,
    ZERO_VISIBILITY_PRINCIPALS,
)
from kb_common.errors import AuthzError, PolicyViolation
from kb_schemas.api import Facets, RetrieveRequest
from kb_schemas.enums import DocClass, DocStatus, PrincipalKind, RetrievalMode, Visibility
from pydantic import ValidationError

TODAY = date(2026, 8, 10)


@pytest.fixture
def builder() -> FilterBuilder:
    return FilterBuilder(today=TODAY)


# --------------------------------------------------------------------- per principal kind


def test_user_without_groups_cannot_reach_restricted(builder: FilterBuilder) -> None:
    f = builder.base(USER_NO_GROUPS)
    assert f.visibilities == frozenset({Visibility.EXTERNAL, Visibility.INTERNAL_ALL})
    assert f.group_scope == frozenset()
    assert f.statuses == frozenset({DocStatus.PUBLISHED})


def test_user_with_groups_gets_exactly_their_groups(builder: FilterBuilder) -> None:
    f = builder.base(USER_RETAIL_STAFF)
    assert Visibility.RESTRICTED in f.visibilities
    assert f.group_scope == frozenset({GROUP_RETAIL})


def test_admin_role_grants_no_extra_document_visibility(builder: FilterBuilder) -> None:
    admin = builder.base(USER_ADMIN)
    assert admin.visibilities == builder.base(USER_LEGAL_COUNSEL).visibilities
    assert GROUP_LEGAL not in admin.group_scope


def test_external_bot_scope_is_bound_server_side(builder: FilterBuilder) -> None:
    f = builder.base(EXTERNAL_BOT)
    assert f.visibilities == frozenset({Visibility.EXTERNAL})  # INV-4
    assert f.statuses == frozenset({DocStatus.PUBLISHED})
    assert f.group_scope == frozenset()


def test_internal_bot_alone_has_zero_visibility(builder: FilterBuilder) -> None:
    with pytest.raises(PolicyViolation) as exc:  # INV-3
        builder.base(INTERNAL_BOT_SOLO)
    assert exc.value.detail["invariant"] == "INV-3"


def test_internal_bot_obo_matches_the_user_and_names_both(builder: FilterBuilder) -> None:
    bot = builder.base(INTERNAL_BOT_OBO_RETAIL)
    user = builder.base(USER_RETAIL_STAFF)
    assert (bot.visibilities, bot.group_scope, bot.statuses) == (
        user.visibilities,
        user.group_scope,
        user.statuses,
    )
    assert bot.principal_ref == INTERNAL_BOT_OBO_RETAIL.subject
    assert bot.on_behalf_of == USER_RETAIL_STAFF.subject
    assert bot.principal_kind is PrincipalKind.INTERNAL_BOT


def test_service_account_never_sees_restricted(builder: FilterBuilder) -> None:
    f = builder.base(SERVICE_EVAL_HARNESS)
    assert Visibility.RESTRICTED not in f.visibilities
    assert f.group_scope == frozenset()


def test_write_only_service_account_cannot_retrieve(builder: FilterBuilder) -> None:
    with pytest.raises(AuthzError):
        builder.base(SERVICE_INDEXER)


@pytest.mark.parametrize("name", sorted(ALL_PRINCIPALS))
def test_every_fixture_principal_resolves_or_is_refused(builder: FilterBuilder, name: str) -> None:
    principal = ALL_PRINCIPALS[name]
    if name in ZERO_VISIBILITY_PRINCIPALS:
        with pytest.raises(AuthzError):
            builder.base(principal)
        return
    f = builder.base(principal)
    assert f.visibilities, "a resolved filter must never admit everything by being empty"
    assert f.statuses <= {DocStatus.PUBLISHED}, "default path serves canonical published only"
    assert not (f.group_scope & CANARY_GROUPS), "no fixture principal may unlock a canary"


@pytest.mark.parametrize("name", sorted(ALL_PRINCIPALS))
def test_no_principal_can_reach_canary_groups(builder: FilterBuilder, name: str) -> None:
    principal = ALL_PRINCIPALS[name]
    if name in ZERO_VISIBILITY_PRINCIPALS:
        pytest.skip("principal resolves to no filter at all")
    assert not (builder.base(principal).group_scope & CANARY_GROUPS)


# ------------------------------------------------------------------------- widen attempts


def test_request_schema_cannot_express_widening() -> None:
    """The strongest guarantee is representational: there is no field to widen with."""
    for hostile in ("visibility", "allowed_groups", "principal", "group_scope", "statuses"):
        with pytest.raises(ValidationError):
            RetrieveRequest(query="x", **{hostile: ["anything"]})
        with pytest.raises(ValidationError):
            Facets(**{hostile: "anything"})


def test_category_facet_outside_permitted_subtree_is_rejected(builder: FilterBuilder) -> None:
    scoped = builder.build(USER_RETAIL_STAFF, facets=Facets(category="regulations.sbv"))
    with pytest.raises(PolicyViolation) as exc:
        builder.narrow(scoped, Facets(category="hr.compensation"))
    assert exc.value.detail["invariant"] == "INV-2"


def test_category_facet_may_go_deeper(builder: FilterBuilder) -> None:
    scoped = builder.build(USER_RETAIL_STAFF, facets=Facets(category="regulations.sbv"))
    deeper = builder.narrow(scoped, Facets(category="regulations.sbv.capital"))
    assert deeper.category_prefixes == frozenset({"regulations.sbv.capital"})


def test_climbing_to_a_parent_category_keeps_the_tighter_scope(builder: FilterBuilder) -> None:
    """Narrowing is set intersection: asking for the parent cannot re-admit siblings.

    The intersection is non-empty, so this is not a widening attempt — it is a no-op. Only an
    empty intersection (a request pointing outside every permitted subtree) raises.
    """
    scoped = builder.build(USER_RETAIL_STAFF, facets=Facets(category="regulations.sbv.capital"))
    climbed = builder.narrow(scoped, Facets(category="regulations"))
    assert climbed.category_prefixes == frozenset({"regulations.sbv.capital"})


def test_sibling_prefix_is_not_treated_as_a_subtree(builder: FilterBuilder) -> None:
    """`regulations.sbvx` must not be admitted by a `regulations.sbv` scope."""
    scoped = builder.build(USER_RETAIL_STAFF, facets=Facets(category="regulations.sbv"))
    with pytest.raises(PolicyViolation):
        builder.narrow(scoped, Facets(category="regulations.sbvx"))


def test_malformed_category_is_rejected(builder: FilterBuilder) -> None:
    for bad in ("../etc", "regulations..sbv", "regulations.*", "'; DROP TABLE chunks--"):
        with pytest.raises(PolicyViolation):
            builder.build(USER_RETAIL_STAFF, facets=Facets(category=bad))


def test_department_facet_cannot_be_swapped(builder: FilterBuilder) -> None:
    scoped = builder.build(USER_RETAIL_STAFF, facets=Facets(department="retail"))
    assert scoped.departments == frozenset({"retail"})
    with pytest.raises(PolicyViolation):
        builder.narrow(scoped, Facets(department="legal"))


def test_doc_class_facet_cannot_be_swapped_or_invented(builder: FilterBuilder) -> None:
    scoped = builder.build(USER_RETAIL_STAFF, facets=Facets(doc_class="operational"))
    assert scoped.doc_classes == frozenset({DocClass.OPERATIONAL})
    with pytest.raises(PolicyViolation):
        builder.narrow(scoped, Facets(doc_class="regulatory"))
    with pytest.raises(PolicyViolation):
        builder.build(USER_RETAIL_STAFF, facets=Facets(doc_class="everything"))


def test_date_facets_only_tighten(builder: FilterBuilder) -> None:
    scoped = builder.build(
        USER_RETAIL_STAFF, facets=Facets(date_from=date(2020, 1, 1), date_to=date(2024, 1, 1))
    )
    widened = builder.narrow(scoped, Facets(date_from=date(2000, 1, 1), date_to=date(2030, 1, 1)))
    assert widened.issued_from == date(2020, 1, 1)
    assert widened.issued_to == date(2024, 1, 1)


def test_narrowing_is_monotonic(builder: FilterBuilder) -> None:
    scoped = builder.build(USER_LEGAL_COUNSEL, facets=Facets(department="legal"))
    twice = builder.narrow(scoped, Facets(department="legal"))
    assert twice == scoped


# ------------------------------------------------------------------- point-in-time access


def test_as_of_requires_the_archive_role(builder: FilterBuilder) -> None:
    with pytest.raises(PolicyViolation) as exc:
        builder.build(USER_RETAIL_STAFF, mode=RetrievalMode.AS_OF, as_of_date=date(2020, 1, 1))
    assert exc.value.detail["invariant"] == "INV-6"


def test_as_of_with_archive_role_reaches_archived_versions(builder: FilterBuilder) -> None:
    f = builder.build(
        USER_COMPLIANCE_OFFICER, mode=RetrievalMode.AS_OF, as_of_date=date(2020, 1, 1)
    )
    assert f.mode is RetrievalMode.AS_OF
    assert f.effective_on == date(2020, 1, 1)
    assert DocStatus.ARCHIVED in f.statuses
    # Point-in-time must not relax who may see the document, only when.
    assert f.visibilities == builder.base(USER_COMPLIANCE_OFFICER).visibilities
    assert f.group_scope == builder.base(USER_COMPLIANCE_OFFICER).group_scope


def test_external_bot_cannot_query_the_archive(builder: FilterBuilder) -> None:
    with pytest.raises(PolicyViolation) as exc:
        builder.build(EXTERNAL_BOT, mode=RetrievalMode.AS_OF, as_of_date=date(2020, 1, 1))
    assert exc.value.detail["invariant"] == "INV-4"


def test_as_of_in_the_future_is_rejected(builder: FilterBuilder) -> None:
    with pytest.raises(PolicyViolation):
        builder.build(
            USER_COMPLIANCE_OFFICER,
            mode=RetrievalMode.AS_OF,
            as_of_date=TODAY + timedelta(days=1),
        )


def test_as_of_mode_without_date_is_rejected(builder: FilterBuilder) -> None:
    with pytest.raises(PolicyViolation):
        builder.build(USER_COMPLIANCE_OFFICER, mode=RetrievalMode.AS_OF)


def test_internal_bot_cannot_borrow_a_role_the_user_lacks(builder: FilterBuilder) -> None:
    from kb_authz.principal import Principal, Role

    bot = Principal(
        subject="svc-internal-bot",
        kind=PrincipalKind.INTERNAL_BOT,
        roles=frozenset({Role.ARCHIVE_READER}),  # the bot holds it, the user does not
        on_behalf_of=USER_RETAIL_STAFF,
    )
    with pytest.raises(PolicyViolation):
        builder.build(bot, mode=RetrievalMode.AS_OF, as_of_date=date(2020, 1, 1))


# ------------------------------------------------------------------------- filter identity


def test_resolved_filter_is_immutable(builder: FilterBuilder) -> None:
    f = builder.base(USER_RETAIL_STAFF)
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.visibilities = frozenset(Visibility)  # type: ignore[misc]


def test_filter_id_is_deterministic_and_discriminating(builder: FilterBuilder) -> None:
    a = builder.base(USER_RETAIL_STAFF)
    b = FilterBuilder(today=TODAY).base(USER_RETAIL_STAFF)
    assert a.filter_id == b.filter_id
    assert a.filter_id != builder.base(USER_LEGAL_COUNSEL).filter_id
    assert (
        a.filter_id
        != builder.build(USER_RETAIL_STAFF, facets=Facets(department="retail")).filter_id
    )


def test_audit_payload_carries_the_whole_filter(builder: FilterBuilder) -> None:
    payload = builder.base(INTERNAL_BOT_OBO_RETAIL).audit_payload()
    assert payload["on_behalf_of"] == USER_RETAIL_STAFF.subject
    assert payload["principal"] == INTERNAL_BOT_OBO_RETAIL.subject
    assert payload["visibilities"] == ["external", "internal_all", "restricted"]
    assert payload["filter_id"]


def test_empty_filter_cannot_be_constructed() -> None:
    """An empty visibility set must never be mistakable for "no filter"."""
    with pytest.raises(AuthzError):
        ResolvedFilter(
            principal_ref="u-x",
            on_behalf_of=None,
            principal_kind=PrincipalKind.USER,
            visibilities=frozenset(),
            group_scope=frozenset(),
            statuses=frozenset({DocStatus.PUBLISHED}),
            departments=None,
            category_prefixes=None,
            doc_classes=None,
            mode=RetrievalMode.CURRENT,
            effective_on=TODAY,
        )


def test_restricted_without_group_scope_cannot_be_constructed() -> None:
    with pytest.raises(AuthzError):
        ResolvedFilter(
            principal_ref="u-x",
            on_behalf_of=None,
            principal_kind=PrincipalKind.USER,
            visibilities=frozenset({Visibility.RESTRICTED}),
            group_scope=frozenset(),
            statuses=frozenset({DocStatus.PUBLISHED}),
            departments=None,
            category_prefixes=None,
            doc_classes=None,
            mode=RetrievalMode.CURRENT,
            effective_on=TODAY,
        )
