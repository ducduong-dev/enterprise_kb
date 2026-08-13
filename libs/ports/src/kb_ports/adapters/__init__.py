"""Adapter implementations shared by more than one service.

Service-specific adapters live under `services/*/adapters/`. Everything in an `adapters`
package is exempt from the INV-12 import rule — this is the *only* layer allowed to name a
vendor client. Importing one of these modules from a service is fine; importing what they
wrap is not.
"""

from kb_ports.adapters.embedding_hashed import HashedEmbeddingAdapter
from kb_ports.adapters.embedding_tei import TeiEmbeddingAdapter
from kb_ports.adapters.rerank import LexicalRerankAdapter, TeiRerankAdapter
from kb_ports.adapters.storage_local import LocalStorageAdapter
from kb_ports.adapters.storage_s3 import S3StorageAdapter

__all__ = [
    "HashedEmbeddingAdapter",
    "LexicalRerankAdapter",
    "LocalStorageAdapter",
    "S3StorageAdapter",
    "TeiEmbeddingAdapter",
    "TeiRerankAdapter",
]
