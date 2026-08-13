"""Object storage (MinIO/S3), content-hash addressed."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import BinaryIO, Protocol

from kb_ports.base import AdapterInfo


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    content_hash: str
    size: int
    content_type: str | None = None


def content_key(prefix: str, content_hash: str, suffix: str = "") -> str:
    """`originals/ab/cd/abcd…ef.pdf` — hash-addressed, so re-uploading the same bytes is a
    no-op and a version's `content_ref` is verifiable against `content_hash`."""
    return f"{prefix}/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}{suffix}"


def hash_bytes(data: bytes) -> str:
    """The same digest `put` would compute, without a stream."""
    return hashlib.sha256(data).hexdigest()


def hash_stream(stream: BinaryIO, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(chunk_size), b""):
        digest.update(block)
    stream.seek(0)
    return digest.hexdigest()


class StoragePort(Protocol):
    @property
    def info(self) -> AdapterInfo: ...

    def health(self) -> bool: ...

    def put(
        self, bucket: str, data: BinaryIO, *, suffix: str = "", content_type: str | None = None
    ) -> StoredObject:
        """Store by content hash. Idempotent for identical bytes."""
        ...

    def get(self, bucket: str, key: str) -> bytes: ...

    def presigned_url(self, bucket: str, key: str, expires_seconds: int = 300) -> str: ...

    def exists(self, bucket: str, key: str) -> bool: ...

    def delete(self, bucket: str, key: str) -> None:
        """Only reachable from the retention purge path, which checks `retention_until`
        first (INV-9)."""
        ...
