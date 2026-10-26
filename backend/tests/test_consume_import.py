"""Consume-directory auto-import tests (R-22).

No network. Uses a per-test consume directory under ``tmp_path``, the shared
test SQLite DB, and real ffprobe for media probing (tests skip when ffprobe is
missing). ``queue.enqueue`` is monkeypatched so imports do not start the real
pipeline.
"""

from __future__ import annotations

import hashlib
import os
import time
import wave
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.core import media
from app.db import Base, SessionLocal, init_db
from app.models import ImportedFile, Recording, User
from app.services import consume as consume_service
from app.services.consume import (
    ScanResult,
    consume_status,
    maybe_scan,
    reset_scan_state,
    scan_now,
    scan_once,
)
from app.services.settings_store import update_settings

FFPROBE_AVAILABLE = media.ffprobe_available(settings.ffprobe)
needs_ffprobe = pytest.mark.skipif(not FFPROBE_AVAILABLE, reason="ffprobe unavailable")


# --------------------------------------------------------------------- fixtures
@pytest.fixture()
def database(db):
    init_db()
    with SessionLocal() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    yield db


@pytest.fixture()
def consume_dir(database, tmp_path, monkeypatch):
    root = tmp_path / "consume"
    root.mkdir()
    monkeypatch.setattr(settings, "consume_dir", str(root))
    monkeypatch.setattr(settings, "consume_enabled", True)
    monkeypatch.setattr(settings, "consume_interval_sec", 5)
    monkeypatch.setattr(settings, "consume_owner_username", "")
    reset_scan_state()
    yield root
    reset_scan_state()


@pytest.fixture()
def enqueued(monkeypatch):
    """Capture queue.enqueue calls instead of running the pipeline."""
    calls: list[tuple[int, str]] = []

    def fake_enqueue(db, recording_id: int, from_: str = "all") -> None:
        calls.append((int(recording_id), from_))

    monkeypatch.setattr(consume_service, "enqueue", fake_enqueue)
    return calls


