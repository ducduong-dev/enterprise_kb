"""KBDoc assembly.

Every parser pushes blocks through this builder, so structure tracking, block numbering,
reference detection and the IDP report are implemented once. A parser's only job is to turn
its format into an ordered stream of (type, text, page, bbox) — everything downstream of that
is shared, which is what keeps a DOCX and a scanned PDF genuinely interchangeable to the rest
of the platform.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from kb_schemas.kbdoc import (
    Block,
    BlockType,
    DetectedRef,
    DocMeta,
    Engine,
    IdpReport,
    KBDoc,
    SourceFormat,
    Table,
)
from kb_vntext.dates import detect as detect_dates
from kb_vntext.language import detect_language
from kb_vntext.legal_numbers import extract_legal_numbers, find_anchors, guess_ref_type
from kb_vntext.sections import SectionTracker

#: How many leading blocks are considered when looking for the document's own number.
_HEADER_BLOCKS = 12

#: The header label that introduces an instrument's own number: "Số: 41/2016/TT-NHNN".
#:
#: The colon is load-bearing. Vietnamese headers write "Số: 41/2016/TT-NHNN" while citations
#: write "Nghị định số 88/2019/NĐ-CP" — without the colon requirement, every citation in the
#: opening paragraph would be mistaken for the document's own number. Searched rather than
#: anchored because OCR merges header lines into one block.
_NUMBER_LABEL = re.compile(r"(?:số|so|no)\s*[:.]\s*$", re.IGNORECASE)


@dataclass
class KBDocBuilder:
    source_format: SourceFormat
    engine: Engine = "native"
    _blocks: list[Block] = field(default_factory=list)
    _tracker: SectionTracker = field(default_factory=SectionTracker)
    _warnings: list[str] = field(default_factory=list)
    _engine_versions: dict[str, str] = field(default_factory=dict)
    _page_confidences: dict[int, list[float]] = field(default_factory=dict)
    _started: float = field(default_factory=time.perf_counter)
    _title_hint: str | None = None

    def warn(self, message: str) -> None:
        self._warnings.append(message)

    def note_engine(self, name: str, version: str) -> None:
        self._engine_versions[name] = version

    def add(
        self,
        text: str,
        *,
        block_type: BlockType | None = None,
        page: int = 1,
        bbox: tuple[float, float, float, float] | None = None,
        table: Table | None = None,
        confidence: float = 1.0,
        engine: Engine | None = None,
    ) -> Block | None:
        """Append a block, updating the structural path. Empty text with no table is dropped.

        Returns the block so callers can reference its id (used for `detected_refs`).
        """
        cleaned = _normalize_whitespace(text)
        if not cleaned and table is None:
            return None

        heading = self._tracker.feed(cleaned) if cleaned else None
        resolved_type: BlockType = block_type or ("heading" if heading else "paragraph")
        if table is not None:
            resolved_type = "table"

        block = Block(
            id=f"b{len(self._blocks) + 1:03d}",
            type=resolved_type,
            section_path=list(self._tracker.path),
            text=cleaned,
            table=table,
            page=page,
            bbox=bbox,
            confidence=confidence,
            engine=engine or self.engine,
        )
        self._blocks.append(block)
        self._page_confidences.setdefault(page, []).append(confidence)
        if self._title_hint is None and resolved_type == "heading" and cleaned:
            self._title_hint = cleaned
        return block

    def add_heading(self, text: str, **kwargs: object) -> Block | None:
        return self.add(text, block_type="heading", **kwargs)  # type: ignore[arg-type]

    def add_table(
        self, rows: list[list[str]], *, header_rows: int = 0, **kwargs: object
    ) -> Block | None:
        if not any(any(cell.strip() for cell in row) for row in rows):
            return None
        table = Table(rows=rows, header_rows=header_rows)
        # Tables also carry a flattened text rendering: keyword search and the chunker both
        # need something to match on, and a table with no text is invisible to retrieval.
        flattened = "\n".join(" | ".join(cell.strip() for cell in row) for row in rows)
        return self.add(flattened, table=table, block_type="table", **kwargs)  # type: ignore[arg-type]

    def set_title_hint(self, title: str) -> None:
        self._title_hint = _normalize_whitespace(title) or None

    # ------------------------------------------------------------------------ finishing

    def build(
        self,
        *,
        page_count: int = 0,
        source_format: SourceFormat | None = None,
        issuing_body: str | None = None,
        issued_date: str | None = None,
    ) -> KBDoc:
        full_text = "\n".join(block.text for block in self._blocks if block.text)
        own_number = self._own_legal_number()
        dates = detect_dates(full_text)

        return KBDoc(
            doc_meta=DocMeta(
                detected_title=self._detected_title(),
                legal_number=own_number,
                language=detect_language(full_text),
                source_format=source_format or self.source_format,
                page_count=page_count or max(self._page_confidences or {1: []}, default=1),
                issuing_body=issuing_body,
                issued_date=issued_date or (dates.issued.isoformat() if dates.issued else None),
                effective_from=(dates.effective_from.isoformat() if dates.effective_from else None),
                effective_evidence=dates.effective_evidence or None,
                effective_to=(dates.effective_to.isoformat() if dates.effective_to else None),
                expiry_evidence=dates.expiry_evidence or None,
            ),
            blocks=self._blocks,
            detected_refs=self._detect_refs(own_number),
            idp_report=IdpReport(
                page_confidences=[
                    sum(values) / len(values)
                    for _page, values in sorted(self._page_confidences.items())
                    if values
                ],
                escalated_pages=[],
                warnings=self._warnings,
                engine_versions=self._engine_versions,
                duration_ms=int((time.perf_counter() - self._started) * 1000),
            ),
        )

    def _detected_title(self) -> str | None:
        if self._title_hint:
            return self._title_hint
        for block in self._blocks:
            if block.text:
                return block.text[:300]
        return None

    def _own_legal_number(self) -> str | None:
        """The instrument number this document *is*, not one it mentions.

        Getting this wrong is expensive: `documents.legal_number` is unique, so a citation
        mistaken for the document's own number makes an unrelated upload collide with the
        instrument it cites, and identity resolution (M5) inherits the error. So only the two
        forms a Vietnamese instrument header actually uses are accepted — a `Số:` line, or the
        number standing alone as its own block — and only near the top of the document.
        """
        for block in self._blocks[:_HEADER_BLOCKS]:
            if not block.text:
                continue
            for number in extract_legal_numbers(block.text):
                preceding = block.text[: number.start]
                standalone = block.text.strip() in (number.value, number.raw)
                if standalone or _NUMBER_LABEL.search(preceding):
                    return number.value
        return None

    def _detect_refs(self, own_number: str | None) -> list[DetectedRef]:
        """Every instrument mentioned other than this document itself.

        The relationship is a guess (`detected_by` in the edge); M5's review task is where a
        human confirms it, because an amendment recorded the wrong way round would corrupt the
        consolidation chain.
        """
        refs: list[DetectedRef] = []
        seen: set[tuple[str, str]] = set()
        for block in self._blocks:
            if not block.text:
                continue
            for number in extract_legal_numbers(block.text):
                if own_number and number.value == own_number:
                    continue
                ref_type = guess_ref_type(block.text, number.start)
                key = (number.key, ref_type)
                if key in seen:
                    continue
                seen.add(key)
                refs.append(
                    DetectedRef(
                        raw=number.raw,
                        legal_number=number.value,
                        ref_type_guess=ref_type,
                        # Read from the same window the ref type is: Vietnamese citations run
                        # inside-out and sit before the instrument number (ADR-0036).
                        anchors=find_anchors(block.text, number.start),
                        block_id=block.id,
                        confidence=number.confidence,
                    )
                )
        return refs


def _normalize_whitespace(text: str) -> str:
    """Collapse layout whitespace without touching characters.

    Line structure is preserved — a table's flattened rows and a multi-line address are both
    meaningless once folded onto one line — while runs of spaces from PDF layout are collapsed.

    Deliberately does not normalize Unicode: `NFC`/`NFD` folding would alter Vietnamese
    diacritics, and the byte-level fixture test (M3 AC) exists to prove we never do.
    """
    lines = [" ".join(line.replace("\xa0", " ").split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()
