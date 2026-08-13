"""Port plumbing: adapter registry, content addressing, and the index document shape."""

from __future__ import annotations

import io
import uuid

import pytest
from kb_common.errors import ConfigError
from kb_ports.indexes import IndexDocument
from kb_ports.models import PiiFinding, PiiScanResult
from kb_ports.registry import PortName, clear_registry, get_adapter, register_adapter, registered
from kb_ports.storage import content_key, hash_stream


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    clear_registry()


def test_adapter_resolution_is_by_port_and_name() -> None:
    @register_adapter(PortName.GENERATION, "vllm")
    def _vllm(url: str = "http://local") -> str:
        return f"vllm:{url}"

    @register_adapter(PortName.GENERATION, "api")
    def _api(url: str = "http://vendor") -> str:
        return f"api:{url}"

    # [OPEN]-1 is a config switch, not a code change: both adapters ship.
    assert get_adapter(PortName.GENERATION, "vllm") == "vllm:http://local"
    assert get_adapter(PortName.GENERATION, "api") == "api:http://vendor"
    assert registered(PortName.GENERATION) == [
        (PortName.GENERATION, "api"),
        (PortName.GENERATION, "vllm"),
    ]


def test_unknown_adapter_names_what_is_available() -> None:
    @register_adapter(PortName.KEYWORD_INDEX, "postgres_fts")
    def _fts() -> str:
        return "fts"

    with pytest.raises(ConfigError) as exc:
        get_adapter(PortName.KEYWORD_INDEX, "elasticsearch")
    assert exc.value.detail["available"] == ["postgres_fts"]


def test_duplicate_registration_is_rejected() -> None:
    @register_adapter(PortName.EMBEDDING, "bge-m3")
    def _bge() -> str:
        return "bge"

    with pytest.raises(ConfigError):

        @register_adapter(PortName.EMBEDDING, "bge-m3")
        def _bge_again() -> str:
            return "bge"


def test_storage_keys_are_content_addressed() -> None:
    data = io.BytesIO(b"n\xc3\xb4i dung")
    digest = hash_stream(data)
    assert data.tell() == 0, "the stream must be rewound for the caller"
    key = content_key("originals", digest, ".pdf")
    assert key == f"originals/{digest[:2]}/{digest[2:4]}/{digest}.pdf"
    # Identical bytes produce an identical key, so re-upload is a no-op.
    assert content_key("originals", hash_stream(io.BytesIO(b"n\xc3\xb4i dung")), ".pdf") == key


def test_index_document_expands_category_ancestors() -> None:
    doc = IndexDocument(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        text="…",
        citation_label="Điều 12.2",
        section_path="Chương II > Điều 12",
        visibility="internal_all",
        allowed_groups=[],
        department=None,
        doc_status="published",
        doc_class="regulatory",
        category_path="regulations.sbv.capital",
        effective_from="2020-01-01",
        effective_to=None,
    )
    assert doc.category_ancestors == [
        "regulations",
        "regulations.sbv",
        "regulations.sbv.capital",
    ]


def test_pii_scan_fails_closed_when_incomplete() -> None:
    """An inconclusive scan is not a clear one (INV-7)."""
    assert PiiScanResult(findings=[], scan_complete=True).is_clear
    assert not PiiScanResult(findings=[], scan_complete=False).is_clear
    finding = PiiFinding(
        kind="pan", text="4111…", start=0, end=5, confidence=0.99, detector="pattern:pan_luhn"
    )
    assert not PiiScanResult(findings=[finding]).is_clear
