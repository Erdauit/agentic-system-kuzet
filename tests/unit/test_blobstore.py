"""BlobStore на локальной ФС: атомарная запись, листинг, защита от обхода путей."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from shared.storage import BlobNotFoundError, InvalidBlobKeyError, LocalFSBlobStore


@pytest.fixture
def store(tmp_path: Path) -> LocalFSBlobStore:
    return LocalFSBlobStore(tmp_path / "blobs")


def test_put_get_stat_delete(store: LocalFSBlobStore) -> None:
    info = store.put("clips/2026/10/19/a.mp4", b"video")
    assert info.sha256 == hashlib.sha256(b"video").hexdigest()
    assert store.get("clips/2026/10/19/a.mp4") == b"video"
    assert store.stat("clips/2026/10/19/a.mp4").size == 5
    assert store.exists("clips/2026/10/19/a.mp4")
    store.delete("clips/2026/10/19/a.mp4")
    assert not store.exists("clips/2026/10/19/a.mp4")
    with pytest.raises(BlobNotFoundError):
        store.get("clips/2026/10/19/a.mp4")


def test_put_file_and_list_prefix(store: LocalFSBlobStore, tmp_path: Path) -> None:
    src = tmp_path / "frame.jpg"
    src.write_bytes(b"jpeg" * 1000)
    store.put_file("frames/cam-1/1.jpg", src)
    store.put("clips/x.mp4", b"1")
    assert [b.key for b in store.list("frames/")] == ["frames/cam-1/1.jpg"]
    with store.open("frames/cam-1/1.jpg") as fh:
        assert fh.read(4) == b"jpeg"


@pytest.mark.parametrize("key", ["../etc/passwd", "/abs", "a//b", "a/./b", "dir/", "", "имя.jpg"])
def test_invalid_keys(store: LocalFSBlobStore, key: str) -> None:
    with pytest.raises(InvalidBlobKeyError):
        store.put(key, b"x")


def test_no_partial_files_left_on_failure(store: LocalFSBlobStore, tmp_path: Path) -> None:
    missing = tmp_path / "nope.bin"
    with pytest.raises(FileNotFoundError):
        store.put_file("clips/broken.mp4", missing)
    assert list(store.list()) == []
    assert not any(p.name.startswith(".tmp-") for p in store.root.rglob("*"))
