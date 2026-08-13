"""Filesystem `StoragePort`, for tests and single-node dev without MinIO.

Same key layout as S3 so a fixture written here is byte-identical to one written there, and
switching adapters cannot change what `content_ref` means.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import BinaryIO

from kb_common.errors import NotFound

from kb_ports.base import AdapterInfo
from kb_ports.registry import PortName, register_adapter
from kb_ports.storage import StoredObject, content_key, hash_stream


class LocalStorageAdapter:
    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(name="local", version="1", endpoint=str(self._root))

    def health(self) -> bool:
        return self._root.is_dir()

    def _path(self, bucket: str, key: str) -> Path:
        return self._root / bucket / key

    def put(
        self, bucket: str, data: BinaryIO, *, suffix: str = "", content_type: str | None = None
    ) -> StoredObject:
        digest = hash_stream(data)
        key = content_key("originals", digest, suffix)
        target = self._path(bucket, key)
        payload = data.read()
        data.seek(0)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        return StoredObject(
            key=key, content_hash=digest, size=len(payload), content_type=content_type
        )

    def get(self, bucket: str, key: str) -> bytes:
        target = self._path(bucket, key)
        if not target.is_file():
            raise NotFound("object not found", bucket=bucket, key=key)
        return target.read_bytes()

    def presigned_url(self, bucket: str, key: str, expires_seconds: int = 300) -> str:
        # No signing locally; the portal only ever receives these through portal-api, which
        # has already re-checked access (gate 3).
        return f"file://{self._path(bucket, key)}"

    def exists(self, bucket: str, key: str) -> bool:
        return self._path(bucket, key).is_file()

    def delete(self, bucket: str, key: str) -> None:
        self._path(bucket, key).unlink(missing_ok=True)

    def clear(self) -> None:
        """Tests only."""
        shutil.rmtree(self._root, ignore_errors=True)
        self._root.mkdir(parents=True, exist_ok=True)


@register_adapter(PortName.STORAGE, "local")
def build_local_storage(root: Path | str = "/tmp/kb-storage") -> LocalStorageAdapter:
    return LocalStorageAdapter(root)
