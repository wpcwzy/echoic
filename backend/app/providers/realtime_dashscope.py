"""DashScope Paraformer realtime ASR provider for the live transcription.

Implements the WebSocket duplex protocol from docs/research/dashscope-api.md §4:
connect with a Bearer header → ``run-task`` → ``task-started`` → binary PCM16LE
16 kHz mono frames → ``result-generated`` (partial / final sentences) →
``finish-task`` → ``task-finished``.

Known limitations (documented):
* no automatic reconnect / task rolling (a dropped connection fails the session);
* ``paraformer-realtime-v2`` has no speaker diarization → ``speaker_label`` is
  always ``None``;
* API keys never appear in logs or error messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from ..core.errors import ProviderError
from ._shared import redact

logger = logging.getLogger(__name__)

DEFAULT_REALTIME_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
DEFAULT_MODEL = "paraformer-realtime-v2"
START_TIMEOUT_S = 30.0
FINISH_TIMEOUT_S = 60.0


class DashScopeRealtimeProvider:
    name = "dashscope_realtime"

    def __init__(
        self,
        base_url: str = "",
        model: str = DEFAULT_MODEL,
        api_key: str = "",
        *,
        language: str = "zh",
        sample_rate: int = 16000,
    ) -> None:
        self.base_url = (base_url or DEFAULT_REALTIME_URL).strip()
        self.model = model or DEFAULT_MODEL
        self.api_key = api_key
        self.language = language or "zh"
        self.sample_rate = sample_rate
        self.events: asyncio.Queue = asyncio.Queue()
        self._ws: Any = None
        self._task_id = ""
        self._reader_task: asyncio.Task | None = None
        self._finished = asyncio.Event()
        self._started = False

    # --------------------------------------------------------------- messages
    def _run_task_message(self) -> dict:
        return {
            "header": {"action": "run-task", "task_id": self._task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self.model,
                "input": {},
                "parameters": {
                    "format": "pcm",
                    "sample_rate": self.sample_rate,
                    "language_hints": [self.language or "zh"],
                    "semantic_punctuation_enabled": True,
                    "max_sentence_silence": 1300,
                },
            },
        }

    def _finish_task_message(self) -> dict:
        return {
            "header": {"action": "finish-task", "task_id": self._task_id, "streaming": "duplex"},
            "payload": {"input": {}},
        }

    @staticmethod
    def _parse_event(raw: str | bytes) -> dict:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return event if isinstance(event, dict) else {}

    @staticmethod
    def _timestamps(sentence: dict) -> tuple[int, int]:
        try:
            start = max(0, int(sentence.get("begin_time") or 0))
        except (TypeError, ValueError):
            start = 0
        try:
            end = max(0, int(sentence.get("end_time") or 0))
        except (TypeError, ValueError):
            end = 0
        return start, max(start + 200, end)

    def _sentence_event(self, sentence: dict) -> dict | None:
        if not sentence or sentence.get("heartbeat"):
            return None
        text = str(sentence.get("text") or "").strip()
        if not text:
            return None
        start, end = self._timestamps(sentence)
        event_type = "partial" if sentence.get("sentence_end") is False else "final"
        return {"type": event_type, "text": text, "start_ms": start, "end_ms": end, "speaker_label": None}

    # ------------------------------------------------------------ provider API
    async def start(self, *, language: str = "", resume_from_ms: int = 0) -> None:
        """Open the duplex task.

        ``resume_from_ms`` is accepted for interface parity with the mock
        provider: upstream sentence timestamps are task-relative, so the live
        session manager applies the resume offset when persisting/replaying
        events. A resumed connection starts a fresh upstream task (GA has
        no task rolling) which is fine because only new audio is streamed.
        """
        import websockets

        if language:
            self.language = language
        if resume_from_ms:
            logger.info("dashscope realtime ASR resuming stream at %sms", resume_from_ms)
        if not self.api_key:
            raise ProviderError("实时转写未配置 DashScope API Key，请在设置中填写", provider=self.name)
        self.events = asyncio.Queue()
        self._finished = asyncio.Event()
        self._task_id = uuid.uuid4().hex
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            self._ws = await websockets.connect(
                self.base_url,
                additional_headers=headers,
                max_size=None,
                ping_interval=20,
                open_timeout=START_TIMEOUT_S,
                close_timeout=5,
            )
        except Exception as exc:  # websockets / network errors
            raise ProviderError(
                f"DashScope 实时转写连接失败（{type(exc).__name__}），请检查网络与 API Key 地域配置",
                provider=self.name,
            ) from exc
        try:
            await self._ws.send(json.dumps(self._run_task_message(), ensure_ascii=False))
            started = await self._wait_started()
        except ProviderError:
            await self._safe_close()
            raise
        except Exception as exc:
            await self._safe_close()
            raise ProviderError(
                f"DashScope 实时转写启动失败（{type(exc).__name__}）", provider=self.name
            ) from exc
        if not started:
            await self._safe_close()
            raise ProviderError("DashScope 实时转写启动超时（未收到 task-started）", provider=self.name)
        self._started = True
        self._reader_task = asyncio.create_task(self._reader_loop())
        logger.info("dashscope realtime ASR started (model=%s)", self.model)

    async def _wait_started(self) -> bool:
        deadline = asyncio.get_running_loop().time() + START_TIMEOUT_S
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            event = self._parse_event(raw)
            header = event.get("header") or {}
            name = header.get("event")
            if name == "task-started":
                return True
            if name == "task-failed":
                raise ProviderError(
                    f"DashScope 实时转写启动失败：{redact(str(header.get('error_message') or ''))}",
                    provider=self.name,
                )

    async def _reader_loop(self) -> None:
        try:
            async for raw in self._ws:
                event = self._parse_event(raw)
                header = event.get("header") or {}
                name = header.get("event")
                if name == "result-generated":
                    output = (event.get("payload") or {}).get("output") or {}
                    sentence = output.get("sentence") or {}
                    payload = self._sentence_event(sentence)
                    if payload is not None:
                        self.events.put_nowait(payload)
                elif name == "task-finished":
                    self._finished.set()
                    return
                elif name == "task-failed":
                    message = str(header.get("error_message") or "task-failed")
                    self.events.put_nowait(
                        {"type": "error", "message": f"DashScope 实时转写失败：{redact(message)}", "code": "PROVIDER_ERROR"}
                    )
                    self._finished.set()
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # connection dropped mid-session
            if not self._finished.is_set():
                self.events.put_nowait(
                    {
                        "type": "error",
                        "message": f"DashScope 实时转写连接中断（{type(exc).__name__}），本版本不支持自动重连",
                        "code": "PROVIDER_ERROR",
                    }
                )
            self._finished.set()
        finally:
            self.events.put_nowait(None)

    async def send_audio(self, pcm: bytes) -> None:
        if not self._started or self._ws is None or not pcm:
            return
        try:
            await self._ws.send(pcm)
        except Exception as exc:  # connection closed
            raise ProviderError(
                f"DashScope 实时转写连接已断开（{type(exc).__name__}）", provider=self.name
            ) from exc

    async def finish(self) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(self._finish_task_message(), ensure_ascii=False))
        except Exception:
            logger.info("dashscope realtime finish-task could not be sent (connection already closed)")
        try:
            await asyncio.wait_for(self._finished.wait(), timeout=FINISH_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("dashscope realtime did not receive task-finished within %ss", FINISH_TIMEOUT_S)
        finally:
            self.events.put_nowait(None)

    async def close(self) -> None:
        self._started = False
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - best-effort cleanup
                pass
            self._reader_task = None
        await self._safe_close()

    async def _safe_close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is None:
            return
        try:
            await ws.close()
        except Exception:  # pragma: no cover - best effort
            pass


__all__ = ["DEFAULT_MODEL", "DEFAULT_REALTIME_URL", "DashScopeRealtimeProvider"]
