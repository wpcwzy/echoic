"""Video playback transcoding + media selection tests (v0.3.16)."""

from __future__ import annotations

import json
import subprocess
import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.core import media, storage
from app.db import Base, SessionLocal, init_db
from app.models import Recording
from app.services.pipeline import process_recording
from app.services.queue import enqueue
from tests.test_api_helpers import make_recording, write_media


@pytest.fixture()
def database():
    init_db()
    with SessionLocal() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    with SessionLocal() as session:
        yield session


def _make_test_video(path, seconds: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=640x360:rate=25:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(path),
        ],
        check=True,
    )


@pytest.mark.skipif(not media.ffmpeg_available(settings.ffmpeg), reason="ffmpeg unavailable")
def test_make_video_playback_produces_browser_compatible_mp4(tmp_path):
    source = tmp_path / "source.mov"
    _make_test_video(source)
    target = tmp_path / "playback.mp4"
    media.make_video_playback(source, target, settings.ffmpeg)
    assert target.is_file() and target.stat().st_size > 0
    info = media.probe(target, settings.ffprobe)
    assert info.media_type == "video"
    assert 1500 <= info.duration_ms <= 3000
    proc = subprocess.run(
        [
            settings.ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "json",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    codec = json.loads(proc.stdout)["streams"][0]["codec_name"]
    assert codec == "h264"


@pytest.mark.skipif(not media.ffmpeg_available(settings.ffmpeg), reason="ffmpeg unavailable")
async def test_pipeline_generates_video_playback(database):
    unique = uuid.uuid4().hex
    directory = storage.recording_dir(unique)
    source = directory / "original.mp4"
    _make_test_video(source)
    recording = Recording(
        uuid=unique,
        title="视频转码测试",
        status="uploaded",
        stage="preparing",
        storage_dir=f"media/{unique}",
        media_type="video",
        mime_type="video/mp4",
        original_filename="视频转码测试.mp4",
        original_ext=".mp4",
        size_bytes=source.stat().st_size,
        language="zh",
        source="upload",
    )
    database.add(recording)
    database.commit()
    recording_id = int(recording.id)
    enqueue(database, recording_id, "all")
    await process_recording(recording_id, "all")

    with SessionLocal() as session:
        saved = session.get(Recording, recording_id)
        assert saved is not None and saved.status == "ready"
        playback = storage.video_playback_path(saved.storage_dir)
        assert playback.is_file() and playback.stat().st_size > 0
        assert saved.media_type == "video"
        info = media.probe(playback, settings.ffprobe)
        assert info.media_type == "video"


def test_media_endpoint_prefers_video_playback_but_download_keeps_original(auth_client, db):
    recording = make_recording(
        db,
        title="带转码的视频",
        media_type="video",
        mime_type="video/mp4",
        original_ext=".mp4",
        original_filename="带转码的视频.mp4",
    )
    write_media(recording, original=b"ORIGINAL-VIDEO-BYTES")
    playback = storage.video_playback_path(recording.storage_dir)
    playback.parent.mkdir(parents=True, exist_ok=True)
    playback.write_bytes(b"TRANSCODED-MP4-BYTES")

    inline = auth_client.get(f"/api/recordings/{recording.id}/media")
    assert inline.status_code == 200
    assert inline.content == b"TRANSCODED-MP4-BYTES"
    assert inline.headers["content-type"].startswith("video/mp4")
    assert inline.headers["accept-ranges"] == "bytes"

    download = auth_client.get(f"/api/recordings/{recording.id}/media?download=1")
    assert download.status_code == 200
    assert download.content == b"ORIGINAL-VIDEO-BYTES"
