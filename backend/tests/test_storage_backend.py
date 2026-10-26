"""Contract tests for the local storage backend + factory + health status (R-21).

No network / no DB: everything runs against ``tmp_path`` and monkeypatched
settings.  S3 coverage lives in ``test_storage_s3.py`` (moto).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import settings
from app.core import storage as core_storage
from app.core.storage_backend import (
    S3Storage,
    StorageBackend,
    StorageError,
    LocalStorage,
    asr_wav_key,
    backend_name,
    fetch_to_local,
    file_size,
    get_storage,
    normalize_key,
    normalize_prefix,
    original_key,
    playback_key,
    recording_original_key,
    recording_playback_key,
    recording_prefix,
    reset_storage_cache,
    staging_dir,
    storage_status,
)

CHUNK = 256 * 1024


@pytest.fixture(autouse=True)
def _clear_storage_cache():
    reset_storage_cache()
    yield
    reset_storage_cache()


@pytest.fixture()
def local(tmp_path: Path) -> LocalStorage:
    return LocalStorage(root=tmp_path / "data")


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# --------------------------------------------------------------- local contract
def test_local_save_download_roundtrip(local: LocalStorage, tmp_path: Path) -> None:
    source = _write(tmp_path / "upload.tmp", b"hello-echoic")
    size = local.save_file(source, "media/abc/original.mp3")
    assert size == len(b"hello-echoic")
    assert source.exists(), "save_file must not consume the staging file"
    stored = tmp_path / "data" / "media" / "abc" / "original.mp3"
    assert stored.read_bytes() == b"hello-echoic"
    assert local.exists("media/abc/original.mp3") is True
    assert local.size("media/abc/original.mp3") == size

    dest = tmp_path / "playback-copy.m4a"
    local.download_file("media/abc/original.mp3", dest)
    assert dest.read_bytes() == b"hello-echoic"
    assert not list(dest.parent.glob("*.part-*")), "atomic copy temp file leaked"


def test_local_layout_matches_v02(tmp_path: Path) -> None:
    """Local keys map 1:1 to the v0.2 ``<data_dir>/media/<uuid>/`` layout."""
    assert os.environ["ECHOIC_DATA_DIR"]  # conftest provides an isolated data dir
    storage = LocalStorage(root=tmp_path / "data")
    source = _write(tmp_path / "u", b"x")
    storage_dir = recording_prefix(f"media/{'0' * 8}-uuid")
    storage.save_file(source, f"{storage_dir}/playback.m4a")
    assert (tmp_path / "data" / storage_dir / "playback.m4a").is_file()


def test_local_open_stream_and_range_chunks(local: LocalStorage, tmp_path: Path) -> None:
    payload = bytes(range(256)) * (CHUNK * 2 // 256 + 16)  # > 2 chunks
    source = _write(tmp_path / "big.bin", payload)
    local.save_file(source, "media/big/original.wav")
    chunks = list(local.open_stream("media/big/original.wav"))
    assert b"".join(chunks) == payload
    assert len(chunks) > 1
    assert all(len(chunk) <= CHUNK for chunk in chunks)

    partial = b"".join(local.open_range("media/big/original.wav", 1000, 2 * CHUNK + 5))
    assert partial == payload[1000: 1000 + 2 * CHUNK + 5]

    tail = b"".join(local.open_range("media/big/original.wav", len(payload) - 3, 999))
    assert tail == payload[-3:], "range is clipped to the file size"


@pytest.mark.parametrize(("start", "length"), [(-1, 10), (0, 0), (0, -5)])
def test_local_open_range_rejects_invalid_args(
    local: LocalStorage, tmp_path: Path, start: int, length: int
) -> None:
    source = _write(tmp_path / "x", b"0123456789")
    local.save_file(source, "media/r/original.mp3")
    with pytest.raises(StorageError):
        local.open_range("media/r/original.mp3", start, length)


def test_local_open_range_past_eof_raises(local: LocalStorage, tmp_path: Path) -> None:
    source = _write(tmp_path / "x", b"0123456789")
    local.save_file(source, "media/r/original.mp3")
    with pytest.raises(StorageError):
        local.open_range("media/r/original.mp3", 10, 5)


def test_local_missing_objects_are_friendly(local: LocalStorage) -> None:
    assert local.exists("media/none/original.mp3") is False
    with pytest.raises(StorageError) as excinfo:
        local.size("media/none/original.mp3")
    assert "媒体文件不存在" in str(excinfo.value)
    with pytest.raises(StorageError):
        local.download_file("media/none/original.mp3", Path("/tmp/never-used.bin"))
    with pytest.raises(StorageError):
        list(local.open_stream("media/none/original.mp3"))
    with pytest.raises(StorageError):
        local.save_file(Path("/nonexistent/upload.tmp"), "media/none/original.mp3")


def test_local_list_prefix_sorted_and_scoped(local: LocalStorage, tmp_path: Path) -> None:
    for key in ("media/a/playback.m4a", "media/a/original.mp3", "media/b/original.mp4"):
        local.save_file(_write(tmp_path / key.replace("/", "_"), key.encode()), key)
    assert local.list_prefix("media/a/") == ["media/a/original.mp3", "media/a/playback.m4a"]
    assert local.list_prefix("media/") == [
        "media/a/original.mp3",
        "media/a/playback.m4a",
        "media/b/original.mp4",
    ]
    # a partial key behaves like the S3 prefix semantics
    assert local.list_prefix("media/a/orig") == ["media/a/original.mp3"]
    assert local.list_prefix("missing/") == []


def test_local_delete_prefix_idempotent_and_sibling_safe(local: LocalStorage, tmp_path: Path) -> None:
    for key in ("media/a/original.mp3", "media/a/playback.m4a", "media/ab/original.mp3"):
        local.save_file(_write(tmp_path / key.replace("/", "_"), b"x"), key)
    assert local.delete_prefix("media/a") == 2
    assert local.list_prefix("media/a/") == []
    assert local.exists("media/ab/original.mp3"), "sibling prefix must not be deleted"
    assert local.delete_prefix("media/a") == 0
    with pytest.raises(StorageError):
        local.delete_prefix("")


@pytest.mark.parametrize(
    "key",
    [
        "../etc/passwd",
        "media/../../etc/passwd",
        "/absolute/path.mp3",
        "media/./../escape.mp3",
        "media\\..\\..\\escape.mp3",
    ],
)
def test_local_rejects_path_traversal(local: LocalStorage, tmp_path: Path, key: str) -> None:
    source = _write(tmp_path / "upload", b"x")
    with pytest.raises(StorageError):
        local.save_file(source, key)
    with pytest.raises(StorageError):
        local.size(key)
    with pytest.raises(StorageError):
        local.list_prefix(key)


def test_local_does_not_follow_symlinks_outside_root(local: LocalStorage, tmp_path: Path) -> None:
    outside = _write(tmp_path / "secret.txt", b"top-secret")
    media = tmp_path / "data" / "media"
    media.mkdir(parents=True, exist_ok=True)
    (media / "link.mp3").symlink_to(outside)
    with pytest.raises(StorageError):
        local.size("media/link.mp3")
    with pytest.raises(StorageError):
        list(local.open_stream("media/link.mp3"))


def test_local_misc_contract(local: LocalStorage, tmp_path: Path) -> None:
    source = _write(tmp_path / "clip.m4a", b"m4a")
    local.save_file(source, "media/c/playback.m4a")
    assert local.presigned_url("media/c/playback.m4a") is None
    assert local.local_path_if_any("media/c/playback.m4a") == (
        tmp_path / "data" / "media" / "c" / "playback.m4a"
    )
    assert local.local_path_if_any("media/c/missing.bin") is None
    assert isinstance(local, StorageBackend), "LocalStorage must satisfy the protocol"

    status = local.status()
    assert status["backend"] == "local"
    assert status["status"] == "ok"
    assert "本地存储可用" in status["detail"]
    assert status["free_bytes"] > 0


def test_local_status_degraded_when_root_missing(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path / "does-not-exist")
    status = storage.status()
    assert status["status"] == "degraded"
    assert "本地存储" in status["detail"]


# ----------------------------------------------------------------- key helpers
def test_key_helpers() -> None:
    assert recording_prefix("media/uuid-1") == "media/uuid-1"
    assert recording_prefix("", "uuid-1") == "media/uuid-1"
    assert original_key("media/uuid-1", "Meeting.MP3") == "media/uuid-1/original.mp3"
    assert original_key("media/uuid-1", "", ".wav") == "media/uuid-1/original.wav"
    assert original_key("media/uuid-1", "noext") == "media/uuid-1/original.bin"
    assert playback_key("media/uuid-1") == "media/uuid-1/playback.m4a"
    assert asr_wav_key("media/uuid-1") == "media/uuid-1/asr.wav"
    with pytest.raises(StorageError):
        recording_prefix("")
    assert normalize_key(" media//x/./y ") == "media/x/y"
    assert normalize_prefix("media/x") == "media/x/"
    assert normalize_prefix("") == ""


def test_recording_media_key_lookup(local: LocalStorage, tmp_path: Path) -> None:
    local.save_file(_write(tmp_path / "o", b"original"), "media/u1/original.mp3")
    local.save_file(_write(tmp_path / "p", b"playback"), "media/u1/playback.m4a")
    assert recording_original_key(local, "media/u1", ".mp3") == "media/u1/original.mp3"
    # extension mismatch falls back to the single original.* object
    assert recording_original_key(local, "media/u1", ".wav") == "media/u1/original.mp3"
    assert recording_playback_key(local, "media/u1") == "media/u1/playback.m4a"
    assert recording_original_key(local, "media/none", ".mp3") is None
    assert recording_playback_key(local, "media/none") is None
    assert recording_original_key(local, "", ".mp3") is None
    assert file_size(local, "media/u1/original.mp3") == len(b"original")
    assert file_size(local, "media/u1/missing.bin") == 0


def test_fetch_to_local_and_staging_dir(
    local: LocalStorage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    source = _write(tmp_path / "o", b"payload")
    local.save_file(source, "media/stage/original.mp3")
    # local backend: no copy, the stored path is returned directly
    resolved = fetch_to_local(local, "media/stage/original.mp3", tmp_path / "stage.bin")
    assert resolved.read_bytes() == b"payload"
    assert resolved.parent == tmp_path / "data" / "media" / "stage"

    staging = staging_dir("test-stage-")
    assert staging.is_dir()
    assert staging.parent == tmp_path / "data" / "tmp"


# --------------------------------------------------------- factory / selection
def test_backend_name_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "storage_backend", "LOCAL")
    assert backend_name() == "local"
    monkeypatch.setattr(settings, "storage_backend", "s3")
    assert backend_name() == "s3"
    monkeypatch.setattr(settings, "storage_backend", "minio")
    assert backend_name() == "s3"
    monkeypatch.setattr(settings, "storage_backend", "ceph")
    assert backend_name() == "local"
    monkeypatch.setattr(settings, "storage_backend", "")
    assert backend_name() == "local"


def test_get_storage_defaults_to_local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    storage = get_storage()
    assert isinstance(storage, LocalStorage)
    assert get_storage() is storage, "factory caches the backend instance"


def test_get_storage_selects_s3_without_connecting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "s3_bucket", "echoic-test")
    monkeypatch.setattr(settings, "s3_endpoint", "http://minio:9000")
    monkeypatch.setattr(settings, "s3_access_key", "key")
    monkeypatch.setattr(settings, "s3_secret_key", "secret")
    monkeypatch.setattr(settings, "s3_path_style", True)
    monkeypatch.setattr(settings, "s3_prefix", "echoic/")
    storage = get_storage()
    assert isinstance(storage, S3Storage)
    assert storage.bucket == "echoic-test"
    assert storage.endpoint == "http://minio:9000"
    assert storage.prefix == "echoic/"
    # lazy: the boto3 client must not be created by the factory
    assert storage._client_obj is None
    assert "对象存储" in storage.status()["detail"] or storage.status()["status"] in {"ok", "degraded"}


def test_get_storage_picks_up_config_change(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    assert isinstance(get_storage(), LocalStorage)
    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(settings, "s3_bucket", "bucket-x")
    assert isinstance(get_storage(), S3Storage)
    monkeypatch.setattr(settings, "storage_backend", "local")
    assert isinstance(get_storage(), LocalStorage)


def test_build_storage_explicit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.core.storage_backend import build_storage

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    assert isinstance(build_storage("local"), LocalStorage)
    s3 = build_storage("s3")
    assert isinstance(s3, S3Storage)


# --------------------------------------------------------------- health status
def test_storage_status_local_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    status = storage_status(force=True)
    assert status["backend"] == "local"
    assert status["status"] == "ok"
    assert "本地存储可用" in status["detail"]


def test_storage_status_s3_missing_config_is_degraded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "s3_bucket", "")
    status = storage_status(force=True)
    assert status["backend"] == "s3"
    assert status["status"] == "degraded"
    assert "bucket" in status["detail"]


def test_storage_status_never_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    def boom(*args, **kwargs):
        raise RuntimeError("factory exploded")

    import app.core.storage_backend as storage_backend_module

    monkeypatch.setattr(storage_backend_module, "get_storage", boom)
    status = storage_status(force=True)
    assert status["status"] == "degraded"
    assert "初始化失败" in status["detail"]


def test_storage_status_reflects_config_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "storage_backend", "local")
    assert storage_status()["status"] == "ok"
    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(settings, "s3_bucket", "")
    assert storage_status()["backend"] == "s3"
    assert storage_status()["status"] == "degraded"


def test_storage_error_compatible_with_core_storage_error() -> None:
    """Existing pipeline code catches ``core.storage.StorageError``."""
    assert issubclass(StorageError, core_storage.StorageError)
    with pytest.raises(core_storage.StorageError):
        raise StorageError("媒体文件不存在：x")
