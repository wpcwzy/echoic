"""Offline mock ASR provider.

No network access. Two modes (docs/PIPELINE.md §4.1):

* Demo fixture — audio longer than 15 minutes whose recording title / filename
  mentions ``traceopt``: replay the hand-written transcript from
  ``app/demo_data.json``.
* Synthetic — any other audio: generate a deterministic Chinese meeting
  transcript (8-14 segments, 2-4 speakers) spread over the real audio duration.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import wave
from pathlib import Path

from ..core import media
from .base import ASRResult, ASRSegment
from .demo_fixtures import fixture_segments, fixture_speaker_labels, find_recording

logger = logging.getLogger(__name__)

DEMO_MIN_DURATION_MS = 15 * 60 * 1000
FALLBACK_DURATION_MS = 10 * 60 * 1000

_SYNTHETIC_LINES: list[str] = [
    "好，我们开始吧，今天主要同步一下项目进展和接下来两周的安排。",
    "上周安排的事情基本都完成了，需求文档我更新到最新版本，会后发到群里。",
    "这里还有一个细节需要确认，接口字段的定义最好和前端再对一遍，避免联调返工。",
    "测试环境已经搭好了，下周一可以开始第一轮回归测试，我会先把用例梳理出来。",
    "这个风险的优先级比较高，建议我们提前准备一个备选方案，免得进度被卡住。",
    "时间上我觉得可以再压缩两天，但需要再加一个人手配合，具体我来协调。",
    "关于预算这部分，我先和财务确认一下，下周三之前给大家一个明确答复。",
    "数据统计的口径要统一，否则后面报表对不上，我建议先拉一个小会定下来。",
    "好的，那这块就按刚才讨论的方案推进，有进展随时在群里同步。",
    "文档这块我负责整理，周五之前把初稿发出来，大家帮忙看看有没有遗漏。",
    "还有一个问题，外部依赖的交付时间不确定，我们是不是要准备一个降级方案？",
    "我这边没有别的疑问了，主要是把时间节点和负责人确认清楚就行。",
    "那今天就先到这里，会后我把会议纪要和待办清单发给大家。",
    "对了，下次评审的时间定在下周四下午，地点还是老地方。",
]

_SPEAKER_LABELS = ["说话人1", "说话人2", "说话人3", "说话人4"]


def _wav_duration_ms(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate() or 16000
            if frames and rate:
                return int(frames / rate * 1000)
    except (wave.Error, OSError, EOFError):
        pass
    try:
        return media.media_duration_ms(path)
    except Exception:  # pragma: no cover - defensive
        return 0


def _recording_title_from_path(audio_path: Path) -> str:
    """Look up the recording title via the storage directory UUID (best effort)."""
    try:
        storage_uuid = audio_path.parent.name
        if not storage_uuid:
            return ""
        from sqlalchemy import select

        from ..db import SessionLocal
        from ..models import Recording

        with SessionLocal() as db:
            title = db.scalar(select(Recording.title).where(Recording.uuid == storage_uuid).limit(1))
            return str(title or "")
    except Exception:  # pragma: no cover - DB may not exist in isolated unit tests
        return ""


def _progress_steps(duration_ms: int) -> list[int]:
    """Progress ticks that grow faster for short files (mirrors upload progress UX)."""
    if duration_ms <= 2 * 60 * 1000:
        return [0, 25, 50, 75, 100]
    if duration_ms <= 15 * 60 * 1000:
        return [0, 10, 25, 40, 55, 70, 85, 100]
    return [0, 8, 16, 25, 34, 43, 52, 61, 70, 79, 88, 100]


class MockASRProvider:
    name = "mock"

    def __init__(self, model: str = "mock-asr", language: str = "zh") -> None:
        self.model = model or "mock-asr"
        self.language = language or "zh"

    async def transcribe(
        self,
        audio_path: Path,
        *,
        language: str = "zh",
        diarization: bool = True,
        progress=None,
    ) -> ASRResult:
        audio_path = Path(audio_path)
        duration_ms = _wav_duration_ms(audio_path) if audio_path.exists() else 0
        title = _recording_title_from_path(audio_path)
        hint = f"{title} {audio_path.name}"
        fixture = None
        if duration_ms >= DEMO_MIN_DURATION_MS and "traceopt" in hint.lower():
            fixture = find_recording(key="traceopt") or find_recording(title="TraceOpt")
        if fixture is None and duration_ms == 0:
            duration_ms = FALLBACK_DURATION_MS

        steps = _progress_steps(duration_ms)
        results: list[ASRSegment] = []
        speakers: list[str] = []

        if fixture is not None:
            fixture_segments_list = fixture_segments(fixture)
            labels = fixture_speaker_labels(fixture)
            for item in fixture_segments_list:
                results.append(
                    ASRSegment(
                        start_ms=item["start_ms"],
                        end_ms=item["end_ms"] or item["start_ms"] + 3000,
                        text=item["text"],
                        speaker_label=item["speaker_label"],
                        confidence=0.96,
                    )
                )
            speakers = list(labels)
            logger.info("mock ASR replaying demo fixture %r (%s segments)", fixture.get("key"), len(results))
        else:
            segment_count = max(8, min(14, 8 + duration_ms // (10 * 60 * 1000)))
            speaker_count = 2 + min(2, duration_ms // (20 * 60 * 1000))
            seed = int(hashlib.sha1(f"{duration_ms}:{title}".encode()).hexdigest()[:8], 16)
            slot = max(1000, duration_ms // segment_count)
            for index in range(segment_count):
                start = min(duration_ms - 500, index * slot) if duration_ms else index * slot
                end = min(duration_ms, start + int(slot * 0.86)) if duration_ms else start + int(slot * 0.86)
                speaker_index = (seed // (index + 3) + index * 3) % speaker_count
                text = _SYNTHETIC_LINES[(seed + index * 5) % len(_SYNTHETIC_LINES)]
                results.append(
                    ASRSegment(
                        start_ms=max(0, start),
                        end_ms=max(start + 500, end),
                        text=text,
                        speaker_label=_SPEAKER_LABELS[speaker_index],
                        confidence=round(0.88 + ((seed >> index) % 10) / 100, 2),
                    )
                )
            speakers = _SPEAKER_LABELS[:speaker_count]
            logger.info("mock ASR generated %s synthetic segments for %.1fs audio", segment_count, duration_ms / 1000)

        if progress:
            for pct in steps:
                progress(pct)
                await asyncio.sleep(0.003)

        text = "".join(segment.text for segment in results)
        return ASRResult(
            text=text,
            segments=results,
            speakers=speakers,
            language=language or self.language,
            provider=self.name,
            model=self.model,
            raw={"mode": "demo" if fixture is not None else "synthetic", "duration_ms": duration_ms},
        )


__all__ = ["MockASRProvider", "DEMO_MIN_DURATION_MS"]
