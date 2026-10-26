"""Helpers to load and match the bundled ``app/demo_data.json`` fixtures.

The file is authored by the Lead (TraceOpt paper discussion + a few other
recordings). Both the mock providers and ``services/demo.py`` read it; this
module is the single loader so caching/matching stay consistent.

The loader is deliberately tolerant: the file may be absent in a fresh
checkout (tests then fall back to synthetic data) and optional fields may be
missing.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

from ..config import APP_DIR

logger = logging.getLogger(__name__)

DEMO_DATA_PATH = APP_DIR / "demo_data.json"

_lock = threading.Lock()
_cache_mtime: float | None = None
_cache_data: dict[str, Any] | None = None

_WS_PATTERN = re.compile(r"\s+")
_PUNCT_PATTERN = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()\[\]【】<>《》—\-…·]+")


def demo_data_path() -> Path:
    return DEMO_DATA_PATH


def normalize(text: str) -> str:
    """Lowercase, strip whitespace/punctuation — used for fuzzy fixture matching."""
    return _PUNCT_PATTERN.sub("", (text or "").lower())


def load_demo_data() -> dict[str, Any] | None:
    """Return the parsed demo fixture (reloaded when the file changes) or None."""
    global _cache_mtime, _cache_data
    path = DEMO_DATA_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with _lock:
        if _cache_data is not None and _cache_mtime == mtime:
            return _cache_data
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("demo_data.json 读取失败：%s", exc)
            return None
        if isinstance(raw, list):
            raw = {"recordings": raw}
        if not isinstance(raw, dict):
            logger.warning("demo_data.json 结构异常（应为对象）")
            return None
        raw.setdefault("recordings", [])
        _cache_data = raw
        _cache_mtime = mtime
        return raw


def demo_recordings() -> list[dict[str, Any]]:
    data = load_demo_data() or {}
    recordings = data.get("recordings") or []
    return [r for r in recordings if isinstance(r, dict)]


def find_recording(key: str | None = None, title: str | None = None) -> dict[str, Any] | None:
    """Find a fixture recording by key or (fuzzy) title."""
    recordings = demo_recordings()
    if key:
        for rec in recordings:
            if str(rec.get("key") or "") == key:
                return rec
    if title:
        needle = normalize(title)
        for rec in recordings:
            candidate = normalize(str(rec.get("title") or ""))
            if candidate and (candidate in needle or needle in candidate):
                return rec
    return None


def recording_full_text(rec: dict[str, Any]) -> str:
    parts = []
    for seg in rec.get("segments") or []:
        if isinstance(seg, dict):
            parts.append(str(seg.get("text") or ""))
    return "".join(parts)


def _sample_shingles(text: str, size: int = 8, limit: int = 400) -> list[str]:
    normalized = normalize(text)
    if len(normalized) <= size:
        return [normalized] if normalized else []
    step = max(1, (len(normalized) - size) // limit)
    return [normalized[i : i + size] for i in range(0, len(normalized) - size, step)][:limit]


def match_recording_by_text(text: str, *, min_score: float = 0.3) -> dict[str, Any] | None:
    """Return the demo recording whose transcript best overlaps ``text``.

    ``text`` is typically a rendered analysis prompt or a chat context block.
    Used by the mock providers to return the hand-written demo analyses.
    """
    if not text:
        return None
    haystack = normalize(text)
    if not haystack:
        return None
    best: tuple[float, dict[str, Any]] | None = None
    for rec in demo_recordings():
        sample = _sample_shingles(recording_full_text(rec))
        if not sample:
            continue
        hits = sum(1 for shingle in sample if shingle in haystack)
        score = hits / len(sample)
        if best is None or score > best[0]:
            best = (score, rec)
    if best is not None and best[0] >= min_score:
        return best[1]
    return None


def match_recording_by_summary(text: str, *, min_score: float = 0.25) -> dict[str, Any] | None:
    """Match a chat prompt context to a fixture recording via its summary markdown.

    Chat prompts only embed the retrieved chunks plus the summary, so the full
    transcript shingle match is too strict; the summary is a better signal.
    """
    haystack = normalize(text)
    if not haystack:
        return None
    best: tuple[float, dict[str, Any]] | None = None
    for rec in demo_recordings():
        summary = str(demo_insights(rec).get("summary_md") or "")
        sample = _sample_shingles(summary, limit=150)
        if not sample:
            continue
        hits = sum(1 for shingle in sample if shingle in haystack)
        score = hits / len(sample)
        if best is None or score > best[0]:
            best = (score, rec)
    if best is not None and best[0] >= min_score:
        return best[1]
    return None


def _query_grams(text: str) -> set[str]:
    normalized = normalize(text)
    if not normalized:
        return set()
    if len(normalized) <= 1:
        return {normalized}
    return {normalized[i : i + 2] for i in range(len(normalized) - 1)}


def find_chat_example(question: str) -> tuple[dict[str, Any], str] | None:
    """Match a user question against all fixture ``chat_examples`` maps.

    Returns ``(recording_fixture, answer)`` or None. Matching tries exact
    normalized equality first, then containment, then CJK bigram overlap.
    """
    question_norm = normalize(question)
    if not question_norm:
        return None
    question_grams = _query_grams(question)
    best: tuple[float, dict[str, Any], str] | None = None
    for rec in demo_recordings():
        examples = rec.get("chat_examples") or {}
        if not isinstance(examples, dict):
            continue
        for raw_question, answer in examples.items():
            if not isinstance(answer, str) or not answer.strip():
                continue
            candidate = normalize(str(raw_question))
            if not candidate:
                continue
            if candidate == question_norm:
                return rec, answer
            if candidate in question_norm or question_norm in candidate:
                return rec, answer
            candidate_grams = _query_grams(candidate)
            if not candidate_grams or not question_grams:
                continue
            overlap = len(question_grams & candidate_grams) / len(question_grams | candidate_grams)
            if best is None or overlap > best[0]:
                best = (overlap, rec, answer)
    if best is not None and best[0] >= 0.45:
        return best[1], best[2]
    return None


def fixture_speaker_labels(rec: dict[str, Any]) -> list[str]:
    """Ordered speaker labels for a fixture recording."""
    labels: list[str] = []
    for speaker in rec.get("speakers") or []:
        if isinstance(speaker, dict):
            label = str(speaker.get("label") or speaker.get("display_name") or "").strip()
            if label:
                labels.append(label)
    if not labels:
        seen: set[int] = set()
        for segment in rec.get("segments") or []:
            if isinstance(segment, dict):
                idx = int(segment.get("speaker") or 0)
                seen.add(idx)
        labels = [f"说话人{i + 1}" for i in sorted(seen)] or ["说话人1"]
    return labels


def fixture_segments(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalized segment dicts (start_ms/end_ms/text/speaker_index/speaker_label)."""
    speakers = fixture_speaker_labels(rec)
    result: list[dict[str, Any]] = []
    for raw in rec.get("segments") or []:
        if not isinstance(raw, dict):
            continue
        speaker_idx = raw.get("speaker")
        try:
            speaker_idx = int(speaker_idx) if speaker_idx is not None else 0
        except (TypeError, ValueError):
            speaker_idx = 0
        label = speakers[speaker_idx] if 0 <= speaker_idx < len(speakers) else speakers[0]
        result.append(
            {
                "start_ms": max(0, int(raw.get("start_ms") or 0)),
                "end_ms": max(0, int(raw.get("end_ms") or 0)),
                "text": str(raw.get("text") or "").strip(),
                "speaker_index": speaker_idx,
                "speaker_label": label,
            }
        )
    return result


