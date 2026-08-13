"""The 14 fixture principals.

One source of truth, shared by unit tests, the CI ACL sweep (M2 AC) and the eval harness, and
mirrored by the users in `ops/keycloak-realm/kb-realm.json` so an end-to-end run exercises the
same identities as the unit tests. Adding a principal here means adding it to the realm too.
"""

from __future__ import annotations

from kb_schemas.enums import PrincipalKind

from kb_authz.principal import Principal, Role, Scope

# Group names as they appear in `allowed_groups` (Keycloak paths minus the leading slash).
GROUP_RETAIL = "dept/retail"
GROUP_LEGAL = "dept/legal"
GROUP_COMPLIANCE = "dept/compliance"
GROUP_IT = "dept/it"
GROUP_OPERATIONS = "dept/operations"
GROUP_BRANCH_HCM = "branch/hcm"
#: Deliberately held by nobody in the fixtures — canary documents are ACL'd to it, so any
#: principal that can retrieve a canary has escaped the filter.
GROUP_BOARD_ONLY = "restricted/board"

USER_NO_GROUPS = Principal(
    subject="u-newjoiner",
    kind=PrincipalKind.USER,
    display_name="new.joiner",
    department="retail",
)

USER_RETAIL_STAFF = Principal(
    subject="u-retail-staff",
    kind=PrincipalKind.USER,
    display_name="nguyen.van.a",
    groups=frozenset({GROUP_RETAIL}),
    department="retail",
)

USER_BRANCH_TELLER = Principal(
    subject="u-branch-teller",
    kind=PrincipalKind.USER,
    display_name="tran.thi.b",
    groups=frozenset({GROUP_RETAIL, GROUP_BRANCH_HCM}),
    department="retail",
)

USER_LEGAL_COUNSEL = Principal(
    subject="u-legal-counsel",
    kind=PrincipalKind.USER,
    display_name="le.van.c",
    groups=frozenset({GROUP_LEGAL}),
    department="legal",
    roles=frozenset({Role.LEGAL, Role.ARCHIVE_READER}),
)

USER_COMPLIANCE_OFFICER = Principal(
    subject="u-compliance-officer",
    kind=PrincipalKind.USER,
    display_name="pham.thi.d",
    groups=frozenset({GROUP_COMPLIANCE}),
    department="compliance",
    roles=frozenset({Role.ARCHIVE_READER, Role.PII_OVERRIDER}),
)

USER_IT_ENGINEER = Principal(
    subject="u-it-engineer",
    kind=PrincipalKind.USER,
    display_name="do.van.e",
    groups=frozenset({GROUP_IT}),
    department="it",
)

USER_OPERATIONS_STEWARD = Principal(
    subject="u-ops-steward",
    kind=PrincipalKind.USER,
    display_name="hoang.thi.f",
    groups=frozenset({GROUP_OPERATIONS}),
    department="operations",
    roles=frozenset({Role.STEWARD}),
)

USER_LEGAL_APPROVER = Principal(
    subject="u-legal-approver",
    kind=PrincipalKind.USER,
    display_name="vu.van.g",
    groups=frozenset({GROUP_LEGAL}),
    department="legal",
    roles=frozenset({Role.LEGAL, Role.APPROVER}),
)

#: Platform admin. Deliberately holds no extra *document* visibility: administering the
#: platform is not a reason to read restricted content (INV-2).
USER_ADMIN = Principal(
    subject="u-admin",
    kind=PrincipalKind.USER,
    display_name="platform.admin",
    groups=frozenset({GROUP_IT}),
    department="it",
    roles=frozenset({Role.ADMIN}),
)

#: The internal bot's own service account — must resolve to zero visibility (INV-3).
INTERNAL_BOT_SOLO = Principal(
    subject="svc-internal-bot",
    kind=PrincipalKind.INTERNAL_BOT,
    display_name="kb-internal-bot",
    scopes=frozenset({Scope.RETRIEVE_INTERNAL}),
)

INTERNAL_BOT_OBO_RETAIL = Principal(
    subject="svc-internal-bot",
    kind=PrincipalKind.INTERNAL_BOT,
    display_name="kb-internal-bot",
    scopes=frozenset({Scope.RETRIEVE_INTERNAL}),
    on_behalf_of=USER_RETAIL_STAFF,
)

EXTERNAL_BOT = Principal(
    subject="svc-external-bot",
    kind=PrincipalKind.EXTERNAL_BOT,
    display_name="kb-external-bot",
    scopes=frozenset({Scope.RETRIEVE_EXTERNAL}),
)

SERVICE_EVAL_HARNESS = Principal(
    subject="svc-eval-harness",
    kind=PrincipalKind.SERVICE,
    display_name="kb-eval",
    scopes=frozenset({Scope.RETRIEVE_INTERNAL}),
)

#: Write-only service account: must never be able to read through retrieval.
SERVICE_INDEXER = Principal(
    subject="svc-indexer",
    kind=PrincipalKind.SERVICE,
    display_name="kb-indexer",
    scopes=frozenset({Scope.INDEX_WRITE}),
)

ALL_PRINCIPALS: dict[str, Principal] = {
    "user_no_groups": USER_NO_GROUPS,
    "user_retail_staff": USER_RETAIL_STAFF,
    "user_branch_teller": USER_BRANCH_TELLER,
    "user_legal_counsel": USER_LEGAL_COUNSEL,
    "user_compliance_officer": USER_COMPLIANCE_OFFICER,
    "user_it_engineer": USER_IT_ENGINEER,
    "user_operations_steward": USER_OPERATIONS_STEWARD,
    "user_legal_approver": USER_LEGAL_APPROVER,
    "user_admin": USER_ADMIN,
    "internal_bot_solo": INTERNAL_BOT_SOLO,
    "internal_bot_obo_retail": INTERNAL_BOT_OBO_RETAIL,
    "external_bot": EXTERNAL_BOT,
    "service_eval_harness": SERVICE_EVAL_HARNESS,
    "service_indexer": SERVICE_INDEXER,
}

#: Principals that must never resolve to any filter at all.
ZERO_VISIBILITY_PRINCIPALS: frozenset[str] = frozenset({"internal_bot_solo", "service_indexer"})

#: Groups no fixture principal holds. Canary documents use these.
CANARY_GROUPS: frozenset[str] = frozenset({GROUP_BOARD_ONLY})
