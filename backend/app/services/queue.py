"""Database backed job queue (docs/PIPELINE.md §2, docs/API.md §8).

Public contract (kept stable for main.py / api routes):

    enqueue(db, recording_id, from_="all") -> None
    request_cancel(db, recording_id) -> bool
    queue_depth(db) -> int
    reset_stale_tasks() -> None

Additional hook used by ``app.worker``:

    claim_next_job() -> tuple[job_id, recording_id, from_stage] | None
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.utils import utcnow
from ..db import SessionLocal
from ..models import Job, JobStatus, Recording, RecordingStatus, Stage

logger = logging.getLogger(__name__)

VALID_FROM_STAGES = {"all", "asr", "insights", "embeddings"}
MAX_AUTO_RESTARTS = 2


def _active_jobs(db: Session, recording_id: int) -> list[Job]:
    return list(
        db.scalars(
            select(Job).where(
                Job.recording_id == recording_id,
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
        ).all()
    )


def enqueue(db: Session, recording_id: int, from_: str = "all") -> None:
    """Queue a processing job for ``recording_id`` and mark the recording queued."""
    recording = db.get(Recording, recording_id)
    if recording is None:
        raise ValueError(f"recording {recording_id} not found")
    from_stage = from_ if from_ in VALID_FROM_STAGES else "all"
    now = utcnow()
    for job in _active_jobs(db, recording_id):
        job.status = JobStatus.CANCELLED
        job.finished_at = now
        job.error = "已被新的处理任务取代"
    db.add(
        Job(
            recording_id=recording_id,
            type="process",
            status=JobStatus.QUEUED,
            stage=Stage.PREPARING,
            progress=0,
            from_stage=from_stage,
            attempts=0,
        )
    )
    recording.status = RecordingStatus.QUEUED
    recording.stage = Stage.PREPARING
    recording.progress = 0
    recording.error_message = None
    db.commit()
    logger.info("enqueued recording %s (from=%s)", recording_id, from_stage)


def request_cancel(db: Session, recording_id: int) -> bool:
    """Cancel a queued/processing recording. Returns True when something changed."""
    recording = db.get(Recording, recording_id)
    if recording is None:
        return False
    active = _active_jobs(db, recording_id)
    if recording.status not in (RecordingStatus.QUEUED, RecordingStatus.PROCESSING) and not active:
        return False
    now = utcnow()
    for job in active:
        job.status = JobStatus.CANCELLED
        job.finished_at = now
        job.error = "用户取消"
    recording.status = RecordingStatus.CANCELLED
    recording.stage = Stage.FAILED if recording.stage == Stage.FAILED else recording.stage
    db.commit()
    logger.info("cancel requested for recording %s", recording_id)
    return True


def queue_depth(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(Job).where(Job.status == JobStatus.QUEUED)) or 0)


def claim_next_job() -> tuple[int, int, str] | None:
    """Atomically claim the oldest queued job. Returns (job_id, recording_id, from_stage)."""
    with SessionLocal() as db:
        for _ in range(20):
            job = db.scalars(
                select(Job).where(Job.status == JobStatus.QUEUED).order_by(Job.id).limit(1)
            ).first()
            if job is None:
                return None
            recording = db.get(Recording, job.recording_id)
            now = utcnow()
            if recording is None or recording.status == RecordingStatus.CANCELLED:
                job.status = JobStatus.CANCELLED
                job.finished_at = now
                job.error = "录音已取消或不存在"
                db.commit()
                continue
            job.status = JobStatus.RUNNING
            job.attempts = int(job.attempts or 0) + 1
            job.started_at = now
            job.progress = 0
            job.stage = Stage.PREPARING
            recording.status = RecordingStatus.PROCESSING
            recording.stage = Stage.PREPARING
            recording.progress = 0
            db.commit()
            return int(job.id), int(job.recording_id), job.from_stage or "all"
    return None


def reset_stale_tasks() -> None:
    """Startup recovery: running → queued (or failed after too many attempts)."""
    now = utcnow()
    failed_recordings: set[int] = set()
    with SessionLocal() as db:
        running_jobs = list(db.scalars(select(Job).where(Job.status == JobStatus.RUNNING)).all())
        for job in running_jobs:
            if int(job.attempts or 0) >= MAX_AUTO_RESTARTS:
                message = "服务重启导致任务中断，自动重试次数已用完，请手动重试"
                job.status = JobStatus.FAILED
                job.error = message
                job.finished_at = now
                recording = db.get(Recording, job.recording_id)
                if recording is not None and recording.status != RecordingStatus.CANCELLED:
                    recording.status = RecordingStatus.FAILED
                    recording.stage = Stage.FAILED
                    recording.error_message = message
                    failed_recordings.add(recording.id)
                logger.warning("job %s exceeded auto restarts, marked failed", job.id)
            else:
                job.status = JobStatus.QUEUED
                job.stage = Stage.PREPARING
                job.progress = 0
                job.started_at = None
                logger.info("requeued stale job %s (attempts=%s)", job.id, job.attempts)
        stale_recordings = list(
            db.scalars(select(Recording).where(Recording.status == RecordingStatus.PROCESSING)).all()
        )
        for recording in stale_recordings:
            if recording.id in failed_recordings:
                continue
            recording.status = RecordingStatus.QUEUED
            recording.stage = Stage.PREPARING
            recording.progress = 0
        db.commit()
    if running_jobs or stale_recordings:
        logger.info(
            "reset stale tasks: %s job(s), %s recording(s)", len(running_jobs), len(stale_recordings)
        )


__all__ = [
    "claim_next_job",
    "enqueue",
    "queue_depth",
    "request_cancel",
    "reset_stale_tasks",
]
