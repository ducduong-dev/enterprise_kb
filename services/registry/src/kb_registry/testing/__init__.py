"""Registry test fixtures.

Shipped inside the package rather than under `tests/` because several test modules — the
ledger, the sweep, and the workflow-level flows — need the same three rows, and a copy per
module is three chances for them to drift apart in ways that make a failure hard to read.
"""

from kb_registry.testing.rows import chunk_dates, make_chunk, make_document, make_version

__all__ = ["chunk_dates", "make_chunk", "make_document", "make_version"]
