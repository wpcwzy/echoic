"""S3/MinIO backend contract tests using ``moto`` (no network, R-21).

``moto.mock_aws`` intercepts botocore calls, so the tests exercise the real
boto3 code path (lazy client, Range requests, paginated delete, presigning)
without touching a real bucket.
"""

from __future__ import annotations


from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from moto import mock_aws

from app.core.storage_backend import (
    CHUNK_SIZE,
    S3Storage,
    StorageBackend,
    StorageError,
    _describe_s3_error,
    _friendly_s3_error,
    reset_storage_cache,
)

BUCKET = "echoic-test"


@pytest.fixture(autouse=True)
def _clear_storage_cache():
    reset_storage_cache()
    yield
    reset_storage_cache()


@pytest.fixture()
def moto_s3():
    with mock_aws():
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        client.create_bucket(Bucket=BUCKET)
        yield client


def make_storage(**overrides) -> S3Storage:
    params = dict(
        bucket=BUCKET,
        endpoint="",
        access_key="testing",
        secret_key="testing",
        region="us-east-1",
        path_style=True,
        prefix="",
    )
    params.update(overrides)
    return S3Storage(**params)


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


# ------------------------------------------------------------------ roundtrip
def test_s3_roundtrip(moto_s3, tmp_path: Path) -> None:
    storage = make_storage()
    assert isinstance(storage, StorageBackend)
    source = _write(tmp_path / "original.mp3", b"hello-s3")
    key = "media/u1/original.mp3"

    assert storage.save_file(source, key) == len(b"hello-s3")
    assert source.exists(), "save_file must not consume the staging file"
    assert storage.exists(key) is True
    assert storage.size(key) == len(b"hello-s3")
    assert storage.list_prefix("media/") == [key]
    assert storage.local_path_if_any(key) is None

    dest = tmp_path / "downloaded" / "original.mp3"
    storage.download_file(key, dest)
    assert dest.read_bytes() == b"hello-s3"

    assert b"".join(storage.open_stream(key)) == b"hello-s3"
    assert b"".join(storage.open_range(key, 0, 5)) == b"hello"
    assert b"".join(storage.open_range(key, 6, 2)) == b"s3"

    head = moto_s3.head_object(Bucket=BUCKET, Key=key)
    assert head["ContentType"] == "audio/mpeg", "content type is guessed from the extension"


def test_s3_content_type_override(moto_s3, tmp_path: Path) -> None:
    storage = make_storage()
    source = _write(tmp_path / "playback.m4a", b"audio")
    storage.save_file(source, "media/u1/playback.m4a", content_type="audio/x-custom")
    head = moto_s3.head_object(Bucket=BUCKET, Key="media/u1/playback.m4a")
    assert head["ContentType"] == "audio/x-custom"


