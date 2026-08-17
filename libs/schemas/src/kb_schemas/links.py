"""How a citation becomes a link (ADR-0038).

One function, and the reasoning behind it is the whole module.

**Built from `(document_id, version_id, section_path)` and never from `chunk_id`.** The chunk id
stays in the payload — it is what joins a citation to its audit record (INV-11) — but it is a
retrieval fact rather than an address: `_insert_chunks` deletes and re-inserts every chunk of a
version, so a link built from one is broken by the next rechunk. A saved answer is exactly the
boundary a derived identifier must not cross, which is the rule ADR-0036 named and every M9
table already obeys.

**It names the version the answer was drawn from.** A link in a six-month-old answer opens what
was actually cited, not what the document says today. An answer log promising reconstructability
and links that quietly re-point are not compatible; the document view is where "this is not the
current version" belongs, and it already knows how to say so.

**Relative, not absolute.** The platform has no configured public origin, and an API that
invented one would bake a deployment fact into stored answers — the thing this module exists to
avoid. Each surface prepends its own.

**A link is not a capability.** It resolves through the portal's document route, which re-checks
access against whoever opened it (ADR-0038's gate 3). Forwarded to a colleague it is evaluated
against *their* principal and may show them nothing, which is the correct behaviour and not a
broken link.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode
from uuid import UUID

#: The portal route a citation opens. Relative on purpose — see the module docstring.
DOCUMENT_ROUTE = "/documents"


def document_link(
    document_id: UUID | str,
    *,
    version_id: UUID | str | None = None,
    section_path: str | None = None,
) -> str:
    """A link to the cited passage, in the version it was cited from.

    `version_id` and `section_path` are precision rather than resolution: the document route
    resolves on the id alone, and a reader who follows a link with neither still lands on the
    right document under their own filter. What the extra two buy is landing on the *right
    version* and the right place in it.
    """
    params: list[tuple[str, str]] = []
    if version_id is not None:
        params.append(("version", str(version_id)))
    if section_path:
        params.append(("section", section_path))
    query = f"?{urlencode(params)}" if params else ""
    return f"{DOCUMENT_ROUTE}/{quote(str(document_id))}{query}"


__all__ = ["DOCUMENT_ROUTE", "document_link"]
