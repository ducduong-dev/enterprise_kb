"""Index ports.

Both take an already-compiled ACL predicate: the caller (only retrieval-api, INV-1) passes
the `ResolvedFilter`, and the adapter compiles it into its own query language via
`kb_authz.compile`. An adapter that ignored the filter would fail the ACL sweep, and there is
no code path that lets it receive results before filtering (INV-2).

`KeywordIndexPort` keeps more than one adapter — pg_search in production, Postgres FTS for a
node without the extension — because the engine choice is
[OPEN]-2 until the M7 bake-off.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from kb_authz.filters import ResolvedFilter

from kb_ports.base import AdapterInfo


@dataclass(frozen=True, slots=True)
class IndexHit:
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    score: float
    text: str | None = None
    citation_label: str | None = None
    section_path: str | None = None
    highlights: tuple[str, ...] = ()


@dataclass(slots=True)
class IndexDocument:
    """One canonical chunk as written to an index (INV-6: canonical only).

    Carries the denormalized ACL columns; without them the filter could not be applied
    inside the query.
    """

    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    text: str
    citation_label: str | None
    section_path: str | None
    visibility: str
    allowed_groups: list[str]
    department: str | None
    doc_status: str
    doc_class: str
    category_path: str
    effective_from: str | None
    effective_to: str | None
    tombstoned: bool = False
    embedding: list[float] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def category_ancestors(self) -> list[str]:
        """Every ltree prefix, so subtree containment is a term lookup in an external index."""
        parts = self.category_path.split(".")
        return [".".join(parts[: i + 1]) for i in range(len(parts))]


class KeywordIndexPort(Protocol):
    @property
    def info(self) -> AdapterInfo: ...

    def health(self) -> bool: ...

    def search(self, query: str, acl: ResolvedFilter, *, top_k: int = 50) -> Sequence[IndexHit]: ...

    def upsert(self, documents: Sequence[IndexDocument]) -> int: ...

    def tombstone(self, version_ids: Sequence[UUID]) -> int:
        """Make a superseded version's chunks unretrievable. Called inside the publish
        transaction's follow-up (INV-5/INV-6)."""
        ...

    def delete_by_document(self, document_id: UUID) -> int: ...


class VectorIndexPort(Protocol):
    @property
    def info(self) -> AdapterInfo: ...

    def health(self) -> bool: ...

    def search(
        self, embedding: Sequence[float], acl: ResolvedFilter, *, top_k: int = 50
    ) -> Sequence[IndexHit]: ...

    def upsert(self, documents: Sequence[IndexDocument]) -> int: ...

    def tombstone(self, version_ids: Sequence[UUID]) -> int: ...

    def delete_by_document(self, document_id: UUID) -> int: ...
