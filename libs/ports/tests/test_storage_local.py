"""Storage adapter behaviour that the registry relies on."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from kb_common.errors import NotFound
from kb_ports.adapters.storage_local import LocalStorageAdapter

BUCKET = "kb-originals"
# Vietnamese content, deliberately: the byte path must be transparent end to end.
PAYLOAD = "Thông tư 41/2016/TT-NHNN — tỷ lệ an toàn vốn".encode()


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorageAdapter:
    return LocalStorageAdapter(tmp_path)


def test_round_trip_preserves_bytes_exactly(storage: LocalStorageAdapter) -> None:
    stored = storage.put(BUCKET, io.BytesIO(PAYLOAD), suffix=".txt")
    assert storage.get(BUCKET, stored.key) == PAYLOAD


def test_identical_bytes_produce_one_object(storage: LocalStorageAdapter) -> None:
    first = storage.put(BUCKET, io.BytesIO(PAYLOAD), suffix=".pdf")
    second = storage.put(BUCKET, io.BytesIO(PAYLOAD), suffix=".pdf")
    assert first.key == second.key
    assert first.content_hash == second.content_hash


def test_different_bytes_never_collide(storage: LocalStorageAdapter) -> None:
    a = storage.put(BUCKET, io.BytesIO(PAYLOAD))
    b = storage.put(BUCKET, io.BytesIO(PAYLOAD + b"!"))
    assert a.key != b.key


def test_content_hash_verifies_what_was_stored(storage: LocalStorageAdapter) -> None:
    """`document_versions.content_hash` must be checkable against the object."""
    import hashlib

    stored = storage.put(BUCKET, io.BytesIO(PAYLOAD))
    assert stored.content_hash == hashlib.sha256(PAYLOAD).hexdigest()
    assert stored.content_hash in stored.key


def test_missing_object_raises_not_found(storage: LocalStorageAdapter) -> None:
    with pytest.raises(NotFound):
        storage.get(BUCKET, "originals/aa/bb/missing")


def test_exists_and_delete(storage: LocalStorageAdapter) -> None:
    stored = storage.put(BUCKET, io.BytesIO(PAYLOAD))
    assert storage.exists(BUCKET, stored.key)
    storage.delete(BUCKET, stored.key)
    assert not storage.exists(BUCKET, stored.key)


def test_stream_is_left_rewound_for_the_caller(storage: LocalStorageAdapter) -> None:
    stream = io.BytesIO(PAYLOAD)
    storage.put(BUCKET, stream)
    assert stream.read() == PAYLOAD
