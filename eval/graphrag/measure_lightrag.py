"""LightRAG's ingestion unit-of-work on a real Vietnamese instrument.

Produces the LightRAG half of the table in `protocol.md`. No model is needed: the number of
extraction calls is `chunks * (1 + MAX_GLEANING)`, and chunking is deterministic.

Needs LightRAG, which is deliberately not a dependency of this repo:

    uv venv /tmp/lrenv && uv pip install --python /tmp/lrenv/bin/python lightrag-hku
    /tmp/lrenv/bin/python eval/graphrag/measure_lightrag.py
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

DOCS = [
    Path("/home/clt/workspace/mbbank/enterprise_kb/data/Nghi_dinh_309.docx"),
    Path("/home/clt/workspace/mbbank/enterprise_kb/data/2025_765+766_118-2025-NĐ-CP.docx"),
]

_TAG = re.compile(r"<[^>]+>")


def docx_text(path: Path) -> str:
    """Paragraph text straight out of the OOXML — enough for a chunk-count measurement."""
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    xml = xml.replace("</w:p>", "\n")
    text = _TAG.sub("", xml)
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def lightrag_chunks(text: str) -> list[dict]:
    from lightrag.chunker.token_size import chunking_by_token_size
    from lightrag.utils import TiktokenTokenizer

    return chunking_by_token_size(TiktokenTokenizer(), text)


def main() -> None:
    from lightrag.constants import DEFAULT_MAX_GLEANING

    report = []
    for path in DOCS:
        if not path.exists():
            print(f"missing: {path}", file=sys.stderr)
            continue
        text = docx_text(path)
        chunks = lightrag_chunks(text)
        tokens = sum(c.get("tokens", 0) for c in chunks)
        report.append(
            {
                "document": path.name,
                "chars": len(text),
                "lightrag_chunks": len(chunks),
                "lightrag_tokens": tokens,
                "extraction_calls": len(chunks) * (1 + DEFAULT_MAX_GLEANING),
                "tokens_sent_min": tokens * (1 + DEFAULT_MAX_GLEANING),
            }
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
