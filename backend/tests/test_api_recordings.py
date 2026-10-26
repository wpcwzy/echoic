"""API tests: recordings library, upload, media Range, process control (§3)."""

from __future__ import annotations

import pytest

from app.config import settings
from app.models import Recording
from app.services.settings_store import update_settings
from tests.test_api_helpers import (
    add_speakers,
    add_segments,
    add_tag,
    attach_tag,
    ffmpeg_available,
    make_chat_message,
    make_job,
    make_recording,
    make_wav_bytes,
    seed_ready_recording,
    write_media,
)


@pytest.fixture()
def enqueued(monkeypatch):
    calls: list[tuple[int, str]] = []

    from app.services import queue as queue_module

    def fake_enqueue(db, recording_id, from_="all"):
        calls.append((recording_id, from_))

    monkeypatch.setattr(queue_module, "enqueue", fake_enqueue)
    return calls


@pytest.fixture()
def cancel_requests(monkeypatch):
    calls: list[int] = []

    from app.services import queue as queue_module

    def fake_cancel(db, recording_id):
        calls.append(recording_id)
        return True

    monkeypatch.setattr(queue_module, "request_cancel", fake_cancel)
    return calls


# ------------------------------------------------------------------ list / filter
def test_list_recordings_with_stats(auth_client, db):
    make_recording(db, title="Alpha", status="ready", duration_ms=60_000)
    make_recording(db, title="Bravo", status="processing", duration_ms=30_000)
    make_recording(db, title="Charlie", status="failed", duration_ms=10_000)

    response = auth_client.get("/api/recordings")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    assert body["page"] == 1 and body["page_size"] == 24
    stats = body["stats"]
    assert stats["total"] == 3
    assert stats["ready"] == 1
    assert stats["processing"] == 1
    assert stats["failed"] == 1
    assert stats["total_duration_ms"] == 100_000
    assert stats["hours"] == 0.0
    assert stats["this_week"] == 3
    item = body["items"][0]
    for key in (
        "id",
        "uuid",
        "title",
        "status",
        "stage",
        "progress",
        "media_type",
        "recorded_at",
        "tags",
        "speakers",
        "has_transcript",
        "insight_kinds",
        "created_at",
        "updated_at",
    ):
        assert key in item


