"""Port interfaces. Adapters implement these; services depend only on these (INV-12)."""

from kb_ports.base import AdapterInfo, Port
from kb_ports.indexes import IndexDocument, IndexHit, KeywordIndexPort, VectorIndexPort
from kb_ports.models import (
    EmbeddingPort,
    Generation,
    GenerationPort,
    Message,
    OcrLine,
    OcrPageResult,
    OcrPort,
    PiiDetectorPort,
    PiiFinding,
    PiiScanResult,
    RerankPort,
    RerankResult,
    VlmPort,
)
from kb_ports.registry import PortName, get_adapter, register_adapter, registered
from kb_ports.storage import (
    StoragePort,
    StoredObject,
    content_key,
    hash_bytes,
    hash_stream,
)

__all__ = [
    "AdapterInfo",
    "EmbeddingPort",
    "Generation",
    "GenerationPort",
    "IndexDocument",
    "IndexHit",
    "KeywordIndexPort",
    "Message",
    "OcrLine",
    "OcrPageResult",
    "OcrPort",
    "PiiDetectorPort",
    "PiiFinding",
    "PiiScanResult",
    "Port",
    "PortName",
    "RerankPort",
    "RerankResult",
    "StoragePort",
    "StoredObject",
    "VectorIndexPort",
    "VlmPort",
    "content_key",
    "get_adapter",
    "hash_bytes",
    "hash_stream",
    "register_adapter",
    "registered",
]
