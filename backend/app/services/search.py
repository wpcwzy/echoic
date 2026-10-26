"""Global search service (LIKE based, no external search engine).

Owned by backend-api. The router in ``app/api/search.py`` turns the returned
rows into API DTOs; keeping the query logic here makes it testable on its own.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import Recording, Speaker, TranscriptSegment


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _pattern(query: str) -> str:
    return f"%{escape_like(query)}%"


@dataclass
class RecordingHit:
    recording: Recording
    hit_count: int = 0


@dataclass
class SegmentHit:
    recording_id: int
    recording_title: str
    segment_id: int
    speaker: str = ""
    start_ms: int = 0
    snippet: str = ""


@dataclass
class SearchResult:
    query: str
    recordings: list[RecordingHit] = field(default_factory=list)
    segments: list[SegmentHit] = field(default_factory=list)


def make_snippet(text: str, query: str, radius: int = 26) -> str:
    """Return a short context window around the first match (with ellipses)."""
    text = text or ""
    needle = (query or "").strip()
    if not needle:
        return text[: radius * 2] + ("…" if len(text) > radius * 2 else "")
    index = text.lower().find(needle.lower())
    if index < 0:
        head = text[: radius * 2].strip()
        return head + ("…" if len(text) > radius * 2 else "")
    start = max(0, index - radius)
    end = min(len(text), index + len(needle) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"


def search(db: Session, query: str, limit: int = 20) -> SearchResult:
    result = SearchResult(query=query or "")
    needle = (query or "").strip()
    if not needle:
        return result
    pattern = _pattern(needle)

    # recordings whose title/description match
    title_matches = list(
        db.scalars(
            select(Recording).where(
                or_(
                    Recording.title.ilike(pattern, escape="\\"),
                    Recording.description.ilike(pattern, escape="\\"),
                )
            )
        ).all()
    )

    # transcript segment matches (with recording + speaker context)
    segment_rows = db.execute(
        select(TranscriptSegment, Recording, Speaker)
        .join(Recording, Recording.id == TranscriptSegment.recording_id)
        .outerjoin(Speaker, Speaker.id == TranscriptSegment.speaker_id)
        .where(TranscriptSegment.text.ilike(pattern, escape="\\"))
        .order_by(TranscriptSegment.recording_id.asc(), TranscriptSegment.start_ms.asc())
    ).all()

    counts: Counter[int] = Counter()
    segment_hits: list[SegmentHit] = []
    recordings_by_id: dict[int, Recording] = {recording.id: recording for recording in title_matches}
    for segment, recording, speaker in segment_rows:
        counts[recording.id] += 1
        recordings_by_id.setdefault(recording.id, recording)
        if len(segment_hits) < limit:
            segment_hits.append(
                SegmentHit(
                    recording_id=recording.id,
                    recording_title=recording.title,
                    segment_id=segment.id,
                    speaker=(speaker.display_name or speaker.label) if speaker is not None else "",
                    start_ms=int(segment.start_ms or 0),
                    snippet=make_snippet(segment.text, needle),
                )
            )

    ordered = sorted(
        recordings_by_id.values(),
        key=lambda recording: (recording.recorded_at, recording.id),
        reverse=True,
    )[:limit]
    result.recordings = [
        RecordingHit(recording=recording, hit_count=counts.get(recording.id, 0) or 1)
        for recording in ordered
    ]
    result.segments = segment_hits[:limit]
    return result


__all__ = ["SearchResult", "RecordingHit", "SegmentHit", "make_snippet", "search"]
