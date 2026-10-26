"""S3 end-to-end integration: upload -> pipeline staging -> media Range serving.

Uses moto to avoid network/real MinIO. Skips when ffmpeg is unavailable because
the pipeline needs it to normalize the uploaded media and produce playback.m4a.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.storage_backend import reset_storage_cache
from app.db import Base, engine
from app.main import create_app

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe required for S3 pipeline integration",
)

BUCKET = "echoic-s3-test"


def _write_wav(path, seconds: int = 4) -> None:
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
            f"sine=frequency=440:duration={seconds}",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(path),
        ],
        check=True,
    )


def test_s3_upload_pipeline_and_range_media(tmp_path, monkeypatch):
    from moto import mock_aws

    with mock_aws():
        import boto3

        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        monkeypatch.setattr(settings, "storage_backend", "s3")
        monkeypatch.setattr(settings, "s3_bucket", BUCKET)
        monkeypatch.setattr(settings, "s3_access_key", "testing")
        monkeypatch.setattr(settings, "s3_secret_key", "testing")
        monkeypatch.setattr(settings, "s3_region", "us-east-1")
        monkeypatch.setattr(settings, "s3_path_style", True)
        monkeypatch.setattr(settings, "s3_endpoint", "")
        monkeypatch.setattr(settings, "s3_prefix", "")
        reset_storage_cache()
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        application = create_app()
        try:
            with TestClient(application) as client:
                login = client.post(
                    "/api/auth/login", json={"username": "admin", "password": "admin123"}
                )
                assert login.status_code == 200, login.text
                token = login.json()["token"]
                headers = {"Authorization": f"Bearer {token}"}

                wav = tmp_path / "s3-meeting.wav"
                _write_wav(wav)
                with wav.open("rb") as handle:
                    upload = client.post(
                        "/api/recordings/upload",
                        headers=headers,
                        files={"file": ("s3-meeting.wav", handle, "audio/wav")},
                        data={"title": "S3 集成测试会议", "language": "zh"},
                    )
                assert upload.status_code == 201, upload.text
                recording = upload.json()
                recording_id = recording["id"]

                # original must be in S3, not on local disk
                keys = [
                    item["Key"]
                    for item in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])
                ]
                assert any(k.endswith("/original.wav") for k in keys), keys

                from app.services.pipeline import process_recording

                asyncio.run(process_recording(recording_id, "all"))
                detail = client.get(f"/api/recordings/{recording_id}", headers=headers).json()
                assert detail["status"] == "ready", detail
                transcript = client.get(
                    f"/api/recordings/{recording_id}/transcript", headers=headers
                )
                assert transcript.status_code == 200
                assert len(transcript.json()["segments"]) > 0

                keys = [
                    item["Key"]
                    for item in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])
                ]
                assert any(k.endswith("/playback.m4a") for k in keys), keys

                media = client.get(
                    f"/api/recordings/{recording_id}/media",
                    headers={**headers, "Range": "bytes=0-127"},
                )
                assert media.status_code == 206, media.text
                assert media.headers.get("content-range", "").startswith("bytes 0-127/")
                assert len(media.content) == 128
                assert media.headers.get("content-type") == "audio/mp4"
        finally:
            reset_storage_cache()
