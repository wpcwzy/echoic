"""Deterministic offline realtime ASR provider for live transcription (GA).

No network. Audio is only used as a clock: every 32 bytes of PCM16LE 16 kHz mono
equals 1 ms of speech, so silent PCM still produces captions (handy for demos
and tests). Sentences come from a small built-in Chinese meeting corpus, a
partial hypothesis is emitted every ~800 ms and the finished sentence is emitted
as a ``final`` every ~3 s.

Reconnect/resume support (v0.3.10 GA): :meth:`start` accepts
``resume_from_ms`` (the absolute audio position already consumed by previous
connection(s)). The provider resumes at the matching sentence/partial position
and emits **stream-relative** timestamps; ``app.services.live`` adds the resume
offset back before persisting, so final segments stay contiguous and are never
duplicated across reconnects.

Provider interface consumed by ``app.services.live``::

    provider = MockRealtimeProvider(model="mock-realtime")
    await provider.start(language="zh", resume_from_ms=0)
    await provider.send_audio(pcm_bytes)
    await provider.finish()          # flushes the pending partial as a final
    async for event in provider.events:   # {"type": "partial"|"final", ...}
    await provider.close()

``provider.events`` is an ``asyncio.Queue`` receiving ``partial``/``final`` dicts
and a final ``None`` sentinel after :meth:`finish` or :meth:`close`.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

BYTES_PER_MS = 32  # 16 kHz * 2 bytes / 1000 ms
PARTIAL_INTERVAL_MS = 800

# Small deterministic Chinese meeting corpus (no network / no fixtures needed).
MOCK_SENTENCES: list[str] = [
    "好，我们开始今天的讨论。",
    "先同步一下这周的进展和结论。",
    "需求文档已经更新到最新版本。",
    "这里的接口字段还需要再确认一下。",
    "测试环境已经准备好，下周开始回归。",
    "这个风险建议提前准备一个备选方案。",
    "时间节点上还可以再压缩两天。",
    "预算部分我下周给一个明确答复。",
    "数据统计的口径需要先统一。",
    "那就按刚才讨论的方案继续推进。",
    "会议纪要我会在会后整理发给大家。",
    "外部依赖的交付时间还有不确定性。",
    "今天的讨论先到这里，谢谢大家。",
]
MOCK_SENTENCE_MS: list[int] = [3200, 3600, 2800, 3400, 3000, 2600, 3800, 3200, 3000, 3400, 3600, 3000, 2800]


@runtime_checkable
class LiveASRProvider(Protocol):
    """Minimal duplex streaming ASR provider used by the live session manager."""

    name: str
    model: str
    events: asyncio.Queue

    async def start(self, *, language: str = "", resume_from_ms: int = 0) -> None: ...

    async def send_audio(self, pcm: bytes) -> None: ...

    async def finish(self) -> None: ...

    async def close(self) -> None: ...


class MockRealtimeProvider:
    """Offline deterministic provider (default when realtime ASR is ``mock``)."""

    name = "mock"

    def __init__(self, model: str = "mock-realtime", language: str = "zh") -> None:
        self.model = model or "mock-realtime"
        self.language = language or "zh"
        self.events: asyncio.Queue = asyncio.Queue()
        self._started = False
        self._closed = False
        self._resume_ms = 0
        self._pos_ms = 0  # absolute audio position (ms)
        self._sentence_index = 0
        self._sentence_start_ms = 0  # absolute
        self._next_partial_ms = PARTIAL_INTERVAL_MS  # absolute

    # ------------------------------------------------------------------ clock
    def _current_sentence(self) -> str:
        return MOCK_SENTENCES[self._sentence_index % len(MOCK_SENTENCES)]

    def _current_duration(self) -> int:
        return MOCK_SENTENCE_MS[self._sentence_index % len(MOCK_SENTENCE_MS)]

    @staticmethod
    def _rel(value: int) -> int:
        return max(0, int(value))

    def _emit(self, payload: dict[str, Any]) -> None:
        self.events.put_nowait(payload)

    def _emit_partial(self) -> None:
        duration = self._current_duration()
        elapsed = self._pos_ms - self._sentence_start_ms
        progress = min(0.95, max(0.05, elapsed / max(1, duration)))
        sentence = self._current_sentence()
        reveal = max(2, min(len(sentence), int(math.ceil(len(sentence) * progress))))
        start_rel = self._rel(self._sentence_start_ms - self._resume_ms)
        end_rel = self._rel(self._pos_ms - self._resume_ms)
        self._emit(
            {
                "type": "partial",
                "text": sentence[:reveal],
                "start_ms": start_rel,
                "end_ms": max(start_rel + 200, end_rel),
                "speaker_label": None,
            }
        )

    def _emit_final(self, abs_start: int, abs_end: int) -> None:
        start_rel = self._rel(abs_start - self._resume_ms)
        end_rel = max(start_rel + 100, self._rel(abs_end - self._resume_ms))
        self._emit(
            {
                "type": "final",
                "text": self._current_sentence(),
                "start_ms": start_rel,
                "end_ms": end_rel,
                "speaker_label": None,
            }
        )

    def _tick(self) -> None:
        """Emit every partial/final whose audio-time boundary has been crossed."""
        guard = 0
        while guard < 1000:  # bounded loop even for pathologically large frames
            guard += 1
            duration = self._current_duration()
            sentence_end = self._sentence_start_ms + duration
            if self._pos_ms >= sentence_end:
                self._emit_final(self._sentence_start_ms, sentence_end)
                self._sentence_index += 1
                self._sentence_start_ms = sentence_end
                self._next_partial_ms = sentence_end + PARTIAL_INTERVAL_MS
                continue
            if self._pos_ms >= self._next_partial_ms:
                self._emit_partial()
                self._next_partial_ms += PARTIAL_INTERVAL_MS
                continue
            break

    # ------------------------------------------------------------ provider API
    async def start(self, *, language: str = "", resume_from_ms: int = 0) -> None:
        if language:
            self.language = language
        self.events = asyncio.Queue()
        self._started = True
        self._closed = False
        self._resume_ms = max(0, int(resume_from_ms or 0))
        self._pos_ms = self._resume_ms
        # Locate the sentence that contains the resume position.
        index = 0
        start = 0
        while True:
            duration = MOCK_SENTENCE_MS[index % len(MOCK_SENTENCE_MS)]
            if start + duration > self._resume_ms:
                break
            start += duration
            index += 1
        self._sentence_index = index
        self._sentence_start_ms = start
        # First partial tick strictly after the resume position.
        self._next_partial_ms = (self._resume_ms // PARTIAL_INTERVAL_MS + 1) * PARTIAL_INTERVAL_MS
        if self._resume_ms > 0:
            logger.info(
                "mock realtime ASR resumed at %sms (sentence #%s)", self._resume_ms, self._sentence_index
            )
        else:
            logger.info("mock realtime ASR started (language=%s)", self.language)

    async def send_audio(self, pcm: bytes) -> None:
        if not self._started or self._closed or not pcm:
            return
        self._pos_ms += len(pcm) // BYTES_PER_MS
        self._tick()
        await asyncio.sleep(0)  # cooperative: lets the event pump run

    async def finish(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Flush the sentence currently being spoken as a final so an explicit
        # stop does not lose the last partial caption.
        if self._pos_ms > self._sentence_start_ms:
            abs_end = max(self._pos_ms, self._sentence_start_ms + 500)
            self._emit_final(self._sentence_start_ms, abs_end)
            self._sentence_start_ms = abs_end
        self.events.put_nowait(None)
        logger.info(
            "mock realtime ASR finished (audio_pos=%sms, resume=%sms)",
            self._pos_ms,
            self._resume_ms,
        )

    async def close(self) -> None:
        already_closed = self._closed
        self._closed = True
        # Detach/disconnect path: no artificial final flush (the next connection
        # resumes from the same audio position), but the manager's event pump
        # must still terminate.
        if not already_closed:
            self.events.put_nowait(None)


__all__ = ["BYTES_PER_MS", "MOCK_SENTENCES", "LiveASRProvider", "MockRealtimeProvider", "PARTIAL_INTERVAL_MS"]
