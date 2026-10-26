"""AI analysis / insights endpoints (docs/API.md §4)."""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.utils import load_json
from ..db import get_db
from ..deps import get_current_user
from ..models import ANALYSIS_KINDS, Analysis, Recording, RecordingStatus, Stage
from ..schemas import (
    ActionItemOut,
    ChapterOut,
    InsightOut,
    InsightsOut,
    KeywordOut,
    RegenerateInsightsIn,
    SummaryOut,
    ViewpointNodeOut,
    ViewpointTreeOut,
)
from ._common import content_disposition, enqueue_job, get_recording_or_404, title_for_download

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user)])

_CATEGORIES = {"decision", "risk", "insight", "opportunity"}
_NODE_KINDS = {"topic", "point", "task", "risk"}
_STATUSES = {"open", "done"}


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_items(payload: Any, key: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return candidate
    return []


# ------------------------------------------------------------------ payload parsers
def _parse_summary(payload: Any) -> SummaryOut | None:
    if isinstance(payload, dict):
        markdown = payload.get("markdown")
        if markdown is None:
            markdown = payload.get("summary_md")
        if markdown is None:
            markdown = payload.get("summary")
        return SummaryOut(markdown=str(markdown or ""))
    if isinstance(payload, str) and payload.strip():
        return SummaryOut(markdown=payload)
    return None


def _parse_chapters(payload: Any) -> list[ChapterOut] | None:
    chapters: list[ChapterOut] = []
    for index, item in enumerate(_as_items(payload, "chapters")):
        if not isinstance(item, dict):
            continue
        bullets = [
            str(bullet)
            for bullet in (item.get("bullets") or [])
            if isinstance(bullet, (str, int, float))
        ]
        chapters.append(
            ChapterOut(
                id=str(item.get("id") or f"ch{index + 1}"),
                title=str(item.get("title") or f"章节 {index + 1}"),
                start_ms=_to_int(item.get("start_ms")),
                end_ms=_to_int(item.get("end_ms")),
                summary=str(item.get("summary") or ""),
                bullets=bullets,
            )
        )
    return chapters or None


def _parse_node(raw: Any) -> ViewpointNodeOut | None:
    if not isinstance(raw, dict):
        return None
    children = [node for child in (raw.get("children") or []) if (node := _parse_node(child))]
    kind = raw.get("kind") if raw.get("kind") in _NODE_KINDS else "topic"
    speaker = raw.get("speaker")
    return ViewpointNodeOut(
        id=str(raw.get("id") or "node"),
        label=str(raw.get("label") or "主题")[:200],
        detail=str(raw.get("detail") or ""),
        kind=kind,
        time_ms=_to_optional_int(raw.get("time_ms")),
        speaker=str(speaker) if speaker not in (None, "") else None,
        children=children,
    )


def _parse_viewpoint_tree(payload: Any) -> ViewpointTreeOut | None:
    if isinstance(payload, dict):
        root_raw = payload.get("root", payload if "label" in payload else None)
    else:
        root_raw = None
    root = _parse_node(root_raw)
    return ViewpointTreeOut(root=root) if root is not None else None


def _parse_insights(payload: Any) -> list[InsightOut] | None:
    items: list[InsightOut] = []
    for index, item in enumerate(_as_items(payload, "insights")):
        if not isinstance(item, dict):
            continue
        category = item.get("category") if item.get("category") in _CATEGORIES else "insight"
        items.append(
            InsightOut(
                id=str(item.get("id") or f"ins{index + 1}"),
                title=str(item.get("title") or ""),
                content=str(item.get("content") or ""),
                category=category,
            )
        )
    return items or None


def _parse_action_items(payload: Any) -> list[ActionItemOut] | None:
    items: list[ActionItemOut] = []
    for index, item in enumerate(_as_items(payload, "action_items")):
        if not isinstance(item, dict):
            continue
        status = item.get("status") if item.get("status") in _STATUSES else "open"
        items.append(
            ActionItemOut(
                id=str(item.get("id") or f"act{index + 1}"),
                title=str(item.get("title") or ""),
                owner=str(item.get("owner") or ""),
                due=str(item.get("due") or ""),
                status=status,
                detail=str(item.get("detail") or ""),
            )
        )
    return items or None


def _parse_suggested_questions(payload: Any) -> list[str] | None:
    raw = payload if isinstance(payload, list) else _as_items(payload, "questions")
    questions = [str(item).strip() for item in raw if isinstance(item, (str, int, float)) and str(item).strip()]
    return questions or None


def _parse_keywords(payload: Any) -> list[KeywordOut] | None:
    items: list[KeywordOut] = []
    for item in _as_items(payload, "keywords"):
        if isinstance(item, dict) and item.get("name"):
            try:
                weight = float(item.get("weight", 0.5))
            except (TypeError, ValueError):
                weight = 0.5
            items.append(KeywordOut(name=str(item["name"]), weight=weight))
        elif isinstance(item, str) and item.strip():
            items.append(KeywordOut(name=item.strip(), weight=0.5))
    return items or None


_PARSERS: dict[str, Callable[[Any], Any]] = {
    "summary": _parse_summary,
    "chapters": _parse_chapters,
    "viewpoint_tree": _parse_viewpoint_tree,
    "insights": _parse_insights,
    "action_items": _parse_action_items,
    "suggested_questions": _parse_suggested_questions,
    "keywords": _parse_keywords,
}


# ------------------------------------------------------------------ aggregation
def build_insights(db: Session, recording: Recording) -> InsightsOut:
    rows = list(
        db.scalars(select(Analysis).where(Analysis.recording_id == recording.id)).all()
    )
    parsed: dict[str, Any] = {}
    latest: Analysis | None = None
    for row in rows:
        if latest is None or (row.created_at or row.id) > (latest.created_at or latest.id):
            latest = row
        payload = load_json(row.payload_json, None)
        if payload is None:
            continue
        parser = _PARSERS.get(row.kind)
        if parser is None:
            continue
        try:
            value = parser(payload)
        except Exception:  # malformed payload: report the kind as missing
            logger.warning(
                "analysis payload 解析失败 recording=%s kind=%s", recording.id, row.kind, exc_info=True
            )
            value = None
        if value is not None:
            parsed[row.kind] = value

    output = InsightsOut(recording_id=recording.id)
    missing: list[str] = []
    for kind in ANALYSIS_KINDS:
        if kind in parsed:
            setattr(output, kind, parsed[kind])
        else:
            missing.append(kind)
    output.missing = missing
    if latest is not None:
        output.generated_at = latest.created_at
        output.model = latest.model or latest.provider
    return output


# ------------------------------------------------------------------ routes
@router.get("/{recording_id}/insights", response_model=InsightsOut)
def get_insights(recording_id: int, db: Session = Depends(get_db)):
    recording = get_recording_or_404(db, recording_id)
    return build_insights(db, recording)


@router.post("/{recording_id}/insights/regenerate")
def regenerate_insights(
    recording_id: int,
    payload: RegenerateInsightsIn | None = None,
    db: Session = Depends(get_db),
):
    recording = get_recording_or_404(db, recording_id)
    requested = payload.kinds if payload is not None else None
    # Contract note: the queue only knows stage-level reprocessing, so asking for a
    # subset of kinds still recomputes every AI analysis (documented v0.1 limitation).
    logger.info(
        "recording %s insights regenerate requested kinds=%s", recording_id, requested or "all"
    )
    recording.status = RecordingStatus.QUEUED
    recording.stage = Stage.ANALYZING
    recording.progress = 0
    recording.error_message = None
    recording.processed_at = None
    db.commit()
    enqueue_job(db, recording_id, "insights")
    return {"ok": True, "status": RecordingStatus.QUEUED}


@router.get("/{recording_id}/insights/export")
def export_insights(recording_id: int, db: Session = Depends(get_db)):
    recording = get_recording_or_404(db, recording_id)
    insights = build_insights(db, recording)
    lines: list[str] = [f"# {recording.title}", ""]
    if insights.summary is not None:
        lines += [insights.summary.markdown, ""]
    if insights.chapters:
        lines += ["## 章节", ""]
        for chapter in insights.chapters:
            lines.append(f"### {chapter.title}（{chapter.start_ms} ms - {chapter.end_ms} ms）")
            if chapter.summary:
                lines.append(chapter.summary)
            for bullet in chapter.bullets:
                lines.append(f"- {bullet}")
            lines.append("")
    if insights.insights:
        lines += ["## 关键洞察", ""]
        for item in insights.insights:
            lines.append(f"- **{item.title}**（{item.category}）：{item.content}")
        lines.append("")
    if insights.action_items:
        lines += ["## 待办事项", ""]
        for item in insights.action_items:
            lines.append(f"- [ ] {item.title}（负责人：{item.owner or '待定'}；截止：{item.due or '待定'}）")
        lines.append("")
    if insights.keywords:
        lines += ["## 关键词", "", "、".join(keyword.name for keyword in insights.keywords), ""]
    body = "\n".join(lines).rstrip() + "\n"
    filename = title_for_download(recording.title or "insights", "md")
    return Response(
        content=body.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": content_disposition(filename),
            "Content-Length": str(len(body.encode("utf-8"))),
        },
    )
