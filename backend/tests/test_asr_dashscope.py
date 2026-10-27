"""DashScope ASR provider regression tests (URL normalization + realtime handshake).

No network: the realtime WebSocket is faked, the HTTP file-recording endpoints are
mocked with respx. The realtime handshake test guards the v0.3.0 bug where the
provider waited for ``task-started`` before starting its reader task (deadlock).
"""

from __future__ import annotations

import asyncio
import json
import wave
from pathlib import Path

import httpx
import pytest
import respx
import websockets

from app.core.errors import ProviderError
from app.providers.asr_dashscope import (
    DashScopeFileASRProvider,
    DashScopeRealtimeASRProvider,
    normalize_dashscope_file_base,
    normalize_dashscope_realtime_url,
)


# --------------------------------------------------------------------- helpers
def test_normalize_dashscope_file_base():
    assert normalize_dashscope_file_base("") == "https://dashscope.aliyuncs.com"
    assert normalize_dashscope_file_base("https://dashscope.aliyuncs.com/") == "https://dashscope.aliyuncs.com"
    assert (
        normalize_dashscope_file_base("https://dashscope.aliyuncs.com/api/v1")
        == "https://dashscope.aliyuncs.com"
    )
    assert (
        normalize_dashscope_file_base("https://dashscope.aliyuncs.com/compatible-mode/v1")
        == "https://dashscope.aliyuncs.com"
    )
    assert (
        normalize_dashscope_file_base("wss://dashscope.aliyuncs.com/api-ws/v1/inference")
        == "https://dashscope.aliyuncs.com"
    )
    assert (
        normalize_dashscope_file_base("https://abc.cn-beijing.maas.aliyuncs.com")
        == "https://abc.cn-beijing.maas.aliyuncs.com"
    )


def test_normalize_dashscope_realtime_url():
    ws = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    assert normalize_dashscope_realtime_url("") == ws
    assert normalize_dashscope_realtime_url("wss://dashscope.aliyuncs.com/api-ws/v1/inference") == ws
    assert normalize_dashscope_realtime_url("https://dashscope.aliyuncs.com") == ws
    assert normalize_dashscope_realtime_url("https://dashscope.aliyuncs.com/api/v1") == ws
    assert normalize_dashscope_realtime_url("dashscope.aliyuncs.com") == ws
    assert (
        normalize_dashscope_realtime_url("https://abc.cn-beijing.maas.aliyuncs.com")
        == "wss://abc.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
    )


def _write_wav(path: Path, seconds: float = 0.5) -> None:
    frames = int(16000 * seconds)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * frames)


class _FakeWebSocket:
    """Minimal websockets-compatible connection driven by our own protocol."""

    def __init__(self, *, fail_on_run: bool = False) -> None:
        self.fail_on_run = fail_on_run
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[bytes | str] = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.closed = True
        await self.queue.put(None)
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.queue.get()
        if event is None:
            raise StopAsyncIteration
        return event

    async def send(self, data):
        self.sent.append(data)
        if not isinstance(data, str):
            return
        message = json.loads(data)
        action = message.get("header", {}).get("action")
        if action == "run-task":
            if self.fail_on_run:
                await self.queue.put(
                    json.dumps(
                        {
                            "header": {
                                "event": "task-failed",
                                "error_message": "Invalid model: paraformer-realtime-v2",
                            }
                        }
                    )
                )
            else:
                await self.queue.put(json.dumps({"header": {"event": "task-started"}}))
        elif action == "finish-task":
            await self.queue.put(
                json.dumps(
                    {
                        "header": {"event": "result-generated"},
                        "payload": {
                            "output": {
                                "sentence": {
                                    "begin_time": 0,
                                    "end_time": 800,
                                    "text": "你好，这是实时转写测试。",
                                    "sentence_end": True,
                                }
                            }
                        },
                    }
                )
            )
            await self.queue.put(json.dumps({"header": {"event": "task-finished"}}))