def test_s3_open_range_spanning_chunks(moto_s3, tmp_path: Path) -> None:
    storage = make_storage()
    payload = bytes(range(256)) * (CHUNK_SIZE * 2 // 256 + 100)  # > 2 chunks
    source = _write(tmp_path / "big.wav", payload)
    storage.save_file(source, "media/big/original.wav")

    chunks = list(storage.open_stream("media/big/original.wav"))
    assert len(chunks) > 1
    assert all(len(chunk) <= CHUNK_SIZE for chunk in chunks)
    assert b"".join(chunks) == payload

    start, length = 1000, 2 * CHUNK_SIZE + 7
    partial = b"".join(storage.open_range("media/big/original.wav", start, length))
    assert partial == payload[start: start + length]


def test_s3_missing_objects_are_friendly(moto_s3, tmp_path: Path) -> None:
    storage = make_storage()
    missing = "media/none/original.mp3"
    assert storage.exists(missing) is False
    with pytest.raises(StorageError) as excinfo:
        storage.size(missing)
    assert "对象不存在" in str(excinfo.value)
    with pytest.raises(StorageError):
        storage.download_file(missing, tmp_path / "never.bin")
    with pytest.raises(StorageError):
        storage.open_stream(missing)
    # reading past the end of an existing object -> friendly InvalidRange
    storage.save_file(_write(tmp_path / "x", b"0123456789"), "media/r/original.mp3")
    with pytest.raises(StorageError):
        list(storage.open_range("media/r/original.mp3", 100, 5))


def test_s3_delete_prefix_is_scoped_and_idempotent(moto_s3, tmp_path: Path) -> None:
    storage = make_storage()
    for key in ("media/u1/original.mp3", "media/u1/playback.m4a", "media/u2/original.mp3"):
        storage.save_file(_write(tmp_path / key.replace("/", "_"), b"x"), key)
    assert storage.delete_prefix("media/u1") == 2
    assert storage.list_prefix("media/u1/") == []
    assert storage.exists("media/u2/original.mp3")
    assert storage.delete_prefix("media/u1") == 0
    with pytest.raises(StorageError):
        storage.delete_prefix("")


def test_s3_list_and_delete_prefix_paginate(moto_s3) -> None:
    """More than the 1000-key API page size is handled transparently."""
    total = 1005
    for index in range(total):
        moto_s3.put_object(Bucket=BUCKET, Key=f"media/big/part-{index:04d}.bin", Body=b"x")
    storage = make_storage()
    keys = storage.list_prefix("media/big/")
    assert len(keys) == total
    assert keys == sorted(keys)
    assert storage.delete_prefix("media/big/") == total
    assert storage.list_prefix("media/big/") == []


# --------------------------------------------------------------------- prefix
def test_s3_prefix_mode(moto_s3, tmp_path: Path) -> None:
    storage = make_storage(prefix="echoic/")
    key = "media/u1/original.mp3"
    storage.save_file(_write(tmp_path / "o", b"prefixed"), key)
    assert storage.exists(key) and storage.size(key) == len(b"prefixed")
    assert storage.list_prefix("media/") == [key]

    raw = moto_s3.list_objects_v2(Bucket=BUCKET, Prefix="echoic/")
    assert [item["Key"] for item in raw["Contents"]] == ["echoic/media/u1/original.mp3"]

    url = storage.presigned_url(key, expires_sec=120)
    assert url and "echoic/media/u1/original.mp3" in url

    assert storage.delete_prefix("media/u1") == 1
    assert storage.list_prefix("media/") == []


def test_s3_presigned_url(moto_s3, tmp_path: Path) -> None:
    storage = make_storage()
    key = "media/u1/playback.m4a"
    storage.save_file(_write(tmp_path / "p", b"playback"), key)

    url = storage.presigned_url(key, expires_sec=120)
    assert url is not None
    assert key in url
    assert "X-Amz-Signature" in url
    assert "X-Amz-Expires=120" in url

    with_name = storage.presigned_url(key, expires_sec=60, filename="会议记录.m4a", download=True)
    assert with_name is not None
    assert "response-content-disposition" in with_name.lower()
    assert "attachment" in with_name


def test_s3_status_and_ensure_bucket(moto_s3) -> None:
    storage = make_storage()
    status = storage.status()
    assert status["status"] == "ok"
    assert "对象存储可用" in status["detail"]

    missing = make_storage(bucket="missing-bucket")
    degraded = missing.status()
    assert degraded["status"] == "degraded"
    assert "对象存储不可用" in degraded["detail"]

    unconfigured = make_storage(bucket="")
    assert unconfigured.status()["status"] == "degraded"
    assert "bucket" in unconfigured.status()["detail"]

    fresh = make_storage(bucket="brand-new")
    assert fresh.ensure_bucket() is True
    assert fresh.ensure_bucket() is False
    moto_s3.head_bucket(Bucket="brand-new")


def test_s3_config_missing_never_crashes_startup(tmp_path: Path) -> None:
    storage = make_storage(bucket="")
    status = storage.status()  # must not raise even without a bucket/client
    assert status["status"] == "degraded"
    with pytest.raises(StorageError) as excinfo:
        storage.save_file(_write(tmp_path / "x", b"x"), "media/x/original.mp3")
    assert "对象存储未配置 bucket" in str(excinfo.value)
    with pytest.raises(StorageError):
        storage.exists("media/x/original.mp3")


def test_s3_client_is_lazy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def explode(self):
        calls.append("client")
        raise AssertionError("boto3 client must not be created eagerly")

    monkeypatch.setattr(S3Storage, "_build_client", explode)
    storage = make_storage()
    assert calls == [], "constructor/factory must stay network free"
    status = storage.status()  # catches the failure and reports degraded
    assert calls == ["client"]
    assert status["status"] == "degraded"
    with pytest.raises(StorageError):
        storage.size("media/x/original.mp3")


def test_s3_endpoint_normalization() -> None:
    storage = S3Storage(bucket="b", endpoint="minio.internal:9000")
    assert storage.endpoint == "http://minio.internal:9000"
    assert S3Storage(bucket="b", endpoint="https://s3.example.com").endpoint == "https://s3.example.com"


def test_s3_friendly_error_mapping() -> None:
    connection = _friendly_s3_error(
        EndpointConnectionError(endpoint_url="http://localhost:9000"), action="上传媒体文件"
    )
    assert isinstance(connection, StorageError)
    assert "无法连接对象存储服务" in str(connection)

    no_bucket = _friendly_s3_error(
        ClientError({"Error": {"Code": "NoSuchBucket", "Message": "nope"}}, "HeadBucket"),
        action="上传媒体文件",
    )
    assert "bucket 不存在" in str(no_bucket)

    denied = _describe_s3_error(
        ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject")
    )
    assert "访问被拒绝" in denied


def test_s3_save_to_missing_bucket_is_friendly(moto_s3, tmp_path: Path) -> None:
    storage = make_storage(bucket="missing-bucket")
    with pytest.raises(StorageError) as excinfo:
        storage.save_file(_write(tmp_path / "x", b"x"), "media/x/original.mp3")
    message = str(excinfo.value)
    assert "上传媒体文件失败" in message
    assert "bucket" in message


def test_s3_key_traversal_rejected(moto_s3, tmp_path: Path) -> None:
    storage = make_storage()
    source = _write(tmp_path / "x", b"x")
    with pytest.raises(StorageError):
        storage.save_file(source, "../escape.mp3")
    with pytest.raises(StorageError):
        storage.size("/etc/passwd")


def test_s3_empty_object_roundtrip(moto_s3) -> None:
    storage = make_storage()
    storage.client.put_object(Bucket=BUCKET, Key="media/empty/original.mp3", Body=b"")
    assert storage.size("media/empty/original.mp3") == 0
    assert b"".join(storage.open_stream("media/empty/original.mp3")) == b""


# --------------------------------------------------- proxy integration (serve_key)
def test_s3_serve_key_range_proxy(moto_s3, tmp_path: Path) -> None:
    """The media proxy works against S3: full 200 + Range 206 streaming."""
    from app.core.storage_backend import serve_key

    storage = make_storage()
    payload = bytes(range(256)) * 8  # 2048 bytes
    storage.save_file(_write(tmp_path / "playback.m4a", payload), "media/u1/playback.m4a")

    app = FastAPI()

    @app.get("/media/{key:path}")
    def media(key: str, request: Request):
        return serve_key(
            storage,
            key,
            filename="会议记录.m4a",
            request_headers=request.headers,
            media_type="audio/mp4",
        )

    client = TestClient(app)
    full = client.get("/media/media/u1/playback.m4a")
    assert full.status_code == 200, full.text
    assert full.content == payload
    assert full.headers["content-length"] == "2048"

    partial = client.get("/media/media/u1/playback.m4a", headers={"Range": "bytes=100-199"})
    assert partial.status_code == 206
    assert partial.content == payload[100:200]
    assert partial.headers["content-range"] == "bytes 100-199/2048"

    missing = client.get("/media/media/none/original.mp3")
    assert missing.status_code == 404