def _write_wav(path: Path, seconds: float = 2.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


def _age(path: Path, seconds: float = 3600.0) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def _create_user(db, username: str, *, admin: bool = False, role: str = "") -> int:
    user = User(
        username=username,
        password_hash="not-used-in-consume-tests",
        display_name=username,
        is_admin=admin,
        role=role or ("admin" if admin else "member"),
        disabled=False,
    )
    db.add(user)
    db.commit()
    return int(user.id)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _imported_rows(db) -> list[ImportedFile]:
    return list(db.scalars(select(ImportedFile).order_by(ImportedFile.id)).all())


# ----------------------------------------------------------------- import paths
@needs_ffprobe
def test_import_stable_wav_creates_recording_and_imported_file(database, consume_dir, enqueued):
    admin_id = _create_user(database, "admin", admin=True)
    source = _write_wav(consume_dir / "周会录音.wav", seconds=3)
    expected_sha = _sha256(source)
    _age(source)
    mtime = source.stat().st_mtime

    result = scan_once(database, force_stability=False)

    assert isinstance(result, ScanResult)
    assert result.to_dict() == {"scanned": 1, "imported": 1, "duplicates": 0, "failed": 0}
    assert not source.exists(), "imported file must be moved out of the consume directory"
    with SessionLocal() as db:
        recordings = db.scalars(select(Recording)).all()
        assert len(recordings) == 1
        recording = recordings[0]
        assert recording.source == "consume"
        assert recording.title == "周会录音"
        assert recording.original_filename == "周会录音.wav"
        assert recording.original_ext == ".wav"
        assert recording.mime_type.startswith("audio/")
        assert recording.media_type == "audio"
        assert recording.owner_id == admin_id
        assert recording.language == "zh"
        assert recording.duration_ms > 2000
        assert recording.size_bytes > 0
        assert abs((recording.recorded_at - datetime.utcfromtimestamp(mtime)).total_seconds()) < 5
        directory = Path(settings.data_path) / recording.storage_dir
        moved = directory / "original.wav"
        assert moved.exists() and _sha256(moved) == expected_sha

        imported = _imported_rows(db)
        assert len(imported) == 1
        row = imported[0]
        assert row.sha256 == expected_sha
        assert row.status == "imported"
        assert row.recording_id == recording.id
        assert row.filename == "周会录音.wav"
        assert row.error is None
    assert enqueued == [(recordings[0].id, "all")]


@needs_ffprobe
def test_unstable_file_is_imported_on_second_scan(database, consume_dir, enqueued):
    _create_user(database, "admin", admin=True)
    source = _write_wav(consume_dir / "fresh.wav")  # mtime == now, age < 2*interval

    first = scan_once(database, force_stability=False)
    assert first.to_dict() == {"scanned": 1, "imported": 0, "duplicates": 0, "failed": 0}
    assert source.exists()

    second = scan_once(database, force_stability=False)
    assert second.imported == 1
    assert not source.exists()
    assert len(enqueued) == 1


@needs_ffprobe
def test_zero_byte_partial_hidden_and_unsupported_are_skipped(database, consume_dir, enqueued):
    _create_user(database, "admin", admin=True)
    (consume_dir / "zero.wav").write_bytes(b"")
    (consume_dir / "downloading.wav.part").write_bytes(b"x" * 512)
    (consume_dir / "partial.mp3.crdownload").write_bytes(b"x" * 512)
    (consume_dir / ".hidden.wav").write_bytes(b"x" * 512)
    (consume_dir / "notes.txt").write_text("not a media file", encoding="utf-8")

    result = scan_once(database, force_stability=False)

    # Candidates exclude hidden/partial names; zero-byte & unsupported stay put.
    assert result.scanned == 2
    assert result.to_dict()["imported"] == 0
    assert result.failed == 0
    assert sorted(path.name for path in consume_dir.iterdir()) == [
        ".hidden.wav",
        "downloading.wav.part",
        "notes.txt",
        "partial.mp3.crdownload",
        "zero.wav",
    ]
    with SessionLocal() as db:
        assert db.scalars(select(Recording)).all() == []
        assert _imported_rows(db) == []
    assert enqueued == []


@needs_ffprobe
def test_duplicate_content_is_skipped_and_left_in_place(database, consume_dir, enqueued):
    _create_user(database, "admin", admin=True)
    first = _write_wav(consume_dir / "original.wav")
    _age(first)
    assert scan_once(database, force_stability=False).imported == 1

    with SessionLocal() as db:
        recording = db.scalars(select(Recording)).first()
    stored = Path(settings.data_path) / recording.storage_dir / "original.wav"
    duplicate = consume_dir / "原样再投.wav"
    duplicate.write_bytes(stored.read_bytes())
    _age(duplicate)

    result = scan_once(database, force_stability=False)
    assert result.to_dict() == {"scanned": 1, "imported": 0, "duplicates": 1, "failed": 0}
    assert duplicate.exists(), "duplicates are left in place (never silently deleted)"
    with SessionLocal() as db:
        assert len(db.scalars(select(Recording)).all()) == 1
        assert len(_imported_rows(db)) == 1
    assert len(enqueued) == 1

    # The duplicate keeps being reported (idempotent) without new recordings.
    again = scan_once(database, force_stability=False)
    assert again.duplicates == 1 and again.imported == 0


@needs_ffprobe
def test_invalid_audio_is_quarantined_with_error_sidecar(database, consume_dir, enqueued):
    _create_user(database, "admin", admin=True)
    bad = consume_dir / "broken.wav"
    bad.write_bytes(b"this is definitely not an audio container")
    _age(bad)
    bad_sha = _sha256(bad)

    result = scan_once(database, force_stability=False)

    assert result.to_dict() == {"scanned": 1, "imported": 0, "duplicates": 0, "failed": 1}
    assert not bad.exists()
    failed_dir = consume_dir / "failed"
    quarantined = failed_dir / "broken.wav"
    sidecar = failed_dir / "broken.wav.error.txt"
    assert quarantined.exists() and _sha256(quarantined) == bad_sha
    assert sidecar.exists()
    sidecar_text = sidecar.read_text(encoding="utf-8")
    assert "导入失败" in sidecar_text and "broken.wav" in sidecar_text

    with SessionLocal() as db:
        assert db.scalars(select(Recording)).all() == []
        rows = _imported_rows(db)
        assert len(rows) == 1
        assert rows[0].status == "failed"
        assert rows[0].sha256 == bad_sha
        assert rows[0].recording_id is None
        assert rows[0].error and "无法解析" in rows[0].error
    assert enqueued == []

    # Failed files are out of the scan root: a second scan must not loop or crash.
    second = scan_once(database, force_stability=False)
    assert second.to_dict() == {"scanned": 0, "imported": 0, "duplicates": 0, "failed": 0}
    with SessionLocal() as db:
        assert len(_imported_rows(db)) == 1


@needs_ffprobe
def test_failed_file_can_be_retried(database, consume_dir, enqueued, monkeypatch):
    _create_user(database, "admin", admin=True)
    real_probe = consume_service.media.probe
    state = {"calls": 0}

    def flaky_probe(path, ffprobe_bin="ffprobe"):
        state["calls"] += 1
        if state["calls"] == 1:
            raise media.MediaError("模拟解析失败（第一次）")
        return real_probe(path, ffprobe_bin)

    monkeypatch.setattr(consume_service.media, "probe", flaky_probe)

    source = _write_wav(consume_dir / "retry.wav")
    _age(source)
    first = scan_once(database, force_stability=False)
    assert first.failed == 1 and first.imported == 0
    failed_file = consume_dir / "failed" / "retry.wav"
    assert failed_file.exists()
    with SessionLocal() as db:
        row = _imported_rows(db)[0]
        assert row.status == "failed"
        failed_row_id = row.id

    # Put the same content back into the consume directory and scan again.
    source.write_bytes(failed_file.read_bytes())
    _age(source)
    second = scan_once(database, force_stability=False)
    assert second.imported == 1 and second.failed == 0
    assert not source.exists()
    with SessionLocal() as db:
        rows = _imported_rows(db)
        assert len(rows) == 1, "failed dedupe row is replaced/updated in place"
        assert rows[0].id == failed_row_id
        assert rows[0].status == "imported"
        assert rows[0].recording_id is not None
        assert rows[0].error is None
        assert len(db.scalars(select(Recording)).all()) == 1
    assert len(enqueued) == 1


# --------------------------------------------------------------- owner/language
@needs_ffprobe
def test_owner_assignment_config_and_admin_fallback(database, consume_dir, enqueued):
    admin1 = _create_user(database, "admin1", admin=True)
    _create_user(database, "admin2", admin=True)
    alice = _create_user(database, "alice", admin=False)

    source = _write_wav(consume_dir / "owner-a.wav", seconds=3)
    _age(source)
    settings.consume_owner_username = "alice"
    assert scan_once(database, force_stability=False).imported == 1
    with SessionLocal() as db:
        first = db.scalars(select(Recording).order_by(Recording.id)).first()
        assert first.owner_id == alice

    settings.consume_owner_username = ""
    source = _write_wav(consume_dir / "owner-b.wav", seconds=4)
    _age(source)
    assert scan_once(database, force_stability=False).imported == 1
    with SessionLocal() as db:
        second = db.scalars(select(Recording).order_by(Recording.id)).all()[1]
        assert second.owner_id == admin1


@needs_ffprobe
def test_owner_is_none_without_users(database, consume_dir, enqueued):
    source = _write_wav(consume_dir / "no-owner.wav")
    _age(source)
    assert scan_once(database, force_stability=False).imported == 1
    with SessionLocal() as db:
        recording = db.scalars(select(Recording)).one()
        assert recording.owner_id is None


@needs_ffprobe
def test_language_uses_system_default(database, consume_dir, enqueued):
    _create_user(database, "admin", admin=True)
    with SessionLocal() as db:
        update_settings(db, {"system": {"default_language": "en"}})
    source = _write_wav(consume_dir / "english.wav")
    _age(source)
    assert scan_once(database, force_stability=False).imported == 1
    with SessionLocal() as db:
        assert db.scalars(select(Recording)).one().language == "en"


# -------------------------------------------------------- scan_now/status hooks
@needs_ffprobe
def test_scan_now_returns_at_and_result_and_updates_worker_status(database, consume_dir, enqueued):
    _create_user(database, "admin", admin=True)
    _write_wav(consume_dir / "manual.wav")  # fresh: scan_now forces stability
    payload = scan_now()

    assert set(payload) >= {"at", "scanned", "imported", "duplicates", "failed", "result"}
    assert payload["result"] == {
        "scanned": 1,
        "imported": 1,
        "duplicates": 0,
        "failed": 0,
    }
    assert payload["imported"] == 1
    parsed = datetime.fromisoformat(payload["at"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None

    status = consume_status()
    assert status["enabled"] is True
    assert status["dir"] == str(consume_dir)
    assert status["interval_sec"] == 5
    assert status["pending_files"] == 0
    assert status["failed_files"] == 0
    assert status["last_scan"]["imported"] == 1

    from app.worker import worker_status

    last = worker_status()["last_consume_scan"]
    assert last is not None and last["imported"] == 1 and last["at"] == payload["at"]


@needs_ffprobe
def test_consume_status_counts_pending_and_failed(database, consume_dir, enqueued):
    _create_user(database, "admin", admin=True)
    _write_wav(consume_dir / "pending.wav")
    bad = consume_dir / "bad.wav"
    bad.write_bytes(b"garbage")
    _age(bad)
    scan_once(database, force_stability=False)  # pending.wav unstable, bad.wav fails
    status = consume_status()
    assert status["pending_files"] == 1  # pending.wav still waiting to become stable
    assert status["failed_files"] == 1
    assert status["last_scan"] is None or status["last_scan"]["failed"] in (0, 1)


def test_maybe_scan_respects_enabled_switch(database, consume_dir, enqueued, monkeypatch):
    _create_user(database, "admin", admin=True)
    monkeypatch.setattr(settings, "consume_enabled", False)
    _write_wav(consume_dir / "disabled.wav")
    assert maybe_scan(force=True) is None
    with SessionLocal() as db:
        assert db.scalars(select(Recording)).all() == []


@needs_ffprobe
def test_maybe_scan_imports_once_then_waits_for_interval(database, consume_dir, enqueued, monkeypatch):
    _create_user(database, "admin", admin=True)
    _write_wav(consume_dir / "worker.wav")
    _age(consume_dir / "worker.wav")

    first = maybe_scan()  # first scan is always due
    assert first is not None and first.imported == 1
    # Same interval: no new scan.
    assert maybe_scan() is None
    # Forced scan ignores the interval guard.
    forced = maybe_scan(force=True)
    assert forced is not None and forced.scanned == 0


@needs_ffprobe
def test_embedded_worker_periodic_scan(database, consume_dir, enqueued):
    """The embedded worker thread auto-imports without any manual trigger."""
    _create_user(database, "admin", admin=True)
    _write_wav(consume_dir / "thread.wav")
    _age(consume_dir / "thread.wav")

    from app.worker import start_embedded_worker, stop_embedded_worker, worker_status

    start_embedded_worker()
    try:
        deadline = time.time() + 10
        recording_id = None
        while time.time() < deadline:
            with SessionLocal() as db:
                recording = db.scalars(select(Recording)).first()
                if recording is not None:
                    recording_id = int(recording.id)
                    break
            time.sleep(0.2)
        assert recording_id is not None, "embedded worker did not import the consume file"
        status = worker_status()
        assert status["running"] is True
        assert status["last_consume_scan"] is not None
        assert status["last_consume_scan"]["imported"] >= 1
    finally:
        stop_embedded_worker()
    with SessionLocal() as db:
        recording = db.scalars(select(Recording)).one()
        assert recording.source == "consume"
        assert db.scalars(select(ImportedFile)).all()
    assert len(enqueued) == 1
