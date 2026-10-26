"""Media proxy helper tests: HTTP Range 200/206/416, RFC 5987, presign 307 (R-21)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.config import settings
from app.core.storage_backend import (
    LocalStorage,
    content_disposition,
    parse_range_header,
    reset_storage_cache,
    serve_key,
)


@pytest.fixture(autouse=True)
def _clear_storage_cache():
    reset_storage_cache()
    yield
    reset_storage_cache()


@pytest.fixture()
def library(tmp_path: Path) -> LocalStorage:
    storage = LocalStorage(root=tmp_path / "data")
    source = tmp_path / "playback.m4a"
    source.write_bytes(b"0123456789")
    storage.save_file(source, "media/u1/playback.m4a")
    return storage


def make_client(
    storage,
    *,
    filename: str = "会议记录.m4a",
    media_type: str = "audio/mp4",
    **kwargs,
) -> TestClient:
    app = FastAPI()

    @app.get("/media/{key:path}")
    def media(key: str, request: Request, download: bool = False):
        return serve_key(
            storage,
            key,
            filename=filename,
            request_headers=request.headers,
            media_type=media_type,
            download=download,
            **kwargs,
        )

    return TestClient(app)


# ------------------------------------------------------------------ 200 / 206
def test_full_response_headers_and_body(library: LocalStorage) -> None:
    client = make_client(library)
    response = client.get("/media/media/u1/playback.m4a")
    assert response.status_code == 200
    assert response.content == b"0123456789"
    assert response.headers["content-length"] == "10"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert response.headers["content-type"].startswith("audio/mp4")
    assert response.headers["content-disposition"].startswith("inline;")


def test_range_206_partial_content(library: LocalStorage) -> None:
    client = make_client(library)
    response = client.get("/media/media/u1/playback.m4a", headers={"Range": "bytes=2-5"})
    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert response.headers["content-length"] == "4"
    assert response.headers["accept-ranges"] == "bytes"


def test_range_suffix_and_open_ended(library: LocalStorage) -> None:
    client = make_client(library)
    suffix = client.get("/media/media/u1/playback.m4a", headers={"Range": "bytes=-3"})
    assert suffix.status_code == 206
    assert suffix.content == b"789"
    assert suffix.headers["content-range"] == "bytes 7-9/10"

    open_ended = client.get("/media/media/u1/playback.m4a", headers={"Range": "bytes=7-"})
    assert open_ended.status_code == 206
    assert open_ended.content == b"789"
    assert open_ended.headers["content-range"] == "bytes 7-9/10"

    clipped = client.get("/media/media/u1/playback.m4a", headers={"Range": "bytes=8-99"})
    assert clipped.status_code == 206
    assert clipped.content == b"89"
    assert clipped.headers["content-range"] == "bytes 8-9/10"


@pytest.mark.parametrize("range_value", ["bytes=10-20", "bytes=99-", "bytes=abc-def", "bytes=5-2", "chunks=0-5"])
def test_invalid_range_returns_416(library: LocalStorage, range_value: str) -> None:
    client = make_client(library)
    response = client.get("/media/media/u1/playback.m4a", headers={"Range": range_value})
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */10"
    assert response.headers["accept-ranges"] == "bytes"
    payload = response.json()
    assert payload["code"] == "VALIDATION_ERROR"
    assert "Range" in payload["detail"]


# -------------------------------------------------------- Content-Disposition
def test_range_download_disposition_is_attachment(library: LocalStorage) -> None:
    client = make_client(library, filename="会议记录.m4a")
    response = client.get("/media/media/u1/playback.m4a", params={"download": "true"})
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''" in disposition
    assert quote("会议记录.m4a", safe="") in disposition


def test_inline_disposition_rfc5987(library: LocalStorage) -> None:
    client = make_client(library, filename="会议 记录?*.m4a")
    response = client.get("/media/media/u1/playback.m4a")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("inline;")
    # ASCII fallback is sanitised, the RFC 5987 form keeps the real name
    assert 'filename="' in disposition
    assert "filename*=UTF-8''" in disposition
    assert quote("会议 记录?*.m4a", safe="") in disposition


def test_content_disposition_helper() -> None:
    inline = content_disposition("summary.txt", inline=True)
    assert inline.startswith("inline;")
    assert 'filename="summary.txt"' in inline
    download = content_disposition("summary.txt", inline=False)
    assert download.startswith("attachment;")
    # all-non-ascii names still get an ASCII fallback
    fallback = content_disposition("录音", inline=False)
    assert 'filename="download"' in fallback
    assert "filename*=UTF-8''" in fallback


# ---------------------------------------------------------------------- errors
def test_missing_media_returns_404(library: LocalStorage) -> None:
    client = make_client(library)
    response = client.get("/media/media/none/original.mp3")
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
    assert "媒体文件不存在" in response.json()["detail"]


def test_zero_byte_media(library: LocalStorage, tmp_path: Path) -> None:
    empty = tmp_path / "empty.m4a"
    empty.write_bytes(b"")
    library.save_file(empty, "media/u2/playback.m4a")
    client = make_client(library)
    response = client.get("/media/media/u2/playback.m4a")
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == "0"


# ------------------------------------------------------------------ 307 presign
class _PresignS3(LocalStorage):
    name = "s3"
    presign_expires = 300

    def __init__(self, root: Path) -> None:
        super().__init__(root=root)
        self.presign_calls: list[dict] = []

    def presigned_url(self, key: str, expires_sec: int = 3600, *, filename=None, download=False):
        self.presign_calls.append(
            {"key": key, "expires_sec": expires_sec, "filename": filename, "download": download}
        )
        return f"https://minio.example/echoic/{key}?X-Amz-Signature=abc"

    def size(self, key: str) -> int:
        raise AssertionError("presign redirect must not proxy/stat the object")


class _NoPresignS3(LocalStorage):
    name = "s3"

    def presigned_url(self, key: str, expires_sec: int = 3600):
        return None


def test_presign_redirect_307(tmp_path: Path) -> None:
    storage = _PresignS3(tmp_path / "data")
    client = make_client(storage, presign_redirect=True)
    response = client.get("/media/media/u1/playback.m4a", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == (
        "https://minio.example/echoic/media/u1/playback.m4a?X-Amz-Signature=abc"
    )
    assert response.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert storage.presign_calls == [
        {
            "key": "media/u1/playback.m4a",
            "expires_sec": 300,
            "filename": "会议记录.m4a",
            "download": False,
        }
    ]


def test_presign_redirect_honours_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = _PresignS3(tmp_path / "data")
    monkeypatch.setattr(settings, "s3_presign_redirect", True)
    client = make_client(storage)
    response = client.get("/media/media/u1/playback.m4a", follow_redirects=False)
    assert response.status_code == 307


def test_presign_falls_back_to_proxy(tmp_path: Path) -> None:
    storage = _NoPresignS3(tmp_path / "data")
    source = tmp_path / "playback.m4a"
    source.write_bytes(b"0123456789")
    storage.save_file(source, "media/u1/playback.m4a")
    client = make_client(storage, presign_redirect=True)
    response = client.get("/media/media/u1/playback.m4a")
    assert response.status_code == 200
    assert response.content == b"0123456789"


def test_local_backend_never_redirects(library: LocalStorage) -> None:
    client = make_client(library, presign_redirect=True)
    response = client.get("/media/media/u1/playback.m4a", follow_redirects=False)
    assert response.status_code == 200


# ------------------------------------------------------------ range parsing unit
@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        ("bytes=0-0", 10, (0, 0)),
        ("bytes=0-9", 10, (0, 9)),
        ("bytes=5-", 10, (5, 9)),
        ("bytes=-4", 10, (6, 9)),
        ("bytes=-100", 10, (0, 9)),
        ("bytes=8-99", 10, (8, 9)),
        ("bytes=10-", 10, None),
        ("bytes=5-2", 10, None),
        ("bytes=abc", 10, None),
        ("items=0-1", 10, None),
        ("bytes=0-1,3-4", 10, None),
        ("", 10, None),
        ("bytes=0-1", 0, None),
    ],
)
def test_parse_range_header(header, size, expected) -> None:
    assert parse_range_header(header, size) == expected
