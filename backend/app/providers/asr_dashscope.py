"""Aliyun DashScope ASR providers.

Two real paths (see docs/research/dashscope-api.md):

``dashscope_file`` (primary for private deployments)
    Upload the local audio through the temporary upload-policy endpoint
    (``GET /api/v1/uploads?action=getPolicy`` + multipart OSS POST), then submit
    an async transcription task with ``file_urls=[oss://...]``, poll
    ``GET /api/v1/tasks/{task_id}`` and immediately download the 24h-TTL result
    JSON (``transcripts[].sentences[]``, includes diarization + timestamps).

``dashscope_realtime`` (fallback, no diarization)
    Stream a local 16 kHz mono PCM WAV over the Paraformer realtime WebSocket
    (``paraformer-realtime-v2``).  The realtime model does not support speaker
    diarization, so everything is attributed to a single speaker (logged).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
import wave
from pathlib import Path
from typing import Any

import httpx

from ..core.errors import ProviderError
from ._shared import api_url, auth_headers, json_or_error, redact, request
from .base import ASRResult, ASRSegment

logger = logging.getLogger(__name__)

DEFAULT_FILE_BASE_URL = "https://dashscope.aliyuncs.com"
DEFAULT_REALTIME_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
FINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"}
POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = 1800.0
HANDSHAKE_TIMEOUT_S = 45.0

_HTTP_SUFFIXES = ("/api-ws/v1/inference", "/compatible-mode/v1", "/api/v1")


def _strip_known_path_suffix(base: str) -> str:
    value = (base or "").strip().rstrip("/")
    lowered = value.lower()
    for suffix in _HTTP_SUFFIXES:
        if lowered.endswith(suffix):
            value = value[: -len(suffix)].rstrip("/")
            lowered = value.lower()
    return value


def normalize_dashscope_file_base(base_url: str | None) -> str:
    """Normalize user-entered DashScope base URLs for file transcription.

    Accepts the domain, workspace MaaS domain, or a value accidentally pasted
    with ``/compatible-mode/v1`` / ``/api/v1``; strips those path suffixes so
    the provider can append its own ``/api/v1/...`` endpoints.
    """
    value = (base_url or "").strip() or DEFAULT_FILE_BASE_URL
    if value.startswith("ws://"):
        value = "http://" + value[5:]
    elif value.startswith("wss://"):
        value = "https://" + value[6:]
    value = _strip_known_path_suffix(value)
    if not value.startswith(("http://", "https://")):
        value = "https://" + value.lstrip("/")
    return value.rstrip("/")


def normalize_dashscope_realtime_url(base_url: str | None) -> str:
    """Normalize a realtime ASR base URL and always return the WS endpoint."""
    value = (base_url or "").strip() or DEFAULT_REALTIME_URL
    if value.startswith("http://"):
        value = "ws://" + value[7:]
    elif value.startswith("https://"):
        value = "wss://" + value[8:]
    value = value.rstrip("/")
    if value.lower().endswith("/api-ws/v1/inference"):
        return value
    value = _strip_known_path_suffix(value)
    if not value.startswith(("ws://", "wss://")):
        value = "wss://" + value.lstrip("/")
    return value.rstrip("/") + "/api-ws/v1/inference"

SINGLE_SPEAKER = "说话人1"


def _safe_error(payload: Any) -> str:
    try:
        return redact(json.dumps(payload, ensure_ascii=False))
    except (TypeError, ValueError):
        return redact(str(payload))


def _media_content_type(path: Path) -> str:
    from .asr_openai import _content_type

    return _content_type(path)


class DashScopeRealtimeASRProvider:
    name = "dashscope_realtime"
    supports_diarization = False

    def __init__(self, base_url: str, model: str, api_key: str, *, language: str = "zh", diarization: bool = True) -> None:
        self.base_url = normalize_dashscope_realtime_url(base_url)
        self.model = model or "paraformer-realtime-v2"
        self.api_key = api_key
        self.language = language or "zh"
        self.diarization = diarization

    # ------------------------------------------------------------------ helpers
    def _run_task_message(self, task_id: str, language: str) -> dict:
        parameters: dict[str, Any] = {
            "format": "pcm",
            "sample_rate": 16000,
            "language_hints": [language or self.language or "zh"],
            "semantic_punctuation_enabled": True,
            "max_sentence_silence": 1300,
        }
        return {
            "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self.model,
                "input": {},
                "parameters": parameters,
            },
        }

    def _finish_task_message(self, task_id: str) -> dict:
        return {
            "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {"input": {}},
        }

    @staticmethod
    def parse_event(raw: str | bytes) -> dict:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return event if isinstance(event, dict) else {}

    @staticmethod
    def _sentence_timestamps(sentence: dict) -> tuple[int, int]:
        try:
            start = max(0, int(sentence.get("begin_time") or 0))
        except (TypeError, ValueError):
            start = 0
        try:
            end = max(0, int(sentence.get("end_time") or 0))
        except (TypeError, ValueError):
            end = 0
        return start, max(start + 200, end)

    def _validate_wav(self, audio_path: Path) -> tuple[int, int]:
        try:
            with wave.open(str(audio_path), "rb") as wav:
                channels = wav.getnchannels()
                sample_width = wav.getsampwidth()
                rate = wav.getframerate()
                frames = wav.getnframes()
        except (wave.Error, OSError, EOFError) as exc:
            raise ProviderError(
                f"无法读取音频文件（需 16kHz 单声道 PCM WAV）：{exc}；请确认 ffmpeg 已安装并重试",
                provider=self.name,
            ) from exc
        if channels != 1 or sample_width != 2 or rate != 16000:
            raise ProviderError(
                f"DashScope 实时转写要求 16kHz 单声道 16bit PCM WAV（当前 {rate}Hz/{channels}声道/{sample_width * 8}bit）",
                provider=self.name,
            )
        return frames, rate

    # ------------------------------------------------------------------- public
    async def transcribe(
        self,
        audio_path: Path,
        *,
        language: str = "zh",
        diarization: bool = True,
        progress=None,
    ) -> ASRResult:
        import websockets

        audio_path = Path(audio_path)
        if not audio_path.exists():
            raise ProviderError(f"找不到待转写的音频文件：{audio_path.name}", provider=self.name)
        if not self.api_key:
            raise ProviderError("未配置 DashScope API Key，请在设置中填写阿里云百炼 API Key", provider=self.name)
        if diarization and not self.supports_diarization:
            logger.info(
                "%s（%s）不支持说话人分离，已降级为单说话人「%s」",
                self.name,
                self.model,
                SINGLE_SPEAKER,
            )
        frames, rate = self._validate_wav(audio_path)
        duration_ms = int(frames / rate * 1000) if rate else 0
        task_id = uuid.uuid4().hex
        sentences: list[dict] = []
        failure: list[str] = []
        finished = asyncio.Event()
        started = asyncio.Event()

        async def reader(ws) -> None:
            try:
                async for raw in ws:
                    event = self.parse_event(raw)
                    header = event.get("header") or {}
                    event_name = header.get("event")
                    if event_name == "task-started":
                        started.set()
                    elif event_name == "result-generated":
                        output = (event.get("payload") or {}).get("output") or {}
                        sentence = output.get("sentence") or {}
                        if not sentence or sentence.get("heartbeat"):
                            continue
                        if sentence.get("sentence_end") is False:
                            continue  # intermediate hypothesis, ignore
                        if sentence.get("text"):
                            sentences.append(sentence)
                    elif event_name == "task-failed":
                        failure.append(str(header.get("error_message") or "task-failed"))
                        finished.set()
                        return
                    elif event_name == "task-finished":
                        finished.set()
                        return
            except Exception as exc:  # connection closed etc.
                failure.append(type(exc).__name__)
                finished.set()

        headers = auth_headers(self.api_key)
        logger.info(
            "connecting DashScope realtime ASR (%s, %.1fs audio, endpoint=%s)",
            self.model,
            duration_ms / 1000,
            self.base_url,
        )
        try:
            async with websockets.connect(
                self.base_url,
                additional_headers=headers,
                max_size=None,
                ping_interval=20,
                open_timeout=30,
                close_timeout=10,
            ) as ws:
                await ws.send(json.dumps(self._run_task_message(task_id, language), ensure_ascii=False))
                # The reader MUST start before waiting for task-started: websockets buffers
                # frames but the event is only set inside ``reader``. Without this the
                # handshake would always deadlock until the 45s timeout.
                reader_task = asyncio.create_task(reader(ws))
                try:
                    loop = asyncio.get_running_loop()
                    handshake_deadline = loop.time() + HANDSHAKE_TIMEOUT_S
                    while not started.is_set():
                        if failure:
                            raise ProviderError(
                                f"DashScope 实时转写启动失败：{redact(failure[0])}",
                                provider=self.name,
                            )
                        if finished.is_set():
                            raise ProviderError(
                                "DashScope 实时转写任务在启动阶段即结束（未返回 task-started），"
                                f"请检查模型名称（{self.model}）、API Key 地域与实时转写并发额度",
                                provider=self.name,
                            )
                        if loop.time() >= handshake_deadline:
                            raise ProviderError(
                                f"DashScope 实时转写启动超时（{int(HANDSHAKE_TIMEOUT_S)}s 未收到 task-started）："
                                f"model={self.model}，endpoint={self.base_url}；"
                                "请确认 Base URL 为 wss://dashscope.aliyuncs.com/api-ws/v1/inference（或工作空间域名），"
                                "API Key 与地域一致，并检查该 Key 的实时转写并发/额度",
                                provider=self.name,
                            )
                        await asyncio.sleep(0.05)
                    total_bytes = frames * 2
                    sent_bytes = 0
                    frame_size = int(rate * 0.1)  # 100 ms
                    with wave.open(str(audio_path), "rb") as wav:
                        while True:
                            chunk = wav.readframes(frame_size)
                            if not chunk:
                                break
                            await ws.send(chunk)
                            sent_bytes += len(chunk)
                            if progress and total_bytes:
                                progress(min(99, int(sent_bytes * 100 / total_bytes)))
                            await asyncio.sleep(0)
                    await ws.send(json.dumps(self._finish_task_message(task_id), ensure_ascii=False))
                    final_timeout = max(60.0, min(900.0, duration_ms / 1000 * 0.5 + 60))
                    await asyncio.wait_for(finished.wait(), timeout=final_timeout)
                finally:
                    reader_task.cancel()
                    try:
                        await reader_task
                    except (asyncio.CancelledError, Exception):
                        pass
        except ProviderError:
            raise
        except asyncio.TimeoutError:
            raise ProviderError("DashScope 实时转写等待最终结果超时，请稍后重试", provider=self.name) from None
        except Exception as exc:  # websockets / network errors
            raise ProviderError(
                f"DashScope 实时转写连接失败（{type(exc).__name__}），请检查网络与 API Key 地域配置",
                provider=self.name,
            ) from exc

        if failure:
            raise ProviderError(f"DashScope 实时转写失败：{redact(failure[0])}", provider=self.name)
        if progress:
            progress(100)

        segments: list[ASRSegment] = []
        for sentence in sorted(sentences, key=lambda item: item.get("begin_time") or 0):
            start, end = self._sentence_timestamps(sentence)
            segments.append(
                ASRSegment(
                    start_ms=start,
                    end_ms=end,
                    text=str(sentence.get("text") or "").strip(),
                    speaker_label=SINGLE_SPEAKER,
                )
            )
        text = "".join(segment.text for segment in segments)
        return ASRResult(
            text=text,
            segments=segments,
            speakers=[SINGLE_SPEAKER],
            language=language or self.language,
            provider=self.name,
            model=self.model,
        )

    async def check(self) -> str:
        """Lightweight connectivity/credential check without transcribing audio."""
        import websockets

        if not self.api_key:
            raise ProviderError("未配置 DashScope API Key", provider=self.name)
        task_id = uuid.uuid4().hex
        started = False
        try:
            async with websockets.connect(
                self.base_url,
                additional_headers=auth_headers(self.api_key),
                max_size=None,
                ping_interval=20,
                open_timeout=15,
                close_timeout=5,
            ) as ws:
                await ws.send(json.dumps(self._run_task_message(task_id, self.language), ensure_ascii=False))
                deadline = asyncio.get_running_loop().time() + 20
                while asyncio.get_running_loop().time() < deadline:
                    event = self.parse_event(await ws.recv())
                    header = event.get("header") or {}
                    name = header.get("event")
                    if name == "task-started":
                        started = True
                        break
                    if name == "task-failed":
                        raise ProviderError(
                            f"DashScope 实时转写鉴权/启动失败：{redact(str(header.get('error_message') or ''))}",
                            provider=self.name,
                        )
                try:
                    await ws.send(json.dumps(self._finish_task_message(task_id), ensure_ascii=False))
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"DashScope 实时转写连接失败（{type(exc).__name__}），请检查网络与 API Key 地域配置",
                provider=self.name,
            ) from exc
        if not started:
            raise ProviderError("DashScope 实时转写连接超时（未收到 task-started）", provider=self.name)
        return f"连接成功，DashScope 实时语音识别服务可用（{self.base_url} · {self.model}）"


class DashScopeFileASRProvider:
    name = "dashscope_file"

    def __init__(self, base_url: str, model: str, api_key: str, *, language: str = "zh", diarization: bool = True) -> None:
        self.base_url = normalize_dashscope_file_base(base_url)
        self._effective_base = self.base_url
        self.model = model or "paraformer-v2"
        self.api_key = api_key
        self.language = language or "zh"
        self.diarization = diarization

    # ------------------------------------------------------------------ helpers
    def _headers(self, *, async_call: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if async_call:
            headers["Content-Type"] = "application/json"
            headers["X-DashScope-Async"] = "enable"
        return headers

    def _policy_candidates(self) -> list[str]:
        """Base URLs to try for the temporary-upload policy endpoint.

        Workspace-specific MaaS domains occasionally expose a different path
        layout; the legacy ``dashscope.aliyuncs.com`` endpoint is still
        supported and accepts the same CN API keys, so use it as a fallback
        instead of failing with an opaque 404.
        """
        candidates = [self.base_url]
        if self.base_url != DEFAULT_FILE_BASE_URL and "dashscope.aliyuncs.com" not in self.base_url:
            candidates.append(DEFAULT_FILE_BASE_URL)
        return candidates

    def _policy_models(self) -> list[str]:
        """Models to try for getPolicy, most specific first.

        Some file-transcription models (notably the Qwen-Audio Filetrans
        family) are not accepted by the legacy temporary-upload-policy
        endpoint and return HTTP 200 with an empty ``output``. The policy
        itself is model-agnostic OSS credentials, so fall back to the
        classic Paraformer/Fun-ASR policy models before giving up.
        """
        seen: list[str] = []
        for model in (self.model, "paraformer-v2", "fun-asr"):
            value = (model or "").strip()
            if value and value not in seen:
                seen.append(value)
        return seen

    @staticmethod
    def _find_policy_payload(data: dict) -> dict | None:
        required = ("upload_host", "upload_dir", "policy", "signature", "oss_access_key_id")
        queue: list[Any] = [data]
        seen_ids: set[int] = set()
        while queue:
            item = queue.pop(0)
            if not isinstance(item, dict) or id(item) in seen_ids:
                continue
            seen_ids.add(id(item))
            if all(item.get(key) for key in required):
                return item
            for key in ("output", "data", "result", "credentials", "upload_info", "upload", "oss"):
                nested = item.get(key)
                if isinstance(nested, dict):
                    queue.append(nested)
        return None

    async def _get_policy(self, client: httpx.AsyncClient) -> dict:
        last_error: ProviderError | None = None
        attempted: list[str] = []
        missing_details: list[str] = []
        for candidate in self._policy_candidates():
            for policy_model in self._policy_models():
                attempted.append(f"{candidate} model={policy_model}")
                try:
                    response = await request(
                        client,
                        "GET",
                        api_url(candidate, "/api/v1/uploads"),
                        provider=self.name,
                        timeout=30.0,
                        headers=self._headers(),
                        params={"action": "getPolicy", "model": policy_model},
                    )
                    data = json_or_error(response, self.name)
                except ProviderError as exc:
                    if getattr(exc, "status_code", None) in (401, 403):
                        raise
                    if getattr(exc, "status_code", None) == 404:
                        last_error = exc
                        logger.warning(
                            "dashscope_file: policy endpoint 404 on %s, trying fallback if available",
                            candidate,
                        )
                        continue
                    missing_details.append(f"{candidate} model={policy_model}: {exc.message}")
                    continue
                policy = self._find_policy_payload(data)
                if policy is None:
                    snippet = redact(str(data))
                    missing_details.append(f"{candidate} model={policy_model}: 未包含上传凭证（{snippet}）")
                    logger.warning(
                        "dashscope_file: getPolicy returned no usable credentials for model=%s: %s",
                        policy_model,
                        snippet,
                    )
                    continue
                if candidate != self.base_url:
                    logger.warning(
                        "dashscope_file: custom base_url %s failed; fell back to %s. "
                        "Consider setting ECHOIC_ASR_BASE_URL=%s.",
                        self.base_url,
                        candidate,
                        candidate,
                    )
                if policy_model != self.model:
                    logger.warning(
                        "dashscope_file: %s is not accepted by the upload-policy endpoint; "
                        "using model=%s for the temporary OSS policy only.",
                        self.model,
                        policy_model,
                    )
                self._effective_base = candidate
                return policy
        detail = ("；".join(missing_details[-3:]))[:600]
        raise ProviderError(
            "DashScope 临时上传凭证获取失败：请确认 API Key 已开通录音文件识别、Base URL 为 "
            "https://dashscope.aliyuncs.com（工作空间域名填到域名根）。"
            f"当前模型={self.model}，已尝试={len(attempted)} 次"
            + (f"；最后一次返回：{detail}" if detail else "；接口未返回上传凭证"),
            provider=self.name,
            status_code=404 if last_error is not None and not missing_details else None,
        ) from last_error

    async def _upload(self, client: httpx.AsyncClient, audio_path: Path, policy: dict) -> str:
        # Keep the temporary OSS object key ASCII-only: DashScope validates the
        # final ``oss://`` URL strictly, and Chinese/spaces in the original
        # filename (common for meeting recordings) are rejected with
        # REQUEST_INVALID_FILE_URL_VALUE. The original name stays in Echoic's DB.
        suffix = (audio_path.suffix or ".wav").lower()
        if not suffix.startswith("."):
            suffix = f".{suffix}"
        object_key = f"{policy['upload_dir'].rstrip('/')}/{uuid.uuid4().hex}{suffix}"
        form = {
            "OSSAccessKeyId": policy["oss_access_key_id"],
            "Signature": policy["signature"],
            "policy": policy["policy"],
            "key": object_key,
            "x-oss-object-acl": policy.get("x_oss_object_acl") or "private",
            "x-oss-forbid-overwrite": policy.get("x_oss_forbid_overwrite") or "true",
            "success_action_status": "200",
            "x-oss-content-type": _media_content_type(audio_path),
        }
        try:
            with audio_path.open("rb") as handle:
                response = await request(
                    client,
                    "POST",
                    policy["upload_host"],
                    provider=self.name,
                    timeout=600.0,
                    data=form,
                    files={"file": (audio_path.name, handle, _media_content_type(audio_path))},
                )
        except OSError as exc:
            raise ProviderError(f"读取音频文件失败：{exc}", provider=self.name) from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"DashScope 临时上传失败（HTTP {response.status_code}）："
                f"{redact(response.text)}；建议改用 dashscope_realtime 或自建 Whisper 服务",
                provider=self.name,
                status_code=response.status_code,
            )
        return f"oss://{object_key}"

    async def _submit(self, client: httpx.AsyncClient, file_url: str, language: str) -> str:
        parameters: dict[str, Any] = {"channel_id": [0], "language_hints": [language or self.language or "zh"]}
        if self.diarization:
            parameters["diarization_enabled"] = True
        body = {
            "model": self.model,
            "input": {"file_urls": [file_url]},
            "parameters": parameters,
        }
        response = await request(
            client,
            "POST",
            api_url(self._effective_base, "/api/v1/services/audio/asr/transcription"),
            provider=self.name,
            timeout=60.0,
            headers=self._headers(async_call=True),
            json=body,
        )
        data = json_or_error(response, self.name)
        task_id = (data.get("output") or {}).get("task_id")
        if not task_id:
            raise ProviderError(f"DashScope 录音文件识别提交失败：{_safe_error(data)}", provider=self.name)
        return str(task_id)

    async def _poll(self, client: httpx.AsyncClient, task_id: str) -> dict:
        url = api_url(self._effective_base, f"/api/v1/tasks/{task_id}")
        deadline = asyncio.get_running_loop().time() + POLL_TIMEOUT_S
        delay = POLL_INTERVAL_S
        while True:
            response = await request(
                client, "GET", url, provider=self.name, timeout=60.0, headers=self._headers()
            )
            data = json_or_error(response, self.name)
            output = data.get("output") or {}
            status = str(output.get("task_status") or "").upper()
            if status in FINAL_STATUSES:
                return output
            if asyncio.get_running_loop().time() > deadline:
                raise ProviderError("DashScope 录音文件识别等待超时（超过 30 分钟）", provider=self.name)
            await asyncio.sleep(delay)
            delay = min(5.0, delay + 0.5)

    async def _download_result(self, client: httpx.AsyncClient, url: str) -> dict:
        response = await request(client, "GET", url, provider=self.name, timeout=180.0)
        if response.status_code >= 400:
            raise ProviderError(
                f"下载 DashScope 转写结果失败（HTTP {response.status_code}），结果 URL 可能已过期（24 小时有效）",
                provider=self.name,
                status_code=response.status_code,
            )
        try:
            data = response.json()
        except ValueError:
            raise ProviderError("DashScope 转写结果不是合法 JSON", provider=self.name) from None
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------- public
    async def transcribe(
        self,
        audio_path: Path,
        *,
        language: str = "zh",
        diarization: bool = True,
        progress=None,
    ) -> ASRResult:
        audio_path = Path(audio_path)
        if not audio_path.exists():
            raise ProviderError(f"找不到待转写的音频文件：{audio_path.name}", provider=self.name)
        if not self.api_key:
            raise ProviderError("未配置 DashScope API Key，请在设置中填写阿里云百炼 API Key", provider=self.name)
        logger.info("dashscope_file: uploading %s for async transcription (%s)", audio_path.name, self.model)
        async with httpx.AsyncClient() as client:
            if progress:
                progress(3)
            policy = await self._get_policy(client)
            if progress:
                progress(12)
            file_url = await self._upload(client, audio_path, policy)
            if progress:
                progress(25)
            task_id = await self._submit(client, file_url, language)
            logger.info("dashscope_file: task %s submitted, polling", task_id)
            if progress:
                progress(35)
            output = await self._poll(client, task_id)
            if progress:
                progress(85)
            result_url = ""
            subtask_error = ""
            for item in output.get("results") or []:
                if isinstance(item, dict) and item.get("subtask_status") == "SUCCEEDED" and item.get("transcription_url"):
                    result_url = str(item["transcription_url"])
                    break
                if isinstance(item, dict) and item.get("subtask_status") not in (None, "SUCCEEDED", "PENDING", "RUNNING"):
                    subtask_error = f"{item.get('code') or ''} {item.get('message') or ''}".strip()
            if not result_url:
                failure_payload = output.get("task_metrics") or output
                failure_text = redact(subtask_error) or _safe_error(failure_payload)
                hint = ""
                code = str(output.get("code") or "")
                if code == "REQUEST_INVALID_FILE_URL_VALUE":
                    hint = (
                        "。百炼拒绝了上传的临时文件 URL：临时 OSS 对象名已改为纯 ASCII；"
                        "若仍失败，请改用 paraformer-v2（原生支持临时上传凭证），"
                        "或将音频放到可公网访问的 HTTPS/OSS 地址"
                    )
                raise ProviderError(
                    f"DashScope 录音文件识别失败：{failure_text}{hint}",
                    provider=self.name,
                )
            payload = await self._download_result(client, result_url)
        result = self.parse_transcription(payload, language=language, provider_model=self.model)
        if progress:
            progress(100)
        return result

    @staticmethod
    def parse_transcription(payload: dict, *, language: str = "zh", provider_model: str = "paraformer-v2") -> ASRResult:
        segments: list[ASRSegment] = []
        labels: list[str] = []
        transcripts = payload.get("transcripts") or []
        for transcript in transcripts:
            if not isinstance(transcript, dict):
                continue
            for sentence in transcript.get("sentences") or []:
                if not isinstance(sentence, dict):
                    continue
                text = str(sentence.get("text") or "").strip()
                if not text:
                    continue
                try:
                    start = max(0, int(sentence.get("begin_time") or 0))
                except (TypeError, ValueError):
                    start = 0
                try:
                    end = max(0, int(sentence.get("end_time") or 0))
                except (TypeError, ValueError):
                    end = 0
                if end <= start:
                    end = start + 800
                speaker_label = None
                speaker_id = sentence.get("speaker_id")
                if speaker_id is not None:
                    try:
                        speaker_label = f"说话人{int(speaker_id) + 1}"
                    except (TypeError, ValueError):
                        speaker_label = None
                segments.append(
                    ASRSegment(
                        start_ms=start,
                        end_ms=end,
                        text=text,
                        speaker_label=speaker_label,
                    )
                )
                if speaker_label and speaker_label not in labels:
                    labels.append(speaker_label)
            if not segments and transcript.get("text"):
                segments.append(
                    ASRSegment(start_ms=0, end_ms=1000, text=str(transcript["text"]).strip(), speaker_label=None)
                )
        if not segments:
            raise ProviderError("DashScope 未返回任何转写句子，请检查音频是否包含语音", provider="dashscope_file")
        if not labels:
            labels = [SINGLE_SPEAKER]
            for segment in segments:
                segment.speaker_label = SINGLE_SPEAKER
        else:
            for segment in segments:
                if not segment.speaker_label:
                    segment.speaker_label = labels[0]
        segments.sort(key=lambda item: (item.start_ms, item.end_ms))
        text = "".join(segment.text for segment in segments)
        return ASRResult(
            text=text,
            segments=segments,
            speakers=labels,
            language=language or "zh",
            provider="dashscope_file",
            model=provider_model,
            raw={"duration_ms": (payload.get("properties") or {}).get("original_duration_in_milliseconds")},
        )

    async def check(self) -> str:
        """Lightweight auth/policy check (no transcription submitted)."""
        if not self.api_key:
            raise ProviderError("未配置 DashScope API Key", provider=self.name)
        async with httpx.AsyncClient() as client:
            await self._get_policy(client)
        return f"连接成功，DashScope 录音文件识别服务可用（{self._effective_base} · {self.model}）"


__all__ = [
    "DashScopeFileASRProvider",
    "DashScopeRealtimeASRProvider",
    "DEFAULT_FILE_BASE_URL",
    "DEFAULT_REALTIME_URL",
    "normalize_dashscope_file_base",
    "normalize_dashscope_realtime_url",
]
