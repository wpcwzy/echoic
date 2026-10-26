"""Processing pipeline orchestration (docs/PIPELINE.md §1).

Stages: prepare → transcribe → analyze → embed → finalize, with per-stage
progress mapped onto the recording/job rows, cancellation checks between stages
and Chinese, user friendly failure messages.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import wave
from pathlib import Path
from typing import Any, Callable

import numpy as np
from sqlalchemy import select

from ..config import settings
from ..core import media, storage
from ..core.errors import ProviderError
from ..core.utils import format_timestamp, utcnow
from ..db import SessionLocal
from ..models import (
    Analysis,
    Chunk,
    Job,
    JobStatus,
    Recording,
    RecordingStatus,
    Speaker,
    Stage,
    TranscriptSegment,
)
from ..providers.base import ASRResult
from ..providers.registry import build_asr_provider, build_embedding_provider
from .analysis import (
    AnalysisCancelled,
    AnalysisError,
    build_chunk_payloads,
    delete_analyses,
    generate_analyses,
)
from .settings_store import load_ai_config

logger = logging.getLogger(__name__)

PROCESS_TIMEOUT_S = 30 * 60
STAGE_RANGES: dict[str, tuple[int, int]] = {
    Stage.PREPARING: (0, 8),
    Stage.TRANSCRIBING: (8, 55),
    Stage.ANALYZING: (55, 88),
    Stage.EMBEDDING: (88, 96),
    Stage.FINALIZING: (96, 100),
}
SPEAKER_COLORS = ["#5B8FF9", "#61DDAA", "#65789B", "#F6BD16", "#7262FD", "#78D3F8", "#9661BC", "#F6903D"]


# ------------------------------------------------------------------ DB helpers
def _is_cancelled(recording_id: int) -> bool:
    with SessionLocal() as db:
        status = db.scalar(select(Recording.status).where(Recording.id == recording_id))
        return status == RecordingStatus.CANCELLED


def _set_stage(recording_id: int, job_id: int, stage: str, progress: int) -> None:
    try:
        with SessionLocal() as db:
            recording = db.get(Recording, recording_id)
            if recording is not None and recording.status != RecordingStatus.CANCELLED:
                recording.stage = stage
                recording.progress = max(0, min(100, progress))
            job = db.get(Job, job_id)
            if job is not None and job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                job.stage = stage
                job.progress = max(0, min(100, progress))
            db.commit()
    except Exception:  # pragma: no cover - progress updates must never kill the job
        logger.warning("failed to update progress for recording %s", recording_id, exc_info=True)


class _Progress:
    """Maps a stage-local 0-100 progress onto the global 0-100 scale."""

    def __init__(self, recording_id: int, job_id: int, stage: str) -> None:
        self.recording_id = recording_id
        self.job_id = job_id
        self.stage = stage
        low, high = STAGE_RANGES.get(stage, (0, 100))
        self.low = low
        self.high = high
        self._last = -1
        self._last_ts = 0.0

    def __call__(self, local_percent: int) -> None:
        local = max(0, min(100, int(local_percent or 0)))
        global_percent = self.low + int((self.high - self.low) * local / 100)
        now = time.monotonic()
        if global_percent <= self._last:
            return
        if now - self._last_ts < 0.5 and global_percent - self._last < 2:
            return
        self._last = global_percent
        self._last_ts = now
        _set_stage(self.recording_id, self.job_id, self.stage, global_percent)


def _claim_or_create_job(recording_id: int, from_: str) -> tuple[int, str] | None:
    """Return (job_id, from_stage). Creates/claims a job when invoked directly."""
    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        if recording is None:
            logger.warning("process_recording skipped: recording %s not found", recording_id)
            return None
        if recording.status == RecordingStatus.CANCELLED:
            for job in db.scalars(
                select(Job).where(
                    Job.recording_id == recording_id,
                    Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                )
            ).all():
                job.status = JobStatus.CANCELLED
                job.finished_at = utcnow()
            db.commit()
            logger.info("process_recording skipped: recording %s is cancelled", recording_id)
            return None
        job = db.scalars(
            select(Job)
            .where(Job.recording_id == recording_id, Job.status == JobStatus.RUNNING)
            .order_by(Job.id.desc())
        ).first()
        if job is not None:
            from_stage = job.from_stage or from_ or "all"
        else:
            queued = db.scalars(
                select(Job)
                .where(Job.recording_id == recording_id, Job.status == JobStatus.QUEUED)
                .order_by(Job.id)
            ).first()
            if queued is not None:
                job = queued
                job.status = JobStatus.RUNNING
                job.attempts = int(job.attempts or 0) + 1
                job.started_at = utcnow()
                from_stage = job.from_stage or from_ or "all"
            else:
                job = Job(
                    recording_id=recording_id,
                    type="process",
                    status=JobStatus.RUNNING,
                    stage=Stage.PREPARING,
                    from_stage=from_ or "all",
                    attempts=1,
                    started_at=utcnow(),
                )
                db.add(job)
                db.flush()
                from_stage = from_ or "all"
        recording.status = RecordingStatus.PROCESSING
        recording.stage = Stage.PREPARING
        recording.progress = 0
        recording.error_message = None
        db.commit()
        return int(job.id), from_stage


def _mark_failed(recording_id: int, job_id: int, message: str) -> None:
    message = (message or "处理失败").strip()[:1000]
    try:
        with SessionLocal() as db:
            recording = db.get(Recording, recording_id)
            if recording is not None and recording.status != RecordingStatus.CANCELLED:
                recording.status = RecordingStatus.FAILED
                recording.stage = Stage.FAILED
                recording.error_message = message
            job = db.get(Job, job_id)
            if job is not None and job.status != JobStatus.CANCELLED:
                job.status = JobStatus.FAILED
                job.stage = Stage.FAILED
                job.error = message
                job.finished_at = utcnow()
            db.commit()
    except Exception:  # pragma: no cover
        logger.exception("failed to persist failure state for recording %s", recording_id)


def _mark_cancelled(recording_id: int, job_id: int) -> None:
    try:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            if job is not None and job.status not in (JobStatus.DONE, JobStatus.FAILED):
                job.status = JobStatus.CANCELLED
                job.finished_at = utcnow()
            recording = db.get(Recording, recording_id)
            if recording is not None and recording.status != RecordingStatus.CANCELLED:
                recording.status = RecordingStatus.CANCELLED
            db.commit()
    except Exception:  # pragma: no cover
        logger.exception("failed to persist cancel state for recording %s", recording_id)


def _delete_derived(recording_id: int, *, include_transcript: bool) -> None:
    with SessionLocal() as db:
        for chunk in db.scalars(select(Chunk).where(Chunk.recording_id == recording_id)).all():
            db.delete(chunk)
        for analysis in db.scalars(select(Analysis).where(Analysis.recording_id == recording_id)).all():
            db.delete(analysis)
        if include_transcript:
            for segment in db.scalars(
                select(TranscriptSegment).where(TranscriptSegment.recording_id == recording_id)
            ).all():
                db.delete(segment)
            db.flush()
            for speaker in db.scalars(select(Speaker).where(Speaker.recording_id == recording_id)).all():
                db.delete(speaker)
        db.commit()


# -------------------------------------------------------------------- prepare
def _recording_dir(recording: Recording) -> Path:
    try:
        return storage.resolve_storage_dir(recording.storage_dir)
    except storage.StorageError as exc:
        raise media.MediaError(f"录音存储路径无效：{exc}") from exc


def _find_source(original_filename: str, original_ext: str, directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    ext = Path(original_filename or "").suffix.lower() or (original_ext or "")
    if ext:
        candidate = directory / f"original{ext if ext.startswith('.') else '.' + ext}"
        if candidate.is_file():
            return candidate
    candidates = [path for path in sorted(directory.glob("original.*")) if path.is_file()]
    return candidates[0] if candidates else None


def _wav_duration_ms(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as wav:
            frames, rate = wav.getnframes(), wav.getframerate() or 16000
            return int(frames / rate * 1000) if frames else 0
    except (wave.Error, OSError, EOFError):
        return 0


def _prepare(recording_id: int, progress: Callable[[int], None], check_cancel: Callable[[], bool]) -> None:
    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        if recording is None:
            raise media.MediaError("录音不存在")
        cfg = load_ai_config(db)
        asr_provider_name = cfg.asr.provider
        storage_dir = recording.storage_dir
        source_filename = recording.original_filename
        source_ext = recording.original_ext
        known_duration = int(recording.duration_ms or 0)
        language = recording.language or cfg.asr.language
        directory = _recording_dir(recording)

    try:
        from ..core.storage_backend import (
            fetch_to_local as _fetch_to_local,
            get_storage as _get_storage,
            playback_key as _playback_storage_key,
            recording_original_key as _recording_original_key,
        )

        _backend = _get_storage()
        is_remote = getattr(_backend, "name", "local") == "s3"
    except Exception:  # noqa: BLE001 - storage import/config problems degrade to local handling
        _backend = None
        is_remote = False

    if is_remote:
        stage_dir = settings.data_path / "tmp" / f"rec-{recording_id}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        source = None
        try:
            source_key = _recording_original_key(_backend, storage_dir, source_ext)
            if source_key:
                source = _fetch_to_local(
                    _backend, source_key, stage_dir / f"original{source_ext or '.bin'}"
                )
        except Exception as exc:  # noqa: BLE001 - convert to a friendly media error
            raise media.MediaError(f"无法从对象存储读取原始媒体：{exc}") from exc
        asr_wav = stage_dir / "asr.wav"
        playback = stage_dir / "playback.m4a"
        playback_storage_key = _playback_storage_key(storage_dir)
    else:
        source = _find_source(source_filename, source_ext, directory)
        asr_wav = storage.asr_wav_path(storage_dir)
        playback = storage.playback_path(storage_dir)
        playback_storage_key = None
    ffmpeg_ready = media.ffmpeg_available(settings.ffmpeg) and media.ffprobe_available(settings.ffprobe)
    duration_ms = known_duration
    media_info: media.MediaInfo | None = None

    if source is not None and ffmpeg_ready:
        progress(5)
        media_info = media.probe(source, settings.ffprobe)
        if media_info.duration_ms:
            duration_ms = media_info.duration_ms
        progress(20)
        if check_cancel():
            raise AnalysisCancelled()
        media.make_playback(source, playback, settings.ffmpeg)
        progress(55)
        if check_cancel():
            raise AnalysisCancelled()
        media.extract_asr_wav(source, asr_wav, settings.ffmpeg)
        if is_remote and _backend is not None and playback_storage_key:
            try:
                _backend.save_file(playback, playback_storage_key, content_type="audio/mp4")
            except Exception as exc:  # noqa: BLE001 - friendly failure
                raise media.MediaError(f"无法将播放文件写入对象存储：{exc}") from exc
        progress(90)
    elif source is not None and not ffmpeg_ready:
        duration_ms = duration_ms or media.media_duration_ms(source, settings.ffprobe)
        if asr_provider_name != "mock":
            raise media.MediaError(
                "服务器未安装 ffmpeg/ffprobe，无法处理音视频；请安装 ffmpeg 后重试"
                f"（当前 ASR provider：{asr_provider_name}）"
            )
        logger.warning("ffmpeg missing: mock pipeline continues without transcoding (recording %s)", recording_id)
    elif asr_wav.exists():
        duration_ms = duration_ms or _wav_duration_ms(asr_wav)
        logger.info("prepare: reusing existing asr.wav for recording %s", recording_id)
    elif asr_provider_name == "mock":
        duration_ms = duration_ms or 10 * 60 * 1000
        logger.warning("prepare: no source media for recording %s, mock provider continues", recording_id)
    else:
        raise media.MediaError("找不到录音的原始媒体文件，无法处理")

    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        if recording is None:
            raise media.MediaError("录音不存在")
        recording.duration_ms = max(0, int(duration_ms or 0))
        if media_info is not None:
            recording.media_type = media_info.media_type
            if media_info.mime_type:
                recording.mime_type = media_info.mime_type
        recording.language = language or recording.language or "zh"
        db.commit()
    progress(100)


# ----------------------------------------------------------------- transcribe
def _write_transcript(recording_id: int, result: ASRResult) -> None:
    raw_segments = [segment for segment in result.segments if (segment.text or "").strip()]
    if not raw_segments:
        raise ProviderError("未识别到有效语音内容，请检查音频文件或转写服务配置", provider=result.provider)
    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        if recording is None:
            raise ProviderError("录音不存在")
        duration_ms = int(recording.duration_ms or 0)
        for segment in db.scalars(
            select(TranscriptSegment).where(TranscriptSegment.recording_id == recording_id)
        ).all():
            db.delete(segment)
        db.flush()
        for speaker in db.scalars(select(Speaker).where(Speaker.recording_id == recording_id)).all():
            db.delete(speaker)
        db.flush()

        labels: list[str] = []
        for speaker_name in result.speakers:
            name = (speaker_name or "").strip()
            if name and name not in labels:
                labels.append(name)
        for segment in raw_segments:
            label = (segment.speaker_label or "").strip()
            if label and label not in labels:
                labels.append(label)
        if not labels:
            labels = ["说话人1"]
        speaker_rows: dict[str, Speaker] = {}
        for index, label in enumerate(labels):
            row = Speaker(
                recording_id=recording_id,
                label=label,
                display_name=label,
                color=SPEAKER_COLORS[index % len(SPEAKER_COLORS)],
                is_auto=True,
            )
            db.add(row)
            speaker_rows[label] = row
        db.flush()
        for index, segment in enumerate(raw_segments):
            label = (segment.speaker_label or "").strip() or labels[0]
            speaker_row = speaker_rows.get(label) or list(speaker_rows.values())[0]
            start = max(0, int(segment.start_ms or 0))
            end = max(start + 100, int(segment.end_ms or 0))
            if duration_ms:
                start = min(start, duration_ms)
                end = min(end, duration_ms)
                if end <= start:
                    end = min(duration_ms, start + 100)
            db.add(
                TranscriptSegment(
                    recording_id=recording_id,
                    idx=index,
                    speaker_id=speaker_row.id,
                    start_ms=start,
                    end_ms=end,
                    text=(segment.text or "").strip(),
                    confidence=segment.confidence,
                )
            )
        recording.language = result.language or recording.language
        recording.asr_provider = result.provider
        db.commit()
    logger.info(
        "transcribed recording %s: %s segments, %s speakers (%s)",
        recording_id,
        len(raw_segments),
        len(labels),
        result.provider,
    )


async def _transcribe(
    recording_id: int,
    job_id: int,
    progress: Callable[[int], None],
    check_cancel: Callable[[], bool],
) -> None:
    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        if recording is None:
            raise ProviderError("录音不存在")
        cfg = load_ai_config(db)
        storage_dir = recording.storage_dir
        language = recording.language or cfg.asr.language
    try:
        from ..core.storage_backend import get_storage as _get_storage

        if getattr(_get_storage(), "name", "local") == "s3":
            asr_wav = settings.data_path / "tmp" / f"rec-{recording_id}" / "asr.wav"
        else:
            asr_wav = storage.asr_wav_path(storage_dir)
    except Exception:  # noqa: BLE001 - fall back to local path
        asr_wav = storage.asr_wav_path(storage_dir)
    provider = build_asr_provider(cfg.asr)
    logger.info(
        "transcribing recording %s with %s (%s), audio=%s",
        recording_id,
        cfg.asr.provider,
        cfg.asr.model,
        asr_wav.name,
    )
    progress(0)
    result = await provider.transcribe(
        asr_wav,
        language=language or "zh",
        diarization=bool(cfg.asr.diarization),
        progress=progress,
    )
    if check_cancel():
        raise AnalysisCancelled()
    progress(100)
    _write_transcript(recording_id, result)


# -------------------------------------------------------------------- analyze
async def _analyze(
    recording_id: int,
    job_id: int,
    progress: Callable[[int], None],
    check_cancel: Callable[[], bool],
) -> None:
    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        if recording is None:
            raise AnalysisError("录音不存在")
        cfg = load_ai_config(db)
        delete_analyses(db, recording_id)
        recording = db.get(Recording, recording_id)
        await generate_analyses(
            db,
            recording,
            cfg,
            progress=progress,
            check_cancel=check_cancel,
        )
        recording.llm_provider = cfg.llm.provider or None
        db.commit()
    logger.info("analyses generated for recording %s", recording_id)


# ---------------------------------------------------------------------- embed
_build_chunk_payloads = build_chunk_payloads


async def _embed(
    recording_id: int,
    job_id: int,
    progress: Callable[[int], None],
    check_cancel: Callable[[], bool],
) -> None:
    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        if recording is None:
            raise AnalysisError("录音不存在")
        cfg = load_ai_config(db)
        duration_ms = int(recording.duration_ms or 0)
        speaker_names = {
            speaker.id: (speaker.display_name or speaker.label)
            for speaker in db.scalars(select(Speaker).where(Speaker.recording_id == recording_id)).all()
        }
        segments = list(
            db.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.recording_id == recording_id)
                .order_by(TranscriptSegment.idx)
            ).all()
        )
        segment_rows = [
            {
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
                "speaker": speaker_names.get(segment.speaker_id) or "说话人",
                "text": (segment.text or "").strip(),
            }
            for segment in segments
            if (segment.text or "").strip()
        ]
    payloads = _build_chunk_payloads(segment_rows, duration_ms)
    vectors: list[list[float]] | None = None
    if payloads and (cfg.embeddings.provider or "none") != "none":
        try:
            provider = build_embedding_provider(cfg.embeddings)
            if provider is not None:
                collected: list[list[float]] = []
                batch = 8
                for offset in range(0, len(payloads), batch):
                    if check_cancel():
                        raise AnalysisCancelled()
                    part = await provider.embed([item["text"] for item in payloads[offset : offset + batch]])
                    collected.extend(part)
                    progress(min(90, int((offset + len(part)) * 90 / max(1, len(payloads)))))
                if len(collected) != len(payloads):
                    raise ProviderError(
                        f"向量数量不匹配（期望 {len(payloads)}，实际 {len(collected)}）",
                        provider=cfg.embeddings.provider,
                    )
                vectors = collected
                logger.info("embedded %s chunks for recording %s", len(vectors), recording_id)
        except AnalysisCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - embedding failures must not fail the recording
            logger.warning(
                "embedding failed for recording %s, keeping chunks without vectors: %s", recording_id, exc
            )
            vectors = None
    with SessionLocal() as db:
        for chunk in db.scalars(select(Chunk).where(Chunk.recording_id == recording_id)).all():
            db.delete(chunk)
        db.flush()
        for index, payload in enumerate(payloads):
            embedding_bytes = None
            dim = None
            if vectors is not None and index < len(vectors):
                array = np.asarray(vectors[index], dtype=np.float32)
                if array.size:
                    embedding_bytes = array.tobytes()
                    dim = int(array.size)
            db.add(
                Chunk(
                    recording_id=recording_id,
                    idx=index,
                    start_ms=int(payload["start_ms"]),
                    end_ms=int(payload["end_ms"]),
                    text=payload["text"],
                    embedding=embedding_bytes,
                    dim=dim,
                    embedding_model=(cfg.embeddings.model or None) if embedding_bytes is not None else None,
                )
            )
        db.commit()
        try:
            from .vector_store import vector_index_chunks

            vector_index_chunks(
                db,
                recording_id,
                model=(cfg.embeddings.model or None) if vectors is not None else None,
            )
        except Exception:  # noqa: BLE001 - vector index sync must never fail processing
            logger.warning("vector index sync failed for recording %s", recording_id, exc_info=True)
    progress(100)
    logger.info(
        "built %s chunks for recording %s (vectors=%s)",
        len(payloads),
        recording_id,
        "yes" if vectors is not None else "no",
    )


# ------------------------------------------------------------------- finalize
def _finalize(recording_id: int, job_id: int) -> None:
    with SessionLocal() as db:
        recording = db.get(Recording, recording_id)
        if recording is None:
            return
        if recording.status == RecordingStatus.CANCELLED:
            job = db.get(Job, job_id)
            if job is not None:
                job.status = JobStatus.CANCELLED
                job.finished_at = utcnow()
            db.commit()
            return
        try:
            from ..core.storage_backend import get_storage as _get_storage

            if getattr(_get_storage(), "name", "local") == "s3":
                shutil.rmtree(
                    settings.data_path / "tmp" / f"rec-{recording_id}", ignore_errors=True
                )
            else:
                asr_wav = storage.asr_wav_path(recording.storage_dir)
                if asr_wav.exists():
                    asr_wav.unlink()
        except Exception:  # noqa: BLE001 - cleanup must never fail finalization
            logger.warning("failed to clean pipeline staging for recording %s", recording_id)
        now = utcnow()
        recording.status = RecordingStatus.READY
        recording.stage = Stage.DONE
        recording.progress = 100
        recording.error_message = None
        recording.processed_at = now
        job = db.get(Job, job_id)
        if job is not None:
            job.status = JobStatus.DONE
            job.stage = Stage.DONE
            job.progress = 100
            job.error = None
            job.finished_at = now
        db.commit()


# ----------------------------------------------------------------------- run
def _friendly_error(exc: BaseException) -> str:
    if isinstance(exc, ProviderError):
        return exc.message
    if isinstance(exc, (media.MediaError, AnalysisError, storage.StorageError)):
        return str(exc)
    return f"处理失败（{type(exc).__name__}）：{str(exc)[:300]}"


async def _run(recording_id: int, from_: str) -> None:
    claim = _claim_or_create_job(recording_id, from_)
    if claim is None:
        return
    job_id, from_stage = claim
    logger.info("processing recording %s (job=%s, from=%s)", recording_id, job_id, from_stage)

    def check_cancel() -> bool:
        return _is_cancelled(recording_id)

    try:
        if from_stage in ("all", "asr"):
            _delete_derived(recording_id, include_transcript=True)
            if check_cancel():
                _mark_cancelled(recording_id, job_id)
                return
            progress = _Progress(recording_id, job_id, Stage.PREPARING)
            _set_stage(recording_id, job_id, Stage.PREPARING, 0)
            _prepare(recording_id, progress, check_cancel)
            if check_cancel():
                _mark_cancelled(recording_id, job_id)
                return
            progress = _Progress(recording_id, job_id, Stage.TRANSCRIBING)
            _set_stage(recording_id, job_id, Stage.TRANSCRIBING, STAGE_RANGES[Stage.TRANSCRIBING][0])
            transcribe_ok = True
            try:
                await _transcribe(recording_id, job_id, progress, check_cancel)
            except Exception:  # noqa: BLE001 - a bound live transcript may rescue the recording
                transcribe_ok = False
                try:
                    from .live_merge import merge_live_session_into_recording

                    merged = merge_live_session_into_recording(recording_id)
                except Exception:  # noqa: BLE001 - fall back to the original failure
                    merged = None
                if merged != "promote":
                    raise
                logger.info(
                    "recording %s: offline ASR failed, promoted bound live transcript",
                    recording_id,
                )
            if transcribe_ok:
                try:
                    from .live_merge import merge_live_session_into_recording

                    merge_live_session_into_recording(recording_id)
                except Exception:  # noqa: BLE001 - merge is best-effort
                    logger.warning("live merge failed for recording %s", recording_id, exc_info=True)
            if check_cancel():
                _mark_cancelled(recording_id, job_id)
                return

        if from_stage != "embeddings":
            progress = _Progress(recording_id, job_id, Stage.ANALYZING)
            _set_stage(recording_id, job_id, Stage.ANALYZING, STAGE_RANGES[Stage.ANALYZING][0])
            await _analyze(recording_id, job_id, progress, check_cancel)
            if check_cancel():
                _mark_cancelled(recording_id, job_id)
                return

        progress = _Progress(recording_id, job_id, Stage.EMBEDDING)
        _set_stage(recording_id, job_id, Stage.EMBEDDING, STAGE_RANGES[Stage.EMBEDDING][0])
        await _embed(recording_id, job_id, progress, check_cancel)
        if check_cancel():
            _mark_cancelled(recording_id, job_id)
            return

        _set_stage(recording_id, job_id, Stage.FINALIZING, STAGE_RANGES[Stage.FINALIZING][0])
        _finalize(recording_id, job_id)
        logger.info("recording %s processed successfully", recording_id)
    except AnalysisCancelled:
        logger.info("recording %s cancelled during processing", recording_id)
        _mark_cancelled(recording_id, job_id)
    except Exception as exc:  # noqa: BLE001 - convert to a friendly failure row
        message = _friendly_error(exc)
        if isinstance(exc, (ProviderError, media.MediaError, AnalysisError)):
            logger.warning("processing recording %s failed: %s", recording_id, message)
        else:
            logger.exception("processing recording %s failed: %s", recording_id, exc)
        _mark_failed(recording_id, job_id, message)


async def process_recording(recording_id: int, from_: str = "all") -> None:
    """Process one recording end-to-end. Safe to call without the embedded worker."""
    try:
        await asyncio.wait_for(_run(recording_id, from_), timeout=PROCESS_TIMEOUT_S)
    except asyncio.TimeoutError:
        message = "处理超时（超过 30 分钟），请检查音视频大小、网络或服务商配置后重试"
        logger.error("processing recording %s timed out", recording_id)
        with SessionLocal() as db:
            job = db.scalars(
                select(Job)
                .where(Job.recording_id == recording_id, Job.status == JobStatus.RUNNING)
                .order_by(Job.id.desc())
            ).first()
            job_id = int(job.id) if job is not None else 0
        _mark_failed(recording_id, job_id, message)
    finally:
        try:
            from ..core.storage_backend import get_storage as _get_storage

            if getattr(_get_storage(), "name", "local") == "s3":
                shutil.rmtree(
                    settings.data_path / "tmp" / f"rec-{recording_id}", ignore_errors=True
                )
        except Exception:  # noqa: BLE001 - best effort staging cleanup
            logger.debug("staging cleanup failed for recording %s", recording_id, exc_info=True)


__all__ = ["process_recording"]