@pytest.fixture()
def fake_connect(monkeypatch):
    holder = {}

    def _install(*, fail_on_run: bool = False):
        connection = _FakeWebSocket(fail_on_run=fail_on_run)
        holder["connection"] = connection

        def _connect(*_args, **_kwargs):
            return connection

        monkeypatch.setattr(websockets, "connect", _connect)
        return connection

    return _install


async def test_realtime_transcribe_handshake_and_result(tmp_path, fake_connect):
    connection = fake_connect()
    audio = tmp_path / "audio.wav"
    _write_wav(audio)
    provider = DashScopeRealtimeASRProvider(
        "https://dashscope.aliyuncs.com/api/v1", "paraformer-realtime-v2", "sk-test-key"
    )
    result = await asyncio.wait_for(provider.transcribe(audio, language="zh"), timeout=5)
    assert result.segments and "实时转写" in result.segments[0].text
    assert result.provider == "dashscope_realtime"
    assert connection.sent and isinstance(connection.sent[0], str)
    assert json.loads(connection.sent[0])["header"]["action"] == "run-task"
    # normalized endpoint used for the connection
    assert provider.base_url == "wss://dashscope.aliyuncs.com/api-ws/v1/inference"


async def test_realtime_task_failed_during_handshake_is_reported(tmp_path, fake_connect):
    fake_connect(fail_on_run=True)
    audio = tmp_path / "audio.wav"
    _write_wav(audio)
    provider = DashScopeRealtimeASRProvider(
        "wss://dashscope.aliyuncs.com/api-ws/v1/inference", "paraformer-realtime-v2", "sk-test-key"
    )
    with pytest.raises(ProviderError) as excinfo:
        await asyncio.wait_for(provider.transcribe(audio, language="zh"), timeout=5)
    assert "启动失败" in excinfo.value.message or "Invalid model" in excinfo.value.message


@respx.mock
async def test_file_provider_policy_uses_normalized_url():
    policy = {
        "upload_host": "https://oss.example.com",
        "upload_dir": "dashscope/asr/tmp",
        "policy": "p",
        "signature": "s",
        "oss_access_key_id": "ak",
        "x_oss_object_acl": "private",
        "x_oss_forbid_overwrite": "true",
    }
    route = respx.get("https://dashscope.aliyuncs.com/api/v1/uploads").mock(
        return_value=httpx.Response(200, json={"output": policy})
    )
    provider = DashScopeFileASRProvider(
        "https://dashscope.aliyuncs.com/compatible-mode/v1", "paraformer-v2", "sk-test-key"
    )
    async with httpx.AsyncClient(trust_env=False) as client:
        result = await provider._get_policy(client)
    assert result["upload_host"] == "https://oss.example.com"
    assert route.called
    assert str(route.calls[0].request.url).startswith("https://dashscope.aliyuncs.com/api/v1/uploads")


@respx.mock
async def test_file_provider_404_has_actionable_message():
    respx.get("https://dashscope.aliyuncs.com/api/v1/uploads").mock(return_value=httpx.Response(404))
    provider = DashScopeFileASRProvider("", "paraformer-v2", "sk-test-key")
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(ProviderError) as excinfo:
            await provider._get_policy(client)
    assert "临时上传凭证获取失败" in excinfo.value.message
    assert "https://dashscope.aliyuncs.com" in excinfo.value.message
    assert "paraformer-v2" in excinfo.value.message


@respx.mock
async def test_file_provider_falls_back_to_legacy_domain():
    """Workspace/MaaS domains that 404 for /api/v1/uploads fall back to the legacy domain."""
    policy = {
        "upload_host": "https://oss.example.com",
        "upload_dir": "dashscope/asr/tmp",
        "policy": "p",
        "signature": "s",
        "oss_access_key_id": "ak",
    }
    custom = "https://abc.cn-beijing.maas.aliyuncs.com"
    old_route = respx.get(f"{custom}/api/v1/uploads").mock(return_value=httpx.Response(404))
    legacy_route = respx.get("https://dashscope.aliyuncs.com/api/v1/uploads").mock(
        return_value=httpx.Response(200, json={"output": policy})
    )
    provider = DashScopeFileASRProvider(custom, "paraformer-v2", "sk-test-key")
    async with httpx.AsyncClient(trust_env=False) as client:
        result = await provider._get_policy(client)
    assert result["upload_host"] == "https://oss.example.com"
    assert provider._effective_base == "https://dashscope.aliyuncs.com"
    assert old_route.called and legacy_route.called


