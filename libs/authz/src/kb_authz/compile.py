"""Compile a `ResolvedFilter` into index-native predicates.

The compilers put the ACL *inside* the query — a SQL `WHERE` clause and a graph-edge
context — never as a post-processing step over results (INV-2). Post-filtering would leak
existence through result counts, scores and pagination even when text never reaches the user.

Any new index backend adds a compiler here and inherits the ACL sweep tests unchanged.
"""

from __future__ import annotations

from typing import Any

from kb_schemas.enums import Visibility

from kb_authz.filters import ResolvedFilter


def compile_sql(
    f: ResolvedFilter, alias: str = "c", prefix: str = "acl"
) -> tuple[str, dict[str, Any]]:
    """Return `(where_fragment, params)` for the `chunks` table (or a compatible view).

    The fragment is always non-empty and always parenthesised, so callers can safely
    concatenate it with `AND`.
    """
    a = alias
    p = prefix
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if not f.include_tombstoned:
        clauses.append(f"{a}.tombstoned = FALSE")

    # Visibility + group unlock for restricted content, as one predicate.
    open_visibilities = sorted(v.value for v in f.visibilities if v is not Visibility.RESTRICTED)
    visibility_terms: list[str] = []
    if open_visibilities:
        params[f"{p}_visibilities"] = open_visibilities
        visibility_terms.append(f"{a}.visibility = ANY(CAST(:{p}_visibilities AS visibility[]))")
    if Visibility.RESTRICTED in f.visibilities:
        params[f"{p}_group_scope"] = sorted(f.group_scope)
        visibility_terms.append(
            f"({a}.visibility = 'restricted'"
            f" AND {a}.allowed_groups && CAST(:{p}_group_scope AS TEXT[]))"
        )
    clauses.append("(" + " OR ".join(visibility_terms) + ")")

    params[f"{p}_statuses"] = sorted(s.value for s in f.statuses)
    clauses.append(f"{a}.doc_status = ANY(CAST(:{p}_statuses AS doc_status[]))")

    # Effectivity at the evaluation date (today, or the as-of date).
    params[f"{p}_effective_on"] = f.effective_on
    clauses.append(
        f"({a}.effective_from IS NULL OR {a}.effective_from <= :{p}_effective_on)"
        f" AND ({a}.effective_to IS NULL OR {a}.effective_to >= :{p}_effective_on)"
    )

    if f.departments is not None:
        params[f"{p}_departments"] = sorted(f.departments)
        clauses.append(f"{a}.department = ANY(CAST(:{p}_departments AS TEXT[]))")

    if f.category_prefixes is not None:
        subtree_terms = []
        for i, prefix_path in enumerate(sorted(f.category_prefixes)):
            key = f"{p}_category_{i}"
            params[key] = prefix_path
            subtree_terms.append(f"{a}.category_path <@ CAST(:{key} AS ltree)")
        clauses.append("(" + " OR ".join(subtree_terms) + ")")

    if f.doc_classes is not None:
        params[f"{p}_doc_classes"] = sorted(c.value for c in f.doc_classes)
        clauses.append(f"{a}.doc_class = ANY(CAST(:{p}_doc_classes AS doc_class[]))")

    if f.issued_from is not None:
        params[f"{p}_issued_from"] = f.issued_from
        clauses.append(f"{a}.effective_from >= :{p}_issued_from")
    if f.issued_to is not None:
        params[f"{p}_issued_to"] = f.issued_to
        clauses.append(f"{a}.effective_from <= :{p}_issued_to")

    return "(" + " AND ".join(f"({c})" for c in clauses) + ")", params


