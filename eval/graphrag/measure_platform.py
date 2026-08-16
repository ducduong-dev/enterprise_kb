"""The same two documents through the platform's own chunker, for comparison.

Produces the platform half of the table in `protocol.md`: chunk count, how many chunks carry a
real citation label, how many distinct articles the document has — the unit M9b/M9c operate on
— and the dates and typed references M9a's detectors consume, all with no model call.

    .venv/bin/python eval/graphrag/measure_platform.py
"""

from __future__ import annotations

import json
from pathlib import Path

from kb_idp.parsers.docx import parse_docx
from kb_indexer.chunker import chunk_document
from kb_vntext.dates import detect
from kb_vntext.legal_numbers import extract_legal_numbers, guess_ref_type

DOCS = [
    Path("/home/clt/workspace/mbbank/enterprise_kb/data/Nghi_dinh_309.docx"),
    Path("/home/clt/workspace/mbbank/enterprise_kb/data/2025_765+766_118-2025-NĐ-CP.docx"),
]


def main() -> None:
    report = []
    for path in DOCS:
        kbdoc = parse_docx(path.read_bytes())
        legal_number = kbdoc.doc_meta.legal_number
        chunks = chunk_document(kbdoc, legal_number=legal_number)
        text = "\n".join(block.text for block in kbdoc.blocks)
        articles = {
            part for chunk in chunks for part in chunk.section_path if part.startswith("Điều")
        }
        dates = detect(text)
        mentions = extract_legal_numbers(text)
        typed = [guess_ref_type(text, m.start) for m in mentions if hasattr(m, "start")]
        ref_types = sorted(set(typed))
        report.append(
            {
                "document": path.name,
                "blocks": len(kbdoc.blocks),
                "legal_number": legal_number,
                "kb_chunks": len(chunks),
                "chunks_with_citation": sum(1 for c in chunks if c.citation_label),
                "distinct_articles": len(articles),
                "issued_date": str(dates.issued) if dates.issued else None,
                "effective_from": str(dates.effective_from) if dates.effective_from else None,
                "effective_evidence": (dates.effective_evidence or "")[:160],
                "legal_numbers_cited": len(extract_legal_numbers(text)),
                "typed_references": len(typed),
                "ref_types": ref_types,
            }
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