@respx.mock
async def test_qwen_filetrans_policy_falls_back_to_paraformer_model():
    """Qwen-Audio Filetrans returns empty output from getPolicy; use a classic policy model."""
    policy = {
        "upload_host": "https://oss.example.com",
        "upload_dir": "dashscope/asr/tmp",
        "policy": "p",
        "signature": "s",
        "oss_access_key_id": "ak",
        "x_oss_object_acl": "private",
        "x_oss_forbid_overwrite": "true",
    }
    requested_models: list[str] = []

    def _handler(request):
        requested_models.append(request.url.params.get("model", ""))
        if request.url.params.get("model") == "qwen-audio-3.1-asr-flash-filetrans":
            return httpx.Response(200, json={"output": {}, "request_id": "x"})
        return httpx.Response(200, json={"output": policy})

    respx.get("https://dashscope.aliyuncs.com/api/v1/uploads").mock(side_effect=_handler)
    provider = DashScopeFileASRProvider(
        "https://dashscope.aliyuncs.com",
        "qwen-audio-3.1-asr-flash-filetrans",
        "sk-test-key",
    )
    async with httpx.AsyncClient(trust_env=False) as client:
        result = await provider._get_policy(client)
    assert result["upload_host"] == "https://oss.example.com"
    assert requested_models[:2] == [
        "qwen-audio-3.1-asr-flash-filetrans",
        "paraformer-v2",
    ]


@respx.mock
async def test_upload_uses_ascii_object_key_for_chinese_filename(tmp_path):
    """DashScope rejects non-ASCII temporary oss:// URLs (REQUEST_INVALID_FILE_URL_VALUE)."""
    policy = {
        "upload_host": "https://oss.example.com",
        "upload_dir": "dashscope/asr/tmp",
        "policy": "p",
        "signature": "s",
        "oss_access_key_id": "ak",
    }
    route = respx.post("https://oss.example.com").mock(return_value=httpx.Response(200, text="ok"))
    audio = tmp_path / "会议录音 2026.wav"
    audio.write_bytes(b"RIFFfake")
    provider = DashScopeFileASRProvider("https://dashscope.aliyuncs.com", "paraformer-v2", "sk-test-key")
    async with httpx.AsyncClient(trust_env=False) as client:
        file_url = await provider._upload(client, audio, policy)
    assert route.called
    sent_key = route.calls[0].request.content.decode("latin-1")
    # The returned URL that DashScope validates must be pure ASCII.
    assert file_url.isascii()
    assert file_url.startswith("oss://dashscope/asr/tmp/")
    assert file_url.endswith(".wav")
    assert "会议录音" not in file_url


@respx.mock
async def test_file_submit_sets_oss_resolve_header():
    """Qwen-Audio Filetrans needs X-DashScope-OssResourceResolve for private oss:// URLs."""
    route = respx.post("https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription").mock(
        return_value=httpx.Response(200, json={"output": {"task_id": "task-1", "task_status": "PENDING"}})
    )
    provider = DashScopeFileASRProvider(
        "https://dashscope.aliyuncs.com", "qwen-audio-3.1-asr-flash-filetrans", "sk-test-key"
    )
    async with httpx.AsyncClient(trust_env=False) as client:
        await provider._submit(client, "oss://bucket/path/file.wav", "zh")
    assert route.called
    headers = route.calls[0].request.headers
    assert headers.get("X-DashScope-OssResourceResolve") == "enable"
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "qwen-audio-3.1-asr-flash-filetrans"
    assert body["input"]["file_urls"] == ["oss://bucket/path/file.wav"]