def demo_insights(rec: dict[str, Any]) -> dict[str, Any]:
    """Return the analysis payloads of a fixture recording in canonical form."""
    raw = rec.get("insights") or {}
    if not isinstance(raw, dict):
        return {}

    def _unwrap(value: Any, *keys: str) -> Any:
        if isinstance(value, dict):
            for key in keys:
                if key in value:
                    return value[key]
        return value

    return {
        "summary_md": (
            ((raw.get("summary") or {}).get("markdown") if isinstance(raw.get("summary"), dict) else None)
            or raw.get("summary_md")
            or ""
        ),
        "chapters": _unwrap(raw.get("chapters"), "chapters") or [],
        "viewpoint_tree": _unwrap(raw.get("viewpoint_tree"), "root") or {},
        "insights": _unwrap(raw.get("insights"), "insights") or [],
        "action_items": _unwrap(raw.get("action_items"), "action_items") or [],
        "suggested_questions": _unwrap(raw.get("suggested_questions"), "questions") or [],
        "keywords": _unwrap(raw.get("keywords"), "keywords") or [],
    }


__all__ = [
    "DEMO_DATA_PATH",
    "demo_data_path",
    "demo_insights",
    "demo_recordings",
    "find_chat_example",
    "find_recording",
    "fixture_segments",
    "fixture_speaker_labels",
    "load_demo_data",
    "match_recording_by_text",
    "match_recording_by_summary",
    "normalize",
    "recording_full_text",
]
