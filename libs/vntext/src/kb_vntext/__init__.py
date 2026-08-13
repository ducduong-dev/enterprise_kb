"""Vietnamese/English text utilities shared by IDP (M1) and identity resolution (M5)."""

from kb_vntext.language import detect_language
from kb_vntext.legal_numbers import (
    INSTRUMENT_TYPES,
    LegalNumber,
    extract_legal_numbers,
    find_document_number,
    fold_code,
    guess_ref_type,
    normalize_legal_number,
)
from kb_vntext.sections import (
    Heading,
    Level,
    SectionTracker,
    build_citation_label,
    parse_heading,
)

__all__ = [
    "INSTRUMENT_TYPES",
    "Heading",
    "LegalNumber",
    "Level",
    "SectionTracker",
    "build_citation_label",
    "detect_language",
    "extract_legal_numbers",
    "find_document_number",
    "fold_code",
    "guess_ref_type",
    "normalize_legal_number",
    "parse_heading",
]
