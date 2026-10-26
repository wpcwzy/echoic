"""Merge a finished live-transcription session into its bound recording.

Contract for the pipeline hook (Lead integrates in ``services/pipeline.py`` after
the transcribe/analyze stages, and/or in the ASR-failure path)::

    from app.services.live_merge import merge_live_session_into_recording
    outcome = merge_live_session_into_recording(recording_id)   # -> "replace" | "promote" | None

Semantics (docs/09 §2, docs/10 §4.1):

``replace`` (default when the offline transcript exists)
    Keep the offline ASR transcript (with speakers) as the final transcript; the
    live transcript stays in ``live_sessions``/``live_segments`` for reference
    (``GET /api/recordings/{id}/live``). The session is marked ``merged``.

``promote`` (used when ``merge_mode="promote"`` **or** the offline ASR produced
no usable transcript, e.g. offline ASR failed/unsupported)
    Replace the recording transcript with the live ``final`` segments using a
    single speaker ``说话人1``, set ``recording.asr_provider = "live:<provider>"``
    and mark the session ``merged``. The caller should then enqueue analysis
    (``queue.enqueue(recording_id, "insights")`` or a full reprocess) because the
    previous analyses/chunks refer to the old transcript — this helper does not
    regenerate them itself.

The helper is idempotent (a session already ``merged`` returns ``None``), safe
when no session/segments exist (returns ``None``) and never raises into the
pipeline: unexpected errors are logged and reported as ``None``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.utils import utcnow
from ..db import SessionLocal
from ..models import LiveSegment, LiveSession, Recording, Speaker, TranscriptSegment

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]

LIVE_SPEAKER_LABEL = "说话人1"
LIVE_SPEAKER_COLOR = "#5B8FF9"
MERGED_STATUS = "merged"
SKIPPED_SESSION_STATUSES = {"cancelled"}


def find_live_session_for_recording(db: Session, recording_id: int) -> LiveSession | None:
    """Most recent live session bound to a recording (backend-api also uses this)."""
    return db.scalar(
        select(LiveSession)
        .where(LiveSession.recording_id == recording_id)
        .order_by(LiveSession.id.desc())
        .limit(1)
    )


def _final_segments(db: Session, session_id: int) -> list[LiveSegment]:
    return list(
        db.scalars(
            select(LiveSegment)
            .where(LiveSegment.session_id == session_id, LiveSegment.is_final.is_(True))
            .order_by(LiveSegment.idx, LiveSegment.id)
        ).all()
    )


def _offline_segment_count(db: Session, recording_id: int) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(TranscriptSegment).where(
                TranscriptSegment.recording_id == recording_id
            )
        )
        or 0
    )


def _promote_live_transcript(
    db: Session,
    recording: Recording,
    session: LiveSession,
    segments: list[LiveSegment],
) -> None:
    """Replace the recording transcript with the live final segments."""
    # Remove the old transcript + speakers (segments reference speakers).
    for segment in db.scalars(
        select(TranscriptSegment).where(TranscriptSegment.recording_id == recording.id)
    ).all():
        db.delete(segment)
    db.flush()
    for speaker in db.scalars(
        select(Speaker).where(Speaker.recording_id == recording.id)
    ).all():
        db.delete(speaker)
    db.flush()

    speaker = Speaker(
        recording_id=recording.id,
        label=LIVE_SPEAKER_LABEL,
        display_name=LIVE_SPEAKER_LABEL,
        color=LIVE_SPEAKER_COLOR,
        is_auto=True,
    )
    db.add(speaker)
    db.flush()

    last_end_ms = 0
    for idx, live_segment in enumerate(segments):
        text = (live_segment.text or "").strip()
        if not text:
            continue
        start = max(0, int(live_segment.start_ms or 0))
        end = max(start + 100, int(live_segment.end_ms or 0))
        last_end_ms = max(last_end_ms, end)
        db.add(
            TranscriptSegment(
                recording_id=recording.id,
                idx=idx,
                speaker_id=speaker.id,
                start_ms=start,
                end_ms=end,
                text=text,
                confidence=None,
            )
        )
    recording.language = session.language or recording.language or "zh"
    recording.asr_provider = f"live:{session.provider or 'mock'}"[:32]
    if last_end_ms > int(recording.duration_ms or 0):
        recording.duration_ms = last_end_ms
    if int(recording.duration_ms or 0) <= 0 and session.total_audio_ms:
        recording.duration_ms = int(session.total_audio_ms)
    recording.error_message = None


def merge_live_session_into_recording(
    recording_id: int,
    db_session_factory: SessionFactory | None = None,
) -> str | None:
    """Merge the bound live session into a recording.

    Returns ``"replace"`` / ``"promote"`` when a merge happened, ``None`` when
    there is nothing to merge (no session/segments, session cancelled or already
    merged) or on unexpected errors — never raises into the pipeline.
    """
    factory: SessionFactory = db_session_factory or SessionLocal
    try:
        with factory() as db:
            recording = db.get(Recording, int(recording_id))
            if recording is None:
                return None
            session = find_live_session_for_recording(db, recording.id)
            if session is None:
                return None
            if session.status in SKIPPED_SESSION_STATUSES:
                return None
            if session.status == MERGED_STATUS:
                return None
            segments = _final_segments(db, session.id)
            if not segments:
                logger.info(
                    "live merge: session %s has no final segments, nothing to merge", session.id
                )
                return None

            offline_count = _offline_segment_count(db, recording.id)
            promote = (
                (session.merge_mode or "replace") == "promote"
                or offline_count == 0
                or recording.status == "failed"
            )
            now = utcnow()
            if promote:
                _promote_live_transcript(db, recording, session, segments)
                outcome = "promote"
            else:
                outcome = "replace"
            session.status = MERGED_STATUS
            if session.ended_at is None:
                session.ended_at = now
            db.commit()
            logger.info(
                "live merge: recording %s <- session %s (%s, %s final segments, offline segments=%s)",
                recording.id,
                session.id,
                outcome,
                len(segments),
                offline_count,
            )
            return outcome
    except Exception:  # noqa: BLE001 - merge must never break the pipeline
        logger.exception("live merge failed for recording %s", recording_id)
        return None


__all__ = [
    "LIVE_SPEAKER_LABEL",
    "find_live_session_for_recording",
    "merge_live_session_into_recording",
]
