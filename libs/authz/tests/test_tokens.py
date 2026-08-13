"""Claim → principal mapping, including RFC 8693 delegation (INV-3)."""

from __future__ import annotations

import pytest
from kb_authz.tokens import principal_from_claims
from kb_common.config import IdentitySettings
from kb_common.errors import AuthenticationError
from kb_schemas.enums import PrincipalKind

CFG = IdentitySettings(issuer="http://kc/realms/kb", audience="kb-platform")


def claims(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "sub": "u-1",
        "iss": CFG.issuer,
        "aud": CFG.audience,
        "jti": "t-1",
        "preferred_username": "nguyen.van.a",
        "groups": ["/dept/retail", "/branch/hcm"],
        "department": "retail",
        "realm_access": {"roles": ["kb-steward"]},
        "scope": "openid profile",
        "azp": "kb-portal",
    }
    base.update(overrides)
    return base


def test_user_claims_map_to_a_user_principal() -> None:
    p = principal_from_claims(claims(), CFG)
    assert p.kind is PrincipalKind.USER
    # Keycloak group paths are normalized to the names stored in `allowed_groups`.
    assert p.groups == frozenset({"dept/retail", "branch/hcm"})
    assert p.roles == frozenset({"kb-steward"})
    assert p.department == "retail"


def test_delegated_token_becomes_bot_acting_for_user() -> None:
    p = principal_from_claims(claims(act={"sub": "svc-internal-bot"}), CFG)
    assert p.kind is PrincipalKind.INTERNAL_BOT
    assert p.subject == "svc-internal-bot"
    assert p.on_behalf_of is not None
    assert p.on_behalf_of.subject == "u-1"
    assert p.effective_user is p.on_behalf_of
    assert p.audit_on_behalf_of == "u-1"


def test_external_bot_client_is_recognised_by_client_id() -> None:
    p = principal_from_claims(
        claims(
            sub="svc-ext",
            azp=CFG.external_bot_client_id,
            preferred_username="service-account-kb-external-bot",
            scope="kb.retrieve.external",
        ),
        CFG,
    )
    assert p.kind is PrincipalKind.EXTERNAL_BOT
    assert p.has_scope("kb.retrieve.external")
    assert p.groups == frozenset()  # never inherits group claims


def test_service_account_is_recognised() -> None:
    p = principal_from_claims(
        claims(sub="svc-1", preferred_username="service-account-kb-eval", azp="kb-eval"), CFG
    )
    assert p.kind is PrincipalKind.SERVICE


def test_token_without_subject_is_rejected() -> None:
    bad = claims()
    del bad["sub"]
    with pytest.raises(AuthenticationError):
        principal_from_claims(bad, CFG)


def test_delegation_may_not_be_chained() -> None:
    from kb_authz.principal import Principal

    user = Principal(subject="u-1", kind=PrincipalKind.USER)
    bot = Principal(subject="b-1", kind=PrincipalKind.INTERNAL_BOT, on_behalf_of=user)
    with pytest.raises(ValueError, match=r"human user|chained"):
        Principal(subject="b-2", kind=PrincipalKind.INTERNAL_BOT, on_behalf_of=bot)


def test_only_the_internal_bot_may_delegate() -> None:
    from kb_authz.principal import Principal

    user = Principal(subject="u-1", kind=PrincipalKind.USER)
    with pytest.raises(ValueError, match="internal bot"):
        Principal(subject="svc", kind=PrincipalKind.SERVICE, on_behalf_of=user)
