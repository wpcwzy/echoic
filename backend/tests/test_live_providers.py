"""Realtime provider tests (mock determinism + DashScope protocol parsing, offline)."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.config import settings
from app.core.errors import ProviderError
from app.db import Base, SessionLocal, init_db
from app.providers.realtime_dashscope import (
    DEFAULT_MODEL,
    DEFAULT_REALTIME_URL,
    DashScopeRealtimeProvider,
)
from app.providers.realtime_mock import (
    MOCK_SENTENCES,
    MockRealtimeProvider,
    PARTIAL_INTERVAL_MS,
)
from app.services import live as live_service
from app.services.settings_store import ensure_default_settings, update_settings

FRAME = b"\x00\x00" * 1600  # 100 ms of 16 kHz mono PCM16LE
BYTES_PER_MS = 32


# ------------------------------------------------------------------ fixtures
@pytest.fixture()
def database(db):
    init_db()
    with SessionLocal() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    ensure_default_settings(db)
    yield db


async def _collect_mock_events(provider: MockRealtimeProvider, frames: int = 40) -> list[dict]:
    await provider.start(language="zh")
    for _ in range(frames):
        await provider.send_audio(FRAME)
    await provider.finish()
    events: list[dict] = []
    while True:
        event = await asyncio.wait_for(provider.events.get(), timeout=5)
        if event is None:
            return events
        events.append(event)
    raise AssertionError("unreachable")


# ----------------------------------------------------------------- mock provider
async def test_mock_provider_is_deterministic():
    first = await _collect_mock_events(MockRealtimeProvider())
    second = await _collect_mock_events(MockRealtimeProvider())
    assert first == second
    assert first, "mock provider must emit events"
    types = [event["type"] for event in first]
    assert "partial" in types and "final" in types
    assert types.index("partial") < types.index("final")
    finals = [event for event in first if event["type"] == "final"]
    assert finals[0]["text"] == MOCK_SENTENCES[0]
    assert finals[0]["start_ms"] == 0
    assert finals[0]["end_ms"] == 3200
    assert all(event["speaker_label"] is None for event in first)


async def test_mock_provider_partials_advance_over_time():
    provider = MockRealtimeProvider()
    await provider.start(language="zh")
    partials_before = []
    for _ in range(17):  # 1700 ms -> partials at 800 ms and 1600 ms
        await provider.send_audio(FRAME)
    while not provider.events.empty():
        partials_before.append(provider.events.get_nowait())
    assert partials_before and all(event["type"] == "partial" for event in partials_before)
    texts = [event["text"] for event in partials_before]
    assert len(texts) >= 2
    assert len(texts[-1]) >= len(texts[0]), "partial hypothesis should grow"
    assert partials_before[0]["end_ms"] <= 1700

    # 1.6s more finishes the first sentence (boundary at 3.2s).
    for _ in range(16):
        await provider.send_audio(FRAME)
    events = []
    while not provider.events.empty():
        events.append(provider.events.get_nowait())
    assert any(event["type"] == "final" for event in events)
    await provider.finish()
    await provider.close()


async def test_mock_provider_finish_flushes_pending_partial():
    provider = MockRealtimeProvider()
    await provider.start(language="zh")
    for _ in range(9):  # 900 ms -> partial only, no final yet
        await provider.send_audio(FRAME)
    await provider.finish()
    events = []
    while True:
        event = await asyncio.wait_for(provider.events.get(), timeout=5)
        if event is None:
            break
        events.append(event)
    finals = [event for event in events if event["type"] == "final"]
    assert finals and finals[-1]["start_ms"] == 0
    assert finals[-1]["end_ms"] >= 900


async def test_mock_provider_silence_still_produces_captions():
    provider = MockRealtimeProvider()
    await provider.start(language="zh")
    await provider.send_audio(b"\x00\x00" * (16000 * 2))  # 2s of digital silence
    for _ in range(2):
        await provider.send_audio(FRAME)
    await provider.finish()
    events = []
    while True:
        event = await asyncio.wait_for(provider.events.get(), timeout=5)
        if event is None:
            break
        events.append(event)
    assert any(event["type"] == "final" for event in events)


# ------------------------------------------------------------- feature helpers
def test_resolve_live_provider_modes(database, monkeypatch):
    monkeypatch.setattr(settings, "live_provider", "mock")
    assert live_service.resolve_live_provider(database) == ("mock", "mock-realtime")

    monkeypatch.setattr(settings, "live_provider", "dashscope_realtime")
    provider, model = live_service.resolve_live_provider(database)
    assert provider == "dashscope_realtime"
    assert model == DEFAULT_MODEL

    monkeypatch.setattr(settings, "live_provider", "auto")
    # Default test settings use the mock ASR provider -> mock live provider.
    assert live_service.resolve_live_provider(database) == ("mock", "mock-realtime")


def test_live_features_block(database, monkeypatch):
    monkeypatch.setattr(settings, "live_enabled", True)
    monkeypatch.setattr(settings, "live_provider", "mock")
    monkeypatch.setattr(settings, "live_merge_mode", "promote")
    monkeypatch.setattr(settings, "live_max_minutes", 45)
    features = live_service.live_features(database)
    assert features == {
        "enabled": True,
        "provider": "mock",
        "model": "mock-realtime",
        "max_minutes": 45,
        "merge_mode": "promote",
    }


def test_build_provider_factory(database, monkeypatch):
    monkeypatch.setattr(settings, "live_provider", "mock")
    provider = live_service.build_live_provider(database, "mock", "mock-realtime")
    assert provider.name == "mock"
    with pytest.raises(ProviderError):
        live_service.build_live_provider(database, "unknown-provider")
    with pytest.raises(ProviderError):
        live_service.build_live_provider(database, "dashscope_realtime")  # no API key configured


# -------------------------------------------------------- DashScope (offline)
def test_dashscope_realtime_messages_and_parsing():
    provider = DashScopeRealtimeProvider(
        base_url=DEFAULT_REALTIME_URL,
        model=DEFAULT_MODEL,
        api_key="sk-test-key",
        language="zh",
    )
    provider._task_id = "task-123"
    run_task = provider._run_task_message()
    assert run_task["header"]["action"] == "run-task"
    assert run_task["payload"]["task_group"] == "audio"
    assert run_task["payload"]["task"] == "asr"
    assert run_task["payload"]["function"] == "recognition"
    assert run_task["payload"]["model"] == DEFAULT_MODEL
    assert run_task["payload"]["parameters"]["format"] == "pcm"
    assert run_task["payload"]["parameters"]["sample_rate"] == 16000
    assert run_task["payload"]["parameters"]["language_hints"] == ["zh"]

    finish_task = provider._finish_task_message()
    assert finish_task["header"]["action"] == "finish-task"
    assert finish_task["header"]["task_id"] == "task-123"

    partial = provider._sentence_event(
        {"begin_time": 100, "end_time": 900, "text": "我们开始", "sentence_end": False}
    )
    assert partial == {
        "type": "partial",
        "text": "我们开始",
        "start_ms": 100,
        "end_ms": 900,
        "speaker_label": None,
    }
    final = provider._sentence_event(
        {"begin_time": 100, "end_time": 1200, "text": "我们开始讨论。", "sentence_end": True}
    )
    assert final is not None and final["type"] == "final"
    assert provider._sentence_event({"heartbeat": True, "text": "x"}) is None
    assert provider._sentence_event({"text": ""}) is None

    event = provider._parse_event(
        json.dumps({"header": {"event": "task-started"}, "payload": {}}).encode()
    )
    assert event["header"]["event"] == "task-started"
    assert provider._parse_event(b"not json") == {}


async def test_dashscope_realtime_requires_api_key():
    provider = DashScopeRealtimeProvider(api_key="", model=DEFAULT_MODEL)
    with pytest.raises(ProviderError) as excinfo:
        await provider.start(language="zh")
    assert "API Key" in str(excinfo.value)


async def test_dashscope_realtime_connection_error_is_provider_error(monkeypatch):
    provider = DashScopeRealtimeProvider(
        base_url="wss://127.0.0.1:1/api-ws/v1/inference",
        model=DEFAULT_MODEL,
        api_key="sk-test-key",
    )
    with pytest.raises(ProviderError) as excinfo:
        await provider.start(language="zh")
    message = str(excinfo.value)
    assert "sk-test-key" not in message
    assert "连接失败" in message or "启动失败" in message


def test_realtime_asr_is_configured_independently(database, monkeypatch):
    update_settings(
        database,
        {
            "ai": {
                "asr": {
                    "provider": "dashscope_file",
                    "api_key": "sk-file-key-123456",
                    "base_url": "https://dashscope.aliyuncs.com",
                    "model": "qwen-audio-3.1-asr-flash-filetrans",
                },
                "realtime_asr": {
                    "provider": "dashscope_realtime",
                    "api_key": "sk-realtime-key-123456",
                    "base_url": "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
                    "model": "paraformer-realtime-v2",
                    "language": "en",
                },
            }
        },
    )
    monkeypatch.setattr(settings, "live_provider", "auto")
    assert live_service.resolve_live_provider(database) == ("dashscope_realtime", "paraformer-realtime-v2")
    provider = live_service.build_live_provider(database, "dashscope_realtime")
    assert provider.api_key == "sk-realtime-key-123456"
    assert provider.language == "en"


def test_auto_realtime_falls_back_to_file_asr_key(database, monkeypatch):
    update_settings(
        database,
        {
            "ai": {
                "asr": {
                    "provider": "dashscope_file",
                    "api_key": "sk-file-only-key-123456",
                    "base_url": "https://dashscope.aliyuncs.com",
                },
                "realtime_asr": {"provider": "auto", "api_key": "__clear__"},
            }
        },
    )
    monkeypatch.setattr(settings, "live_provider", "auto")
    provider, model = live_service.resolve_live_provider(database)
    assert provider == "dashscope_realtime"
    assert model == "paraformer-realtime-v2"
    built = live_service.build_live_provider(database, "dashscope_realtime")
    assert built.api_key == "sk-file-only-key-123456"
    assert built.base_url == "wss://dashscope.aliyuncs.com/api-ws/v1/inference"


def test_explicit_mock_realtime_overrides_file_asr(database, monkeypatch):
    update_settings(
        database,
        {
            "ai": {
                "asr": {"provider": "dashscope_file", "api_key": "sk-file-key-123456"},
                "realtime_asr": {"provider": "mock"},
            }
        },
    )
    monkeypatch.setattr(settings, "live_provider", "auto")
    assert live_service.resolve_live_provider(database) == ("mock", "mock-realtime")


# ------------------------------------------------ GA reconnect/resume (mock)
async def test_mock_provider_resume_continues_absolute_timeline():
    first = MockRealtimeProvider()
    await first.start(language="zh", resume_from_ms=0)
    for _ in range(40):  # 4000 ms
        await first.send_audio(FRAME)
    await first.finish()
    first_events = []
    while True:
        event = await asyncio.wait_for(first.events.get(), timeout=5)
        if event is None:
            break
        first_events.append(event)
    first_finals = [event for event in first_events if event["type"] == "final"]
    assert first_finals[0]["start_ms"] == 0 and first_finals[0]["end_ms"] == 3200

    # Reconnect at the last received audio position: the provider resumes in
    # sentence #2 and emits stream-relative timestamps again.
    second = MockRealtimeProvider()
    await second.start(language="zh", resume_from_ms=4000)
    for _ in range(40):  # 4000 -> 8000 ms absolute
        await second.send_audio(FRAME)
    await second.finish()
    second_events = []
    while True:
        event = await asyncio.wait_for(second.events.get(), timeout=5)
        if event is None:
            break
        second_events.append(event)
    finals = [event for event in second_events if event["type"] == "final"]
    assert finals, "resumed provider must emit finals"
    assert finals[0]["text"] == MOCK_SENTENCES[1]
    assert finals[0]["start_ms"] == 0  # clamped relative to resume position
    assert finals[0]["end_ms"] == 2800  # 6800 absolute - 4000 resume
    assert finals[-1]["end_ms"] == 4000  # flushed at 8000 absolute - 4000


async def test_mock_provider_close_terminates_event_stream_without_finals():
    provider = MockRealtimeProvider()
    await provider.start(language="zh")
    await provider.send_audio(FRAME)
    await provider.close()
    events = []
    while True:
        event = await asyncio.wait_for(provider.events.get(), timeout=5)
        if event is None:
            break
        events.append(event)
    assert not [event for event in events if event["type"] == "final"]
    assert all(event["type"] == "partial" for event in events)


def test_dashboard_dashscope_realtime_start_accepts_resume_offset():
    import inspect

    signature = inspect.signature(DashScopeRealtimeProvider.start)
    assert "resume_from_ms" in signature.parameters
    provider = DashScopeRealtimeProvider(api_key="sk-test", model=DEFAULT_MODEL)
    assert provider.model == DEFAULT_MODEL


def test_auto_realtime_never_uses_non_dashscope_file_asr(database, monkeypatch):
    """File ASR may be openai/dashscope_file; live must only use DashScope realtime or mock."""
    update_settings(
        database,
        {
            "ai": {
                "asr": {"provider": "openai", "api_key": "sk-openai-key-123456", "base_url": "https://api.openai.com/v1"},
                "realtime_asr": {"provider": "auto", "api_key": "__clear__"},
            }
        },
    )
    monkeypatch.setattr(settings, "live_provider", "auto")
    assert live_service.resolve_live_provider(database) == ("mock", "mock-realtime")
