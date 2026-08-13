"""MinIO / S3 implementation of `StoragePort`.

Keys are content-hash addressed, which buys three things the registry depends on: re-uploading
identical bytes is a no-op, `document_versions.content_hash` is verifiable against what is
actually stored, and an original can never be silently replaced under a version that cites it.
"""

from __future__ import annotations

import io
from typing import Any, BinaryIO

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from kb_common.config import StorageSettings, get_settings
from kb_common.errors import NotFound, UpstreamError
from kb_common.logging import get_logger

from kb_ports.base import AdapterInfo
from kb_ports.registry import PortName, register_adapter
from kb_ports.storage import StoredObject, content_key, hash_stream

log = get_logger(__name__)


class S3StorageAdapter:
    def __init__(self, settings: StorageSettings | None = None) -> None:
        self._cfg = settings or get_settings().storage
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=self._cfg.endpoint,
            aws_access_key_id=self._cfg.access_key.get_secret_value(),
            aws_secret_access_key=self._cfg.secret_key.get_secret_value(),
            region_name=self._cfg.region,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        )

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(name="s3", version="boto3", endpoint=self._cfg.endpoint)

    def health(self) -> bool:
        try:
            self._client.list_buckets()
        except Exception:  # pragma: no cover - network dependent
            return False
        return True

    def put(
        self, bucket: str, data: BinaryIO, *, suffix: str = "", content_type: str | None = None
    ) -> StoredObject:
        digest = hash_stream(data)
        key = content_key("originals", digest, suffix)
        payload = data.read()
        data.seek(0)

        if self.exists(bucket, key):
            # Same bytes, same key: nothing to write, and nothing to invalidate.
            return StoredObject(
                key=key, content_hash=digest, size=len(payload), content_type=content_type
            )

        extra: dict[str, Any] = {"ContentType": content_type} if content_type else {}
        try:
            self._client.put_object(Bucket=bucket, Key=key, Body=payload, **extra)
        except ClientError as exc:  # pragma: no cover - network dependent
            raise UpstreamError("object store write failed", bucket=bucket, key=key) from exc
        log.info("object_stored", extra={"bucket": bucket, "key": key, "size": len(payload)})
        return StoredObject(
            key=key, content_hash=digest, size=len(payload), content_type=content_type
        )

    def get(self, bucket: str, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise NotFound("object not found", bucket=bucket, key=key) from exc
            raise UpstreamError("object store read failed", bucket=bucket, key=key) from exc
        data: bytes = response["Body"].read()
        return data

    def presigned_url(self, bucket: str, key: str, expires_seconds: int = 300) -> str:
        url: str = self._client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_seconds
        )
        return url

    def exists(self, bucket: str, key: str) -> bool:
        try:
            self._client.head_object(Bucket=bucket, Key=key)
        except ClientError:
            return False
        return True

    def delete(self, bucket: str, key: str) -> None:
        """Reachable only from the retention purge path, after the hold check (INV-9)."""
        self._client.delete_object(Bucket=bucket, Key=key)
        log.warning("object_deleted", extra={"bucket": bucket, "key": key})

    def open(self, bucket: str, key: str) -> BinaryIO:
        return io.BytesIO(self.get(bucket, key))


@register_adapter(PortName.STORAGE, "s3")
def build_s3_storage(settings: StorageSettings | None = None) -> S3StorageAdapter:
    return S3StorageAdapter(settings)
