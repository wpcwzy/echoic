"""Video slide/scene extraction tests (v1.0).

Covers:
* ``core.media.extract_video_scenes`` scene-cut detection, dedupe, caps, errors.
* storage helpers (local dir / S3 keys) with path-traversal validation.
* pipeline integration: video_scenes rows + JPEG files after processing, best
  effort (disabled detection / extraction failure never fails the recording).
* ``GET /api/recordings/{id}/scenes`` + ``.../scenes/{scene_id}/image`` API
  (auth, permissions, 404 cases, response shape for the frontend).
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.core import media, storage
from app.core import storage_backend
from app.core.security import create_access_token
from app.core.utils import utcnow
from app.db import Base, SessionLocal, init_db
from app.models import Recording, User, VideoScene
from app.services.pipeline import process_recording
from app.services.queue import enqueue
from tests.test_api_helpers import ffmpeg_available, make_recording

needs_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe unavailable")

FAKE_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


# --------------------------------------------------------------------- fixtures
@pytest.fixture()
def database():
    init_db()
    with SessionLocal() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    with SessionLocal() as session:
        yield session


def _run_ffmpeg(args: list[str]) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
    )


def _make_cuts_video(path: Path, *, segment_seconds: int = 2, with_audio: bool = True) -> None:
    """3 hard scene cuts (testsrc -> smptebars -> rgbtestsrc), optional AAC audio."""
    path.parent.mkdir(parents=True, exist_ok=True)
    size = "320x180"
    args: list[str] = [
        "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:duration={segment_seconds}",
        "-f", "lavfi", "-i", f"smptebars=size={size}:rate=25:duration={segment_seconds}",
        "-f", "lavfi", "-i", f"rgbtestsrc=size={size}:rate=25:duration={segment_seconds}",
    ]
    filters = "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]"
    maps = ["-map", "[v]"]
    if with_audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={segment_seconds * 3}"]
        maps += ["-map", "3:a", "-shortest"]
    args += [
        "-filter_complex", filters, *maps,
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    ]
    if with_audio:
        args += ["-c:a", "aac"]
    args.append(str(path))
    _run_ffmpeg(args)


def _make_solid_video(path: Path, *, seconds: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        [
            "-f", "lavfi", "-i", f"color=c=red:s=320x180:d={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(path),
        ]
    )


def _make_quick_cuts_video(path: Path, *, segment_seconds: float = 0.6, segments: int = 6) -> None:
    """Cuts closer than the dedupe gap (0.6s) to exercise min-gap filtering."""
    path.parent.mkdir(parents=True, exist_ok=True)
    patterns = ["testsrc", "smptebars", "rgbtestsrc"]
    args: list[str] = []
    inputs: list[str] = []
    concat_inputs = ""
    for index in range(segments):
        pattern = patterns[index % len(patterns)]
        durations = f"{segment_seconds}"
        if pattern == "testsrc":
            args += ["-f", "lavfi", "-i", f"testsrc=size=320x180:rate=25:duration={durations}"]
        else:
            args += ["-f", "lavfi", "-i", f"{pattern}=size=320x180:duration={durations}"]
        inputs.append(f"[{index}:v]")
        concat_inputs += f"[{index}:v]"
    args += [
        "-filter_complex", f"{concat_inputs}concat=n={segments}:v=1:a=0[v]",
        "-map", "[v]",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(path),
    ]
    _run_ffmpeg(args)


# --------------------------------------------------------------- media extraction
@needs_ffmpeg
def test_extract_video_scenes_detects_cuts(tmp_path):
    source = tmp_path / "cuts.mp4"
    _make_cuts_video(source, segment_seconds=2)
    dest = tmp_path / "frames"
    frames = media.extract_video_scenes(source, dest, settings.ffmpeg, 0.3, 60)

    assert len(frames) >= 2
    times = [frame.time_ms for frame in frames]
    assert times == sorted(times)
    assert times[0] < times[-1]
    # Hard cuts at 2s and 4s (allow ffmpeg decoder timestamp slack).
    assert 1400 <= times[0] <= 2600
    assert 3400 <= times[-1] <= 4600
    for frame in frames:
        assert frame.path.is_file() and frame.path.stat().st_size > 0
        assert frame.path.suffix == ".jpg"
        assert 0 < frame.width <= 360
        assert frame.height > 0
        assert frame.time_ms >= 0


@needs_ffmpeg
def test_extract_video_scenes_no_changes_returns_empty(tmp_path):
    source = tmp_path / "solid.mp4"
    _make_solid_video(source)
    frames = media.extract_video_scenes(source, tmp_path / "out", settings.ffmpeg, 0.3, 60)
    assert frames == []
    assert list((tmp_path / "out").glob("*.jpg")) == []


@needs_ffmpeg
def test_extract_video_scenes_max_scenes_cap(tmp_path):
    source = tmp_path / "cuts.mp4"
    _make_cuts_video(source)
    frames = media.extract_video_scenes(source, tmp_path / "out", settings.ffmpeg, 0.3, 1)
    assert len(frames) == 1
    assert len(list((tmp_path / "out").glob("*.jpg"))) == 1


@needs_ffmpeg
def test_extract_video_scenes_dedupes_close_cuts(tmp_path):
    source = tmp_path / "quick.mp4"
    _make_quick_cuts_video(source)
    frames = media.extract_video_scenes(source, tmp_path / "out", settings.ffmpeg, 0.3, 60)
    assert frames, "quick cuts should still yield at least one scene"
    for previous, current in zip(frames, frames[1:]):
        assert current.time_ms - previous.time_ms >= media.SCENE_MIN_GAP_MS


def test_extract_video_scenes_missing_source_raises(tmp_path):
    with pytest.raises(media.MediaError):
        media.extract_video_scenes(tmp_path / "nope.mp4", tmp_path / "out", settings.ffmpeg, 0.3, 60)


def test_scene_storage_helpers(tmp_path):
    storage_dir = f"media/{uuid.uuid4().hex}"
    directory = storage.scene_dir_path(storage_dir)
    assert directory.name == storage.SCENES_DIRNAME
    assert directory.parent.name == storage_dir.rsplit("/", 1)[-1]
    created = storage.ensure_scene_dir(storage_dir)
    assert created.is_dir()
    image_path = storage.scene_image_path(storage_dir, "scene_0000.jpg")
    assert image_path.parent == created and image_path.name == "scene_0000.jpg"
    for invalid in ("../evil.jpg", "nested/scene.jpg", "scene.png", "", ".."):
        with pytest.raises(storage.StorageError):
            storage.safe_scene_filename(invalid)
    assert storage_backend.scene_key(storage_dir, "scene_0001.jpg").endswith(
        "/scenes/scene_0001.jpg"
    )
    assert storage_backend.scene_prefix(storage_dir).endswith("/scenes")


# --------------------------------------------------------------- pipeline wiring
def _make_video_recording(database, source: Path, *, suffix: str = ".mp4") -> int:
    unique = uuid.uuid4().hex
    directory = storage.recording_dir(unique)
    target = directory / f"original{suffix}"
    shutil.copyfile(source, target)
    recording = Recording(
        uuid=unique,
        title="视频场景测试",
        status="uploaded",
        stage="preparing",
        storage_dir=f"media/{unique}",
        media_type="video",
        mime_type="video/mp4",
        original_filename=f"视频场景测试{suffix}",
        original_ext=suffix,
        size_bytes=target.stat().st_size,
        language="zh",
        source="upload",
    )
    database.add(recording)
    database.commit()
    return int(recording.id)


@needs_ffmpeg
async def test_pipeline_generates_video_scene_rows_and_images(database, tmp_path):
    source = tmp_path / "pipeline.mp4"
    _make_cuts_video(source)
    recording_id = _make_video_recording(database, source)
    enqueue(database, recording_id, "all")
    await process_recording(recording_id, "all")

    with SessionLocal() as session:
        recording = session.get(Recording, recording_id)
        assert recording is not None and recording.status == "ready"
        assert recording.media_type == "video"
        rows = list(
            session.scalars(
                select(VideoScene)
                .where(VideoScene.recording_id == recording_id)
                .order_by(VideoScene.idx)
            ).all()
        )
        assert len(rows) >= 2, "cuts video must produce at least two scene rows"
        assert [row.idx for row in rows] == list(range(len(rows)))
        assert [row.time_ms for row in rows] == sorted(row.time_ms for row in rows)
        scene_dir = storage.scene_dir_path(recording.storage_dir)
        assert scene_dir.is_dir()
        for row in rows:
            assert row.image_filename.startswith("scene_") and row.image_filename.endswith(".jpg")
            image = scene_dir / row.image_filename
            assert image.is_file() and image.stat().st_size > 0
            assert row.width > 0 and row.height > 0
        assert storage.video_playback_path(recording.storage_dir).is_file()

    # API-visible list after processing.
    from app.db import SessionLocal as _Session

    with _Session() as session:
        scene_count = len(
            session.scalars(select(VideoScene).where(VideoScene.recording_id == recording_id)).all()
        )
    assert scene_count == len(rows)
    # Keep the shared test data dir tidy.
    shutil.rmtree(storage.resolve_storage_dir(recording.storage_dir), ignore_errors=True)


@needs_ffmpeg
async def test_pipeline_scene_detection_disabled(database, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "scene_detection_enabled", False)
    source = tmp_path / "disabled.mp4"
    _make_cuts_video(source)
    recording_id = _make_video_recording(database, source)
    enqueue(database, recording_id, "all")
    await process_recording(recording_id, "all")
    with SessionLocal() as session:
        recording = session.get(Recording, recording_id)
        assert recording is not None and recording.status == "ready"
        assert session.scalars(
            select(VideoScene).where(VideoScene.recording_id == recording_id)
        ).all() == []
    shutil.rmtree(storage.resolve_storage_dir(recording.storage_dir), ignore_errors=True)


@needs_ffmpeg
async def test_pipeline_scene_failure_is_non_fatal(database, tmp_path, monkeypatch):
    source = tmp_path / "failing.mp4"
    _make_cuts_video(source)
    recording_id = _make_video_recording(database, source)

    def boom(*args, **kwargs):
        raise media.MediaError("模拟场景提取失败")

    monkeypatch.setattr(media, "extract_video_scenes", boom)
    enqueue(database, recording_id, "all")
    await process_recording(recording_id, "all")
    with SessionLocal() as session:
        recording = session.get(Recording, recording_id)
        assert recording is not None and recording.status == "ready"
        assert session.scalars(
            select(VideoScene).where(VideoScene.recording_id == recording_id)
        ).all() == []
    shutil.rmtree(storage.resolve_storage_dir(recording.storage_dir), ignore_errors=True)


# ------------------------------------------------------------------- API checks
def _member(db, username: str) -> tuple[int, str]:
    user = User(
        username=username,
        password_hash="not-used",
        display_name=username,
        is_admin=False,
        role="member",
        disabled=False,
    )
    db.add(user)
    db.commit()
    return int(user.id), create_access_token(int(user.id))


def _drop_admin_cookie(client) -> None:
    try:
        client.cookies.delete("echoic_token")
    except Exception:  # pragma: no cover - cookie jar variations
        client.cookies.clear()
    client.headers.pop("Authorization", None)


def _add_scene(db, recording: Recording, idx: int, time_ms: int, *, write_image: bool = True) -> VideoScene:
    filename = f"scene_{idx:04d}.jpg"
    row = VideoScene(
        recording_id=recording.id,
        idx=idx,
        time_ms=time_ms,
        image_filename=filename,
        width=320,
        height=180,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    if write_image:
        directory = storage.ensure_scene_dir(recording.storage_dir)
        (directory / filename).write_bytes(FAKE_JPEG)
    return row


def test_list_scenes_api_shape_and_order(auth_client, db):
    recording = make_recording(
        db,
        title="带画面时间线的视频",
        media_type="video",
        mime_type="video/mp4",
        original_ext=".mp4",
        original_filename="带画面时间线的视频.mp4",
    )
    second = _add_scene(db, recording, 1, 4000)
    first = _add_scene(db, recording, 0, 0)

    response = auth_client.get(f"/api/recordings/{recording.id}/scenes")
    assert response.status_code == 200, response.text
    body = response.json()
    assert list(body.keys()) == ["items"]
    items = body["items"]
    assert [item["idx"] for item in items] == [0, 1]
    assert items[0] == {
        "id": first.id,
        "recording_id": recording.id,
        "idx": 0,
        "time_ms": 0,
        "image_filename": "scene_0000.jpg",
        "width": 320,
        "height": 180,
    }
    assert items[1]["id"] == second.id


def test_scene_image_endpoint_serves_jpeg(auth_client, db):
    recording = make_recording(
        db,
        media_type="video",
        mime_type="video/mp4",
        original_ext=".mp4",
        original_filename="画面.jpg.mp4",
    )
    scene = _add_scene(db, recording, 0, 1000)
    response = auth_client.get(f"/api/recordings/{recording.id}/scenes/{scene.id}/image")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.content == FAKE_JPEG
    assert "max-age=3600" in response.headers.get("cache-control", "")
    # Range requests are not required for images but should not break serving.
    ranged = auth_client.get(
        f"/api/recordings/{recording.id}/scenes/{scene.id}/image",
        headers={"Range": "bytes=0-3"},
    )
    assert ranged.status_code in (200, 206)


def test_scene_image_missing_file_404(auth_client, db):
    recording = make_recording(
        db,
        media_type="video",
        mime_type="video/mp4",
        original_ext=".mp4",
        original_filename="缺图.mp4",
    )
    scene = _add_scene(db, recording, 0, 0, write_image=False)
    response = auth_client.get(f"/api/recordings/{recording.id}/scenes/{scene.id}/image")
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_scene_image_wrong_recording_404(auth_client, db):
    first = make_recording(
        db, media_type="video", mime_type="video/mp4", original_ext=".mp4", original_filename="a.mp4"
    )
    second = make_recording(
        db, media_type="video", mime_type="video/mp4", original_ext=".mp4", original_filename="b.mp4"
    )
    scene = _add_scene(db, first, 0, 0)
    response = auth_client.get(f"/api/recordings/{second.id}/scenes/{scene.id}/image")
    assert response.status_code == 404


def test_scenes_member_foreign_recording_404(auth_client, db):
    admin_recording = make_recording(
        db, media_type="video", mime_type="video/mp4", original_ext=".mp4", original_filename="admin.mp4"
    )
    scene = _add_scene(db, admin_recording, 0, 0)
    _, member_token = _member(db, "scene-member-foreign")
    _drop_admin_cookie(auth_client)
    headers = {"Authorization": f"Bearer {member_token}"}
    assert auth_client.get(f"/api/recordings/{admin_recording.id}/scenes", headers=headers).status_code == 404
    assert (
        auth_client.get(
            f"/api/recordings/{admin_recording.id}/scenes/{scene.id}/image", headers=headers
        ).status_code
        == 404
    )


def test_scenes_member_own_recording_allowed(auth_client, db):
    member_id, member_token = _member(db, "scene-member-own")
    recording = make_recording(
        db,
        owner_id=member_id,
        media_type="video",
        mime_type="video/mp4",
        original_ext=".mp4",
        original_filename="member.mp4",
    )
    scene = _add_scene(db, recording, 0, 1500)
    _drop_admin_cookie(auth_client)
    headers = {"Authorization": f"Bearer {member_token}"}
    listing = auth_client.get(f"/api/recordings/{recording.id}/scenes", headers=headers)
    assert listing.status_code == 200
    assert len(listing.json()["items"]) == 1
    image = auth_client.get(
        f"/api/recordings/{recording.id}/scenes/{scene.id}/image", headers=headers
    )
    assert image.status_code == 200


def test_scenes_require_login(client, db):
    recording = make_recording(
        db, media_type="video", mime_type="video/mp4", original_ext=".mp4", original_filename="anon.mp4"
    )
    scene = _add_scene(db, recording, 0, 0)
    assert client.get(f"/api/recordings/{recording.id}/scenes").status_code == 401
    assert (
        client.get(f"/api/recordings/{recording.id}/scenes/{scene.id}/image").status_code == 401
    )


def test_scenes_trashed_recording_404(auth_client, db):
    recording = make_recording(
        db, media_type="video", mime_type="video/mp4", original_ext=".mp4", original_filename="trash.mp4"
    )
    _add_scene(db, recording, 0, 0)
    recording.deleted_at = utcnow()
    db.commit()
    assert auth_client.get(f"/api/recordings/{recording.id}/scenes").status_code == 404


def test_scenes_empty_for_audio_recording(auth_client, db):
    recording = make_recording(db, title="音频录音", media_type="audio")
    response = auth_client.get(f"/api/recordings/{recording.id}/scenes")
    assert response.status_code == 200
    assert response.json() == {"items": []}


class _FakeRemoteBackend:
    """Minimal S3-like backend capturing scene image uploads."""

    name = "s3"

    def __init__(self) -> None:
        self.saved: dict[str, tuple[bytes, str | None]] = {}
        self.deleted_prefixes: list[str] = []

    def save_file(self, local_path: Path, key: str, *, content_type: str | None = None) -> int:
        data = Path(local_path).read_bytes()
        self.saved[key] = (data, content_type)
        return len(data)

    def delete_prefix(self, prefix: str) -> int:
        self.deleted_prefixes.append(prefix)
        return 0

    def exists(self, key: str) -> bool:
        return key in self.saved


@needs_ffmpeg
def test_generate_video_scenes_uploads_to_remote_backend(database, tmp_path):
    """S3 path: frames are uploaded under ``media/<uuid>/scenes/`` and rows recorded."""
    from app.services.pipeline import _generate_video_scenes

    source = tmp_path / "remote.mp4"
    _make_cuts_video(source)
    recording_id = _make_video_recording(database, source)
    with SessionLocal() as session:
        recording = session.get(Recording, recording_id)
        storage_dir = recording.storage_dir
    backend = _FakeRemoteBackend()

    _generate_video_scenes(
        recording_id,
        storage_dir,
        source,
        is_remote=True,
        backend=backend,
        check_cancel=lambda: False,
    )

    keys = sorted(backend.saved)
    assert len(keys) >= 2
    assert all(storage_backend.scene_key(storage_dir, Path(key).name) == key for key in keys)
    assert all(content_type == "image/jpeg" for _, content_type in backend.saved.values())
    assert all(data.startswith(b"\xff\xd8") for data, _ in backend.saved.values())  # JPEG SOI
    with SessionLocal() as session:
        rows = list(
            session.scalars(
                select(VideoScene)
                .where(VideoScene.recording_id == recording_id)
                .order_by(VideoScene.idx)
            ).all()
        )
    assert [row.image_filename for row in rows] == [Path(key).name for key in keys]
    assert all(row.width > 0 and row.height > 0 for row in rows)
    # No local scene directory is written for the remote backend.
    assert not storage.scene_dir_path(storage_dir).exists()
    shutil.rmtree(storage.resolve_storage_dir(storage_dir), ignore_errors=True)
