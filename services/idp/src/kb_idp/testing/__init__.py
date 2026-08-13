"""Parser fixtures.

Shipped inside the package rather than under `tests/` because other services' tests need the
same twelve documents — the workflow tests ingest them end to end. Keeping one copy means the
goldens and the workflow tests can never drift apart.
"""

from kb_idp.testing.fixtures import (
    ALL_FIXTURES,
    COMMITTED_FIXTURES,
    GENERATED_FIXTURES,
    OCR_ROUTING_FIXTURE,
    fixture_bytes,
)

__all__ = [
    "ALL_FIXTURES",
    "COMMITTED_FIXTURES",
    "GENERATED_FIXTURES",
    "OCR_ROUTING_FIXTURE",
    "fixture_bytes",
]
