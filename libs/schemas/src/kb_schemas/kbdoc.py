"""`KBDoc` — the normalized document format (plan section 5).

Every parser and OCR path emits exactly this. Downstream (chunker, identity resolution, PII
gate, review editor) depends only on this contract, never on the source format. Adding a
field is backwards-compatible; changing the meaning of one is a versioned contract change —
bump `kbdoc_version` and keep the old reader until the corpus is reprocessed.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

KBDOC_VERSION = "1.0"

BlockType = Literal["heading", "paragraph", "table", "figure", "stamp", "list_item", "footer"]
Engine = Literal["native", "paddle", "vlm", "human"]
Language = Literal["vi", "en", "mixed", "unknown"]
#: "structured" is content authored in the portal rather than parsed from a file — a rate
#: schedule entered as items (M8). It matters for provenance: nothing was recognised, so no
#: confidence score on those blocks means "the machine was sure", it means "a person typed it".
SourceFormat = Literal[
    "pdf_native", "pdf_scanned", "docx", "xlsx", "pptx", "html", "txt", "image", "structured"
]


class DocMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detected_title: str | None = None
    legal_number: str | None = None
    language: Language = "unknown"
    source_format: SourceFormat
    page_count: int = 0
    issued_date: str | None = None
    issuing_body: str | None = None
    #: ISO date the instrument states it takes effect, read from its own text. `None` means
    #: the document did not say plainly — a question for the reviewer, never a guess, because
    #: a NULL here silently means "always in effect" (ADR-0029).
    effective_from: str | None = None
    #: The sentence `effective_from` was read from, so a reviewer can check it without opening
    #: the document.
    effective_evidence: str | None = None
    #: ISO date of the last day the instrument applies, when it states its own sunset —
    #: a fee schedule with a window on its face. Almost always `None`: expiry normally arrives
    #: later from another instrument, and that is a ledger decision, not a version fact
    #: (ADR-0030). Read then confirmed, exactly like `effective_from`.
    effective_to: str | None = None
    #: The sentence `effective_to` was read from.
    expiry_evidence: str | None = None


class Table(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[list[str]] = Field(default_factory=list)
    header_rows: int = 0
    #: Set when a table spans pages and was stitched by PP-Structure post-processing.
    continued_from_block: str | None = None


class Block(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: BlockType
    #: Hierarchical location, e.g. ["Chương II", "Điều 12", "Khoản 2"]. Drives chunk
    #: section_path and citation_label; must survive OCR (diacritics are load-bearing).
    section_path: list[str] = Field(default_factory=list)
    text: str = ""
    table: Table | None = None
    page: int = 0
    #: [x0, y0, x1, y1] in page coordinates — the review editor jumps to this.
    bbox: tuple[float, float, float, float] | None = None
    confidence: float = 1.0
    engine: Engine = "native"

    @property
    def needs_review(self) -> bool:
        return self.confidence < LOW_CONFIDENCE_THRESHOLD


class DetectedRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw: str
    legal_number: str | None = None
    ref_type_guess: str = "cites"
    #: Which clauses of the cited instrument this reference names, in the dotted form the
    #: target's chunks carry — "12", "12.2", "12.2a". Empty means the whole instrument, which
    #: is what "theo quy định tại Thông tư 41/2016" cites (ADR-0036).
    anchors: list[str] = Field(default_factory=list)
    block_id: str | None = None
    confidence: float = 0.0


class DetectedDeclaration(BaseModel):
    """A sentence in which this document says what it does to another (ADR-0039).

    Distinct from a `DetectedRef`, which records only that one instrument mentions another. A
    declaration says *what* — ends it, replaces it, amends it — *which clauses*, and often *from
    when*, all read from one sentence that is stored alongside so a steward can confirm it at a
    glance rather than by opening the document.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["abrogates", "replaces", "amends"]
    #: The instrument acted upon. Null when the sentence names none, which makes it a statement
    #: about this document itself — a renumbering, and the merge flow's business.
    target: str | None = None
    #: Clauses of the target, dotted: "12", "12.2", "12.2a". Empty = the whole instrument.
    target_anchors: list[str] = Field(default_factory=list)
    #: Clauses of *this* document that take their place. Empty for a pure abrogation.
    replacement_anchors: list[str] = Field(default_factory=list)
    #: ISO date, only when the sentence states one behind an effectivity cue — never the issue
    #: date that happens to sit beside the target's number.
    effective_from: str | None = None
    #: The sentence itself. The whole reason confirming a declaration is cheap.
    evidence: str = ""
    block_id: str | None = None
    confidence: float = 0.0


class IdpReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_confidences: list[float] = Field(default_factory=list)
    escalated_pages: list[int] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    engine_versions: dict[str, str] = Field(default_factory=dict)
    duration_ms: int = 0


class KBDoc(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kbdoc_version: str = KBDOC_VERSION
    doc_meta: DocMeta
    blocks: list[Block] = Field(default_factory=list)
    detected_refs: list[DetectedRef] = Field(default_factory=list)
    #: Almost always empty: most instruments are not amendments. When it is not, this is the
    #: ~80% path — the corpus stating a supersession rather than us inferring one (ADR-0039).
    declarations: list[DetectedDeclaration] = Field(default_factory=list)
    idp_report: IdpReport = Field(default_factory=IdpReport)

    def block(self, block_id: str) -> Block | None:
        return next((b for b in self.blocks if b.id == block_id), None)

    @property
    def low_confidence_blocks(self) -> list[Block]:
        return [b for b in self.blocks if b.needs_review]

    @property
    def full_text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks if b.text)


#: Below this, a block is highlighted in the review editor and counts toward page escalation.
LOW_CONFIDENCE_THRESHOLD = 0.85
#: Below this page mean, the page is escalated to the VLM (Qwen2.5-VL) rather than PaddleOCR.
PAGE_ESCALATION_THRESHOLD = 0.80
