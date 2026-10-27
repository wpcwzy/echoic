"""Provider registry: build ASR/LLM/Embedding providers from stored settings.

Builders apply default base URLs / model names for empty fields and raise
``ProviderError`` (friendly Chinese) when required configuration is missing.
``test_provider`` performs a lightweight connection check used by the settings
page (docs/API.md §6, docs/PIPELINE.md §4.2).
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
import wave
from pathlib import Path
from typing import Any

from ..core.errors import ProviderError
from ..services.settings_store import (
    DEFAULT_BASE_URLS,
    DEFAULT_MODELS,
    ASRConfig,
    EmbeddingConfig,
    LLMConfig,
    RealtimeASRConfig,
    effective_realtime_config,
    load_ai_config,
)
from .base import ASRProvider, EmbeddingProvider, LLMProvider

logger = logging.getLogger(__name__)

OPENAI_BASE = "https://api.openai.com/v1"
DASHSCOPE_BASE = "https://dashscope.aliyuncs.com"
DASHSCOPE_COMPATIBLE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _default_base(kind: str, provider: str) -> str:
    return DEFAULT_BASE_URLS.get(kind, {}).get(provider, "") or ""


def _default_model(kind: str, provider: str) -> str:
    return DEFAULT_MODELS.get(kind, {}).get(provider, "") or ""


def _apply_defaults(kind: str, provider: str, base_url: str, model: str) -> tuple[str, str]:
    return (base_url or _default_base(kind, provider)).rstrip("/"), model or _default_model(kind, provider)


def _needs_key(provider_label: str, api_key: str, base_url: str, official_base: str) -> None:
    """Local OpenAI-compatible servers may run without an API key; hosted ones may not."""
    if not api_key and (not base_url or base_url == official_base):
        raise ProviderError(
            f"{provider_label} 未配置 API Key，请先在设置页填写后再试",
            provider=provider_label,
        )


# --------------------------------------------------------------------------- ASR
def build_asr_provider(cfg: ASRConfig) -> ASRProvider:
    provider = (cfg.provider or "mock").strip()
    if provider == "mock":
        from .asr_mock import MockASRProvider

        return MockASRProvider(model=cfg.model or "mock-asr", language=cfg.language)

    base_url, model = _apply_defaults("asr", provider, cfg.base_url, cfg.model)
    if provider == "dashscope_realtime":
        from .asr_dashscope import DEFAULT_REALTIME_URL, DashScopeRealtimeASRProvider

        if not cfg.api_key:
            raise ProviderError("dashscope_realtime 未配置 DashScope API Key，请先在设置页填写", provider=provider)
        return DashScopeRealtimeASRProvider(
            cfg.base_url or base_url or DEFAULT_REALTIME_URL,
            model,
            cfg.api_key,
            language=cfg.language,
            diarization=cfg.diarization,
        )
    if provider == "dashscope_file":
        from .asr_dashscope import DashScopeFileASRProvider

        if not cfg.api_key:
            raise ProviderError("dashscope_file 未配置 DashScope API Key，请先在设置页填写", provider=provider)
        return DashScopeFileASRProvider(
            base_url or DASHSCOPE_BASE,
            model,
            cfg.api_key,
            language=cfg.language,
            diarization=cfg.diarization,
        )
    if provider == "openai":
        from .asr_openai import OpenAISRProvider

        _needs_key("OpenAI 兼容 ASR", cfg.api_key, base_url, OPENAI_BASE)
        return OpenAISRProvider(base_url or OPENAI_BASE, model, cfg.api_key, language=cfg.language)
    raise ProviderError(f"不支持的 ASR provider：{provider}", provider=provider)


# --------------------------------------------------------------------------- LLM
def build_llm_provider(cfg: LLMConfig) -> LLMProvider:
    provider = (cfg.provider or "mock").strip()
    if provider == "mock":
        from .llm_mock import MockLLMProvider

        return MockLLMProvider(model=cfg.model or "mock-llm", temperature=cfg.temperature)

    base_url, model = _apply_defaults("llm", provider, cfg.base_url, cfg.model)
    if provider == "openai":
        from .llm_openai import OpenAILLMProvider

        _needs_key("OpenAI 兼容 LLM", cfg.api_key, base_url, OPENAI_BASE)
        return OpenAILLMProvider(base_url or OPENAI_BASE, model, cfg.api_key, temperature=cfg.temperature)
    raise ProviderError(f"不支持的 LLM provider：{provider}", provider=provider)


# --------------------------------------------------------------------- Embedding
def build_embedding_provider(cfg: EmbeddingConfig) -> EmbeddingProvider | None:
    provider = (cfg.provider or "none").strip()
    if provider in ("none", ""):
        return None
    base_url, model = _apply_defaults("embeddings", provider, cfg.base_url, cfg.model)
    if provider == "mock":
        # v0.3.0 R-20: deterministic 256-dim hashing vectors, no network calls.
        from .embeddings_mock import MockEmbeddingProvider

        return MockEmbeddingProvider(model=model or "mock-embedding")
    if provider == "openai":
        from .embeddings_openai import OpenAIEmbeddingProvider

        _needs_key("OpenAI 兼容 Embedding", cfg.api_key, base_url, OPENAI_BASE)
        return OpenAIEmbeddingProvider(base_url or OPENAI_BASE, model, cfg.api_key, dimensions=cfg.dimensions)
    if provider == "dashscope":
        from .embeddings_dashscope import DashScopeEmbeddingProvider

        if not cfg.api_key:
            raise ProviderError("dashscope embedding 未配置 DashScope API Key，请先在设置页填写", provider=provider)
        return DashScopeEmbeddingProvider(
            base_url or DASHSCOPE_BASE, model, cfg.api_key, dimensions=cfg.dimensions
        )
    raise ProviderError(f"不支持的 Embedding provider：{provider}", provider=provider)


# ------------------------------------------------------------- connection testing
def _merged_config(db, kind: str, draft: dict[str, Any] | None) -> tuple[Any, float]:
    """Merge an unsaved draft over the stored config. Returns (config, temperature)."""
    full = load_ai_config(db)
    draft = draft if isinstance(draft, dict) else {}

    def pick(key: str, current: str) -> str:
        value = draft.get(key)
        return str(value).strip() if value is not None else current

    def pick_key(current: str) -> str:
        value = draft.get("api_key")
        if isinstance(value, str) and value.strip() and value != "__clear__":
            return value.strip()
        return current

    if kind == "asr":
        provider = pick("provider", full.asr.provider)
        base_url, model = _apply_defaults("asr", provider, pick("base_url", full.asr.base_url), pick("model", full.asr.model))
        diarization = draft.get("diarization", full.asr.diarization)
        language = pick("language", full.asr.language)
        return (
            ASRConfig(
                provider=provider,
                base_url=base_url,
                model=model,
                api_key=pick_key(full.asr.api_key),
                language=language,
                diarization=bool(diarization),
            ),
            0.3,
        )
    if kind == "realtime_asr":
        provider = pick("provider", full.realtime_asr.provider)
        base_url, model = _apply_defaults(
            "realtime_asr",
            provider,
            pick("base_url", full.realtime_asr.base_url),
            pick("model", full.realtime_asr.model),
        )
        return (
            RealtimeASRConfig(
                provider=provider,
                base_url=base_url,
                model=model,
                api_key=pick_key(full.realtime_asr.api_key),
                language=pick("language", full.realtime_asr.language),
            ),
            0.3,
        )
    if kind == "llm":
        provider = pick("provider", full.llm.provider)
        base_url, model = _apply_defaults("llm", provider, pick("base_url", full.llm.base_url), pick("model", full.llm.model))
        try:
            temperature = float(draft.get("temperature", full.llm.temperature))
        except (TypeError, ValueError):
            temperature = full.llm.temperature
        return (
            LLMConfig(
                provider=provider,
                base_url=base_url,
                model=model,
                api_key=pick_key(full.llm.api_key),
                temperature=temperature,
            ),
            temperature,
        )
    provider = pick("provider", full.embeddings.provider)
    base_url, model = _apply_defaults(
        "embeddings", provider, pick("base_url", full.embeddings.base_url), pick("model", full.embeddings.model)
    )
    try:
        dimensions = int(draft.get("dimensions", full.embeddings.dimensions))
    except (TypeError, ValueError):
        dimensions = full.embeddings.dimensions
    return (
        EmbeddingConfig(
            provider=provider,
            base_url=base_url,
            model=model,
            api_key=pick_key(full.embeddings.api_key),
            dimensions=dimensions,
        ),
        0.3,
    )


def _write_silence_wav(path: Path, seconds: float = 1.0) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * int(16000 * seconds))


async def test_provider(db, kind: str, draft: dict | None = None) -> dict:
    """Return ``{"ok": bool, "latency_ms": int|None, "message": str, "detail": ...}``."""
    started = time.perf_counter()
    kind = (kind or "").strip()
    if kind not in ("asr", "realtime_asr", "llm", "embeddings"):
        raise ProviderError(f"未知的 provider 类型：{kind}")
    try:
        cfg, temperature = _merged_config(db, kind, draft)
    except ProviderError as exc:
        return {"ok": False, "latency_ms": None, "message": exc.message}
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("failed to load provider config for test")
        return {"ok": False, "latency_ms": None, "message": f"读取配置失败：{type(exc).__name__}"}

    try:
        if kind == "realtime_asr":
            # Resolve auto/fallback against the saved file-ASR config, then reuse the
            # realtime check implementation (WebSocket handshake, no audio).
            full = load_ai_config(db)
            full.realtime_asr = cfg
            effective = effective_realtime_config(full)
            if effective.provider == "mock":
                return _ok(started, "演示模式：未调用外部实时转写服务", model=effective.model)
            realtime_cfg = ASRConfig(
                provider="dashscope_realtime",
                base_url=effective.base_url,
                model=effective.model,
                api_key=effective.api_key,
                language=effective.language,
                diarization=False,
            )
            provider = build_asr_provider(realtime_cfg)
            check = getattr(provider, "check", None)
            if callable(check):
                message = await asyncio.wait_for(check(), timeout=60)
                return _ok(started, str(message), model=effective.model)
            return _ok(started, "连接成功，实时转写服务可用", model=effective.model)
        if kind == "llm":
            provider = build_llm_provider(cfg)
            if cfg.provider == "mock":
                return _ok(started, "演示模式：未调用外部 LLM 服务", model=cfg.model)
            await asyncio.wait_for(
                provider.complete(
                    [{"role": "user", "content": "请只回复：OK"}],
                    temperature=0.0,
                    max_tokens=16,
                ),
                timeout=60,
            )
            return _ok(started, "连接成功，模型响应正常", model=cfg.model)
        if kind == "embeddings":
            provider = build_embedding_provider(cfg)
            if provider is None or cfg.provider == "none":
                return _ok(started, "未启用向量模型（使用字面检索）", model="none")
            vectors = await asyncio.wait_for(provider.embed(["ping"]), timeout=60)
            if not vectors or not vectors[0]:
                raise ProviderError("Embedding 服务未返回向量", provider=cfg.provider)
            return _ok(started, f"连接成功，向量维度 {len(vectors[0])}", model=cfg.model)
        # ASR
        provider = build_asr_provider(cfg)
        if cfg.provider == "mock":
            return _ok(started, "演示模式：未调用外部 ASR 服务", model=cfg.model)
        check = getattr(provider, "check", None)
        if callable(check):
            message = await asyncio.wait_for(check(), timeout=60)
            return _ok(started, str(message), model=cfg.model)
        with tempfile.TemporaryDirectory(prefix="echoic-asr-test-") as tmp:
            wav_path = Path(tmp) / "silence.wav"
            _write_silence_wav(wav_path)
            await asyncio.wait_for(
                provider.transcribe(wav_path, language=cfg.language, diarization=cfg.diarization),
                timeout=120,
            )
        return _ok(started, "连接成功，ASR 服务响应正常", model=cfg.model)
    except ProviderError as exc:
        return {"ok": False, "latency_ms": _elapsed(started), "message": exc.message}
    except asyncio.TimeoutError:
        return {"ok": False, "latency_ms": _elapsed(started), "message": "连接测试超时，请检查网络与服务商地址"}
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("provider connection test failed")
        return {
            "ok": False,
            "latency_ms": _elapsed(started),
            "message": f"连接测试失败（{type(exc).__name__}），请检查配置与网络",
        }


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _ok(started: float, message: str, *, model: str) -> dict:
    return {"ok": True, "latency_ms": _elapsed(started), "message": message, "model": model}


__all__ = [
    "build_asr_provider",
    "build_embedding_provider",
    "build_llm_provider",
    "test_provider",
]