def test_list_filters_and_sorting(auth_client, db):
    tag = add_tag(db, "组会")
    first = make_recording(db, title="Bravo", status="ready", duration_ms=20_000)
    make_recording(db, title="Alpha", status="failed", duration_ms=40_000)
    make_recording(db, title="Charlie", status="ready", duration_ms=60_000)
    attach_tag(db, first, tag)

    by_status = auth_client.get("/api/recordings", params={"status": "ready,failed"}).json()
    assert by_status["total"] == 3

    ready = auth_client.get("/api/recordings", params={"status": "ready"}).json()
    assert ready["total"] == 2

    by_tag = auth_client.get("/api/recordings", params={"tag_id": tag.id}).json()
    assert by_tag["total"] == 1
    assert by_tag["items"][0]["id"] == first.id
    assert by_tag["items"][0]["tags"][0]["name"] == "组会"

    by_query = auth_client.get("/api/recordings", params={"query": "alph"}).json()
    assert by_query["total"] == 1
    assert by_query["items"][0]["title"] == "Alpha"

    sorted_titles = auth_client.get("/api/recordings", params={"sort": "title"}).json()
    assert [item["title"] for item in sorted_titles["items"]] == ["Alpha", "Bravo", "Charlie"]

    by_duration = auth_client.get("/api/recordings", params={"sort": "duration"}).json()
    assert [item["title"] for item in by_duration["items"]] == ["Charlie", "Alpha", "Bravo"]

    paginated = auth_client.get("/api/recordings", params={"page": 2, "page_size": 2}).json()
    assert paginated["total"] == 3
    assert len(paginated["items"]) == 1


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 1000}])
def test_list_pagination_validation(auth_client, params):
    response = auth_client.get("/api/recordings", params=params)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_list_query_matches_transcript_text(auth_client, db):
    recording = seed_ready_recording(db, with_transcript=True)
    response = auth_client.get("/api/recordings", params={"query": "Graph500"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == recording.id
    assert body["items"][0]["has_transcript"] is True


def test_list_requires_auth(client):
    assert client.get("/api/recordings").status_code == 401


# ------------------------------------------------------------------ detail
def test_recording_detail_with_job_and_chat_count(auth_client, db):
    recording = seed_ready_recording(db, with_transcript=True, with_insights=True)
    make_job(db, recording, status="done", stage="done", progress=100, attempts=1)
    make_chat_message(db, recording, role="user", content="问题一")
    make_chat_message(db, recording, role="assistant", content="回答一")

    response = auth_client.get(f"/api/recordings/{recording.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == recording.id
    assert body["has_transcript"] is True
    assert set(body["insight_kinds"]) == {
        "summary",
        "chapters",
        "viewpoint_tree",
        "insights",
        "action_items",
        "suggested_questions",
        "keywords",
    }
    assert body["job"]["status"] == "done"
    assert body["job"]["progress"] == 100
    assert body["chat_message_count"] == 2


def test_recording_detail_not_found(auth_client):
    response = auth_client.get("/api/recordings/9999")
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


# ------------------------------------------------------------------ upload
def test_upload_wav_creates_recording(auth_client, db, enqueued):
    if not ffmpeg_available():
        pytest.skip("ffmpeg/ffprobe not available")
    payload = make_wav_bytes(duration=1.0)
    tag = add_tag(db, "上传测试")

    response = auth_client.post(
        "/api/recordings/upload",
        files={"file": ("周会录音.wav", payload, "audio/wav")},
        data={
            "title": "周会录音",
            "description": "测试上传",
            "language": "zh",
            "recorded_at": "2026-05-01T10:00:00Z",
            "tag_ids": str(tag.id),
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["title"] == "周会录音"
    assert body["status"] == "queued"
    assert body["stage"] == "preparing"
    assert body["media_type"] == "audio"
    assert body["duration_ms"] > 0
    assert body["size_bytes"] == len(payload)
    assert body["original_filename"] == "周会录音.wav"
    assert body["recorded_at"].startswith("2026-05-01T10:00:00")
    assert [item["name"] for item in body["tags"]] == ["上传测试"]
    assert enqueued == [(body["id"], "all")]

    stored = settings.media_path / body["uuid"] / "original.wav"
    assert stored.is_file()
    assert stored.stat().st_size == len(payload)

    detail = auth_client.get(f"/api/recordings/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["uuid"] == body["uuid"]


def test_upload_rejects_unsupported_extension(auth_client):
    response = auth_client.post(
        "/api/recordings/upload",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 415
    assert response.json()["code"] == "UNSUPPORTED_MEDIA"


def test_upload_rejects_corrupt_media(auth_client):
    if not ffmpeg_available():
        pytest.skip("ffprobe not available")
    response = auth_client.post(
        "/api/recordings/upload",
        files={"file": ("broken.wav", b"this is not a wav file", "audio/wav")},
    )
    assert response.status_code == 415
    assert response.json()["code"] == "UNSUPPORTED_MEDIA"


def test_upload_too_large(auth_client, db):
    update_settings(db, {"system": {"max_upload_mb": 1}})
    oversized = b"0" * (1024 * 1024 + 1024)
    response = auth_client.post(
        "/api/recordings/upload",
        files={"file": ("big.wav", oversized, "audio/wav")},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "UPLOAD_TOO_LARGE"


def test_upload_requires_auth(client):
    response = client.post(
        "/api/recordings/upload", files={"file": ("a.wav", b"x", "audio/wav")}
    )
    assert response.status_code == 401


# ------------------------------------------------------------------ patch / delete
def test_patch_recording_metadata_tags_and_speakers(auth_client, db):
    recording = seed_ready_recording(db, with_transcript=True)
    speakers = list(recording.speakers)
    tag = add_tag(db, "论文")

    response = auth_client.patch(
        f"/api/recordings/{recording.id}",
        json={
            "title": "新的标题",
            "description": "新的描述",
            "language": "en",
            "recorded_at": "2026-01-02T03:04:05Z",
            "tag_ids": [tag.id],
            "speaker_names": {str(speakers[0].id): "王五"},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "新的标题"
    assert body["description"] == "新的描述"
    assert body["language"] == "en"
    assert body["recorded_at"].startswith("2026-01-02T03:04:05")
    assert [item["name"] for item in body["tags"]] == ["论文"]

    transcript = auth_client.get(f"/api/recordings/{recording.id}/transcript").json()
    assert transcript["speakers"][0]["display_name"] == "王五"

    cleared = auth_client.patch(f"/api/recordings/{recording.id}", json={"description": None})
    assert cleared.status_code == 200
    assert cleared.json()["description"] is None


def test_patch_recording_not_found(auth_client):
    response = auth_client.patch("/api/recordings/9999", json={"title": "x"})
    assert response.status_code == 404


def test_delete_recording_moves_to_trash(auth_client, db):
    recording = make_recording(db, title="待删除")
    write_media(recording, original=b"mp3-bytes", playback=b"m4a-bytes")
    directory = settings.media_path / recording.uuid
    assert directory.is_dir()

    response = auth_client.delete(f"/api/recordings/{recording.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["id"] == recording.id
    assert body["deleted_at"] and body["restore_before"]
    # v0.3 soft delete: hidden from APIs, but the row/media survive until purge.
    assert auth_client.get(f"/api/recordings/{recording.id}").status_code == 404
    assert directory.exists()
    db.expire_all()
    stored = db.get(Recording, recording.id)
    assert stored is not None and stored.deleted_at is not None

    # purging from the trash removes DB row + media files
    purge = auth_client.delete(f"/api/recordings/{recording.id}/purge")
    assert purge.status_code == 200
    assert purge.json()["purged"] is True
    assert not directory.exists()
    remaining = db.query(Recording).filter(Recording.id == recording.id).all()
    assert remaining == []
    assert auth_client.delete(f"/api/recordings/{recording.id}/purge").status_code == 404


# ------------------------------------------------------------------ process control
def test_reprocess_enqueues_job(auth_client, db, enqueued):
    recording = seed_ready_recording(db)
    response = auth_client.post(
        f"/api/recordings/{recording.id}/reprocess", json={"from": "insights"}
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "queued"}
    assert enqueued == [(recording.id, "insights")]
    db.expire_all()
    assert db.get(Recording, recording.id).status == "queued"

    no_body = auth_client.post(f"/api/recordings/{recording.id}/reprocess")
    assert no_body.status_code == 200
    assert enqueued[-1] == (recording.id, "all")

    invalid = auth_client.post(
        f"/api/recordings/{recording.id}/reprocess", json={"from": "nonsense"}
    )
    assert invalid.status_code == 422


def test_cancel_recording(auth_client, db, cancel_requests):
    recording = make_recording(db, title="处理中", status="processing", stage="transcribing")
    response = auth_client.post(f"/api/recordings/{recording.id}/cancel")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert cancel_requests == [recording.id]
    db.expire_all()
    assert db.get(Recording, recording.id).status == "cancelled"

    finished = make_recording(db, title="已完成", status="ready")
    conflict = auth_client.post(f"/api/recordings/{finished.id}/cancel")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "CONFLICT"

    missing = auth_client.post("/api/recordings/9999/cancel")
    assert missing.status_code == 404


# ------------------------------------------------------------------ media + Range
def test_media_full_and_range_requests(auth_client, db):
    recording = make_recording(db, title="音频", media_type="audio")
    playback = bytes(range(256)) * 4  # 1024 bytes
    original = b"ORIGINAL-AUDIO-BYTES"
    write_media(recording, original=original, playback=playback)

    full = auth_client.get(f"/api/recordings/{recording.id}/media")
    assert full.status_code == 200
    assert full.content == playback
    assert full.headers["content-type"].startswith("audio/mp4")
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-length"] == str(len(playback))

    part = auth_client.get(
        f"/api/recordings/{recording.id}/media", headers={"Range": "bytes=0-99"}
    )
    assert part.status_code == 206
    assert part.content == playback[:100]
    assert part.headers["content-range"] == f"bytes 0-99/{len(playback)}"
    assert part.headers["content-length"] == "100"

    tail = auth_client.get(
        f"/api/recordings/{recording.id}/media", headers={"Range": "bytes=1000-"}
    )
    assert tail.status_code == 206
    assert tail.content == playback[1000:]

    suffix = auth_client.get(
        f"/api/recordings/{recording.id}/media", headers={"Range": "bytes=-24"}
    )
    assert suffix.status_code == 206
    assert suffix.content == playback[-24:]

    download = auth_client.get(f"/api/recordings/{recording.id}/media?download=1")
    assert download.status_code == 200
    assert download.content == original
    disposition = download.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert "filename*=UTF-8''" in disposition


@pytest.mark.parametrize(
    "range_header",
    ["bytes=abc", "bytes=1024-1025", "bytes=0-1,4-5", "items=0-1"],
)
def test_media_invalid_range_returns_416(auth_client, db, range_header):
    recording = make_recording(db, title="音频")
    write_media(recording, playback=b"0123456789" * 10)  # 100 bytes
    response = auth_client.get(
        f"/api/recordings/{recording.id}/media", headers={"Range": range_header}
    )
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */100"
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_media_selection_for_video_and_original(auth_client, db):
    video = make_recording(
        db,
        title="视频",
        media_type="video",
        mime_type="video/mp4",
        original_ext=".mp4",
        original_filename="视频.mp4",
    )
    write_media(video, ext=".mp4", original=b"VIDEO-BYTES", playback=b"M4A-PLAYBACK")
    video_response = auth_client.get(f"/api/recordings/{video.id}/media")
    assert video_response.status_code == 200
    assert video_response.content == b"VIDEO-BYTES"
    assert video_response.headers["content-type"].startswith("video/mp4")

    audio = make_recording(db, title="纯原始音频")
    write_media(audio, original=b"RAW-AUDIO")
    audio_response = auth_client.get(f"/api/recordings/{audio.id}/media")
    assert audio_response.status_code == 200
    assert audio_response.content == b"RAW-AUDIO"


def test_media_missing_file(auth_client, db):
    recording = make_recording(db, title="无文件")
    response = auth_client.get(f"/api/recordings/{recording.id}/media")
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_media_requires_auth(client, db):
    recording = make_recording(db, title="无文件")
    assert client.get(f"/api/recordings/{recording.id}/media").status_code == 401


# ------------------------------------------------------------------ jobs
def test_list_recording_jobs(auth_client, db):
    recording = seed_ready_recording(db)
    make_job(db, recording, status="failed", stage="analyzing", error="LLM 超时")
    latest = make_job(db, recording, status="done", stage="done", progress=100)
    response = auth_client.get(f"/api/recordings/{recording.id}/jobs")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 2
    assert items[0]["id"] == latest.id
    assert items[0]["status"] == "done"
    assert items[1]["error"] == "LLM 超时"


def test_rename_recording_updates_title_and_download_filename(auth_client, db):
    from urllib.parse import unquote

    recording = make_recording(db, title="旧名称")
    write_media(recording, original=b"ORIGINAL-BYTES", playback=b"PLAYBACK-BYTES")

    response = auth_client.patch(f"/api/recordings/{recording.id}", json={"title": "新名称-复盘"})
    assert response.status_code == 200, response.text
    assert response.json()["title"] == "新名称-复盘"

    detail = auth_client.get(f"/api/recordings/{recording.id}")
    assert detail.json()["title"] == "新名称-复盘"

    download = auth_client.get(f"/api/recordings/{recording.id}/media?download=1")
    assert download.status_code == 200
    disposition = download.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition
    assert unquote(disposition).find("新名称-复盘.mp3") >= 0

    # title-only update must not affect unrelated fields
    assert detail.json()["duration_ms"] == recording.duration_ms
