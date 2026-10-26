"""OpenAI-compatible provider tests with respx (no real network).

Covers: chat completion request/response, json_mode 400 fallback, SSE streaming,
Whisper multipart request shape + verbose_json parsing, embeddings parsing and
friendly ProviderError conversion (without leaking API keys).
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import httpx
import pytest
import respx

from app.core.errors import ProviderError
from app.providers.asr_openai import OpenAISRProvider
from app.providers.embeddings_openai import OpenAIEmbeddingProvider
from app.providers.llm_openai import OpenAILLMProvider

CHAT_URL = "https://llm.test/v1/chat/completions"
ASR_URL = "https://asr.test/v1/audio/transcriptions"
EMBEDDINGS_URL = "https://emb.test/v1/embeddings"
SECRET = "sk-test-secret-abcdef123456"


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    """Keep respx tests hermetic even when the dev shell exports proxy vars."""
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


def _silence_wav(path: Path, seconds: float = 1.0) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


async def test_chat_completion_json_mode_falls_back_on_400():
    provider = OpenAILLMProvider("https://llm.test/v1", "qwen-plus", SECRET)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(CHAT_URL).mock(
            side_effect=[
                httpx.Response(400, json={"error": {"message": "response_format unsupported"}}),
                httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]}),
            ]
        )
        result = await provider.complete(
            [{"role": "user", "content": "请输出 JSON"}], json_mode=True, max_tokens=128
        )
    assert result == '{"ok": true}'
    assert route.call_count == 2
    first_body = json.loads(route.calls[0].request.content)
    second_body = json.loads(route.calls[1].request.content)
    assert first_body["response_format"] == {"type": "json_object"}
    assert first_body["model"] == "qwen-plus"
    assert "response_format" not in second_body
    assert route.calls[0].request.headers["authorization"] == f"Bearer {SECRET}"


async def test_chat_stream_parses_sse():
    provider = OpenAILLMProvider("https://llm.test/v1", "qwen-plus", SECRET)
    sse = (
        'data: {"choices":[{"delta":{"content":"根据"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"转录稿，"}}]}\n\n'
        'data: {"choices":[{"delta":{}}]}\n\n'
        "data: [DONE]\n\n"
    )
    with respx.mock(assert_all_called=False) as router:
        router.post(CHAT_URL).mock(
            return_value=httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        )
        pieces = [piece async for piece in provider.stream([{"role": "user", "content": "hi"}])]
    assert "".join(pieces) == "根据转录稿，"


async def test_whisper_transcription_request_shape_and_parsing(tmp_path):
    provider = OpenAISRProvider("https://asr.test/v1", "whisper-1", SECRET)
    audio = _silence_wav(tmp_path / "meeting.wav")
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["content"] = request.content
        captured["content_type"] = request.headers.get("content-type", "")
        return httpx.Response(
            200,
            json={
                "task": "transcribe",
                "language": "zh",
                "duration": 1.0,
                "text": "你好世界",
                "segments": [
                    {"id": 0, "start": 0.0, "end": 0.9, "text": "你好世界", "avg_logprob": -0.1}
                ],
            },
        )

    with respx.mock(assert_all_called=False) as router:
        route = router.post(ASR_URL).mock(side_effect=_handler)
        result = await provider.transcribe(audio, language="zh", diarization=True)

    assert route.call_count == 1
    body = captured["content"]
    assert captured["content_type"].startswith("multipart/form-data")
    assert b'name="file"' in body
    assert b'name="model"' in body and b"whisper-1" in body
    assert b'name="language"' in body and b"zh" in body
    assert b'name="response_format"' in body and b"verbose_json" in body
    assert b'timestamp_granularities[]' in body and b"segment" in body
    assert captured and SECRET.encode() not in body  # key travels in header only
    assert result.provider == "openai"
    assert result.speakers == ["说话人1"]
    assert len(result.segments) == 1
    segment = result.segments[0]
    assert (segment.start_ms, segment.end_ms) == (0, 900)
    assert segment.text == "你好世界"
    assert segment.speaker_label == "说话人1"


async def test_whisper_transcription_falls_back_when_segments_missing(tmp_path):
    provider = OpenAISRProvider("https://asr.test/v1", "whisper-1", "")
    audio = _silence_wav(tmp_path / "meeting.wav")
    with respx.mock(assert_all_called=False) as router:
        router.post(ASR_URL).mock(
            return_value=httpx.Response(200, json={"text": "第一句话。第二句话。第三句话。"})
        )
        result = await provider.transcribe(audio, language="zh")
    assert len(result.segments) == 3
    assert result.speakers == ["说话人1"]
    assert [segment.text for segment in result.segments] == ["第一句话。", "第二句话。", "第三句话。"]


async def test_provider_http_error_becomes_provider_error_without_key():
    provider = OpenAILLMProvider("https://llm.test/v1", "qwen-plus", SECRET)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(CHAT_URL).mock(
            return_value=httpx.Response(401, json={"error": {"message": "invalid api key"}})
        )
        with pytest.raises(ProviderError) as excinfo:
            await provider.complete([{"role": "user", "content": "hi"}])
    assert route.call_count == 1  # 401 is not retried
    message = str(excinfo.value)
    assert "HTTP 401" in message
    assert SECRET not in message


async def test_embeddings_request_and_parsing():
    provider = OpenAIEmbeddingProvider("https://emb.test/v1", "text-embedding-3-small", SECRET, dimensions=4)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(EMBEDDINGS_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]},
                        {"index": 1, "embedding": [0.5, 0.6, 0.7, 0.8]},
                    ]
                },
            )
        )
        vectors = await provider.embed(["你好", "世界"])
    assert vectors == [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "text-embedding-3-small"
    assert body["input"] == ["你好", "世界"]
    assert body["dimensions"] == 4
    assert provider.dim == 4
