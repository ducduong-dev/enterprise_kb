"""Loader for the red-team corpus.

The corpus lives in `eval/pii_redteam/` because it is an evaluation asset that Compliance
maintains, not test data the platform team owns. It is loaded from here so the gate's tests,
the chat output-filter tests and any future eval run all read the same forty-plus-forty
documents.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

CORPUS_DIRNAME = Path("eval") / "pii_redteam"


@dataclass(frozen=True, slots=True)
class RedTeamDocument:
    id: str
    text: str
    expect_block: bool
    kind: str | None = None
    note: str = ""


def corpus_dir() -> Path:
    """Walk up to the repository root. The corpus is outside the package by design."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / CORPUS_DIRNAME
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("eval/pii_redteam not found")


@lru_cache(maxsize=1)
def load() -> tuple[RedTeamDocument, ...]:
    directory = corpus_dir()
    documents: list[RedTeamDocument] = []
    for filename, expect_block in (
        ("pii_documents.yaml", True),
        ("clean_documents.yaml", False),
    ):
        entries = yaml.safe_load((directory / filename).read_text(encoding="utf-8")) or []
        documents.extend(
            RedTeamDocument(
                id=entry["id"],
                text=entry["text"],
                expect_block=expect_block,
                kind=entry.get("kind"),
                note=entry.get("note", ""),
            )
            for entry in entries
        )
    return tuple(documents)


def must_block() -> tuple[RedTeamDocument, ...]:
    return tuple(document for document in load() if document.expect_block)


def must_pass() -> tuple[RedTeamDocument, ...]:
    return tuple(document for document in load() if not document.expect_block)
