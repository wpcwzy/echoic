"""Global search endpoint (docs/API.md §5)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..schemas import SearchOut, SearchRecordingHit, SearchSegmentHit
from ..services import search as search_service
from ._common import recordings_to_summaries

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user)])


@router.get("", response_model=SearchOut)
def global_search(
    q: str = Query("", description="搜索关键词"),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    result = search_service.search(db, q, limit=limit)
    summaries = recordings_to_summaries(
        db, [hit.recording for hit in result.recordings]
    )
    recording_hits = [
        SearchRecordingHit(**summary.model_dump(), hit_count=hit.hit_count)
        for summary, hit in zip(summaries, result.recordings)
    ]
    segment_hits = [SearchSegmentHit(**hit.__dict__) for hit in result.segments]
    return SearchOut(query=result.query, recordings=recording_hits, segments=segment_hits)