def compile_sql_expired(
    f: ResolvedFilter, alias: str = "c", prefix: str = "acl"
) -> tuple[str, dict[str, Any]]:
    """The same ACL, restricted to chunks that have *already* expired.

    For the named refusal: "that rule ceased on 31/12/2026" is more use to a reader than
    silence, and silence is indistinguishable from "the bank never said anything about this"
    (ADR-0028). It exists as its own compiler rather than a flag on `compile_sql` for one
    reason — the predicate it emits **cannot return a live chunk**. `effective_to` is required
    to be non-null and in the past, so the worst a bug in the caller can do is show a reader
    something that has stopped applying, never something they may not see and never something
    current dressed up as expired.

    Everything else — visibility, group unlock, status, department, category, class — is the
    caller's own filter, unchanged and still inside the query (INV-2). What comes back is
    metadata for a refusal: the instrument, the label and the date. Never the text, which would
    be a citation, and a citation to a repealed rule is the thing this whole milestone exists
    to prevent (ADR-0018 unchanged).
    """
    where, params = compile_sql(f, alias, prefix)
    # `compile_sql` already asserted `effective_to IS NULL OR effective_to >= :on`; the caller
    # concatenates with AND, so this fragment has to replace it rather than add to it.
    live = (
        f"({alias}.effective_from IS NULL OR {alias}.effective_from <= :{prefix}_effective_on)"
        f" AND ({alias}.effective_to IS NULL OR {alias}.effective_to >= :{prefix}_effective_on)"
    )
    expired = (
        f"({alias}.effective_from IS NULL OR {alias}.effective_from <= :{prefix}_effective_on)"
        f" AND {alias}.effective_to IS NOT NULL"
        f" AND {alias}.effective_to < :{prefix}_effective_on"
    )
    if live not in where:  # pragma: no cover - guards a refactor of compile_sql
        raise AssertionError(
            "compile_sql no longer emits the effectivity predicate this compiler replaces"
        )
    return where.replace(live, expired), params


def compile_sql_graph(
    f: ResolvedFilter, alias: str = "g", prefix: str = "gacl"
) -> tuple[str, dict[str, Any]]:
    """The same predicate over `graph_serving`'s `dst_*` columns.

    Graph expansion must be filtered by the ACL of the *target* document (INV-10), and the
    serving projection carries a copy of it. Compiling it here rather than in retrieval-api
    keeps every ACL predicate in one reviewable module — a second, hand-written version of
    this logic is exactly how expansion ends up leaking what search does not.

    Effectivity and status are deliberately not applied: an edge points at a document, and
    whether that document currently has published, in-effect text is decided when its chunks
    are fetched. What matters here is that the *existence* of the target is not disclosed to
    someone who may not see it.
    """
    a = alias
    p = prefix
    params: dict[str, Any] = {}
    terms: list[str] = []

    open_visibilities = sorted(v.value for v in f.visibilities if v is not Visibility.RESTRICTED)
    if open_visibilities:
        params[f"{p}_visibilities"] = open_visibilities
        terms.append(f"{a}.dst_visibility = ANY(CAST(:{p}_visibilities AS visibility[]))")
    if Visibility.RESTRICTED in f.visibilities:
        params[f"{p}_group_scope"] = sorted(f.group_scope)
        terms.append(
            f"({a}.dst_visibility = 'restricted'"
            f" AND {a}.dst_allowed_groups && CAST(:{p}_group_scope AS TEXT[]))"
        )
    return "(" + " OR ".join(terms) + ")", params


def compile_sql_documents(
    f: ResolvedFilter, alias: str = "d", prefix: str = "dacl"
) -> tuple[str, dict[str, Any]]:
    """The visibility half of the predicate over the `documents` table.

    This is the *existence* question rather than the content one: who may know that a document
    is in the registry at all. The registry listing and the inspection screen both ask it, and
    both must ask it here — a hand-written second copy is how a restricted instrument ends up
    named in a table that never shows its text.

    Status and effectivity are deliberately not applied, as in `compile_sql_graph`: a steward
    lists drafts and expired instruments on purpose, and what may not leak is the row itself.
    """
    a = alias
    p = prefix
    params: dict[str, Any] = {}
    terms: list[str] = []

    open_visibilities = sorted(v.value for v in f.visibilities if v is not Visibility.RESTRICTED)
    if open_visibilities:
        params[f"{p}_visibilities"] = open_visibilities
        terms.append(f"{a}.visibility = ANY(CAST(:{p}_visibilities AS visibility[]))")
    if Visibility.RESTRICTED in f.visibilities:
        params[f"{p}_group_scope"] = sorted(f.group_scope)
        terms.append(
            f"({a}.visibility = 'restricted'"
            f" AND {a}.allowed_groups && CAST(:{p}_group_scope AS TEXT[]))"
        )
    return "(" + " OR ".join(terms) + ")", params
