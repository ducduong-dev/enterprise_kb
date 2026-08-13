"""Plain text, Markdown and HTML → KBDoc.

These arrive from intranet page exports and from systems that emit notices as text. HTML is
parsed with the stdlib parser rather than a dependency: we need block boundaries and table
cells, not CSS-accurate rendering, and every additional parser is another thing that can
mangle diacritics.
"""

from __future__ import annotations

from html.parser import HTMLParser

from kb_schemas.kbdoc import KBDoc

from kb_idp.builder import KBDocBuilder
from kb_idp.detect import decode_text

_BLOCK_TAGS = {"p", "div", "li", "br", "section", "article", "blockquote"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP_TAGS = {"script", "style", "head", "nav", "footer"}


def parse_txt(data: bytes) -> KBDoc:
    text = decode_text(data)
    builder = KBDocBuilder(source_format="txt")
    builder.note_engine("stdlib", "native")
    # A blank line separates paragraphs; a single newline inside one is soft wrapping.
    for paragraph in text.split("\n\n"):
        builder.add(paragraph.replace("\n", " "))
    return builder.build(page_count=1, source_format="txt")


def parse_html(data: bytes) -> KBDoc:
    builder = KBDocBuilder(source_format="html")
    builder.note_engine("html.parser", "native")
    parser = _HtmlToBlocks(builder)
    parser.feed(decode_text(data))
    parser.flush()
    return builder.build(page_count=1, source_format="html")


class _HtmlToBlocks(HTMLParser):
    def __init__(self, builder: KBDocBuilder) -> None:
        super().__init__(convert_charrefs=True)
        self._builder = builder
        self._buffer: list[str] = []
        self._heading = False
        self._skip_depth = 0
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "table":
            self.flush()
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self.flush(into_cell=False)
        elif tag in _HEADING_TAGS:
            self.flush()
            self._heading = True
        elif tag in _BLOCK_TAGS:
            self.flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in {"td", "th"} and self._row is not None:
            self._row.append(self._take())
        elif tag == "tr" and self._table is not None and self._row is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self._builder.add_table(self._table, header_rows=1)
            self._table = None
        elif tag in _HEADING_TAGS:
            self.flush()
            self._heading = False
        elif tag in _BLOCK_TAGS:
            self.flush()

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._buffer.append(data)

    def _take(self) -> str:
        text = "".join(self._buffer).strip()
        self._buffer.clear()
        return text

    def flush(self, into_cell: bool = True) -> None:
        text = self._take()
        if not text or self._table is not None:
            return
        self._builder.add(text, block_type="heading" if self._heading else None)
