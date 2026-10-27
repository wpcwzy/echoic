from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..core.crypto import decrypt_secret, encrypt_secret, mask_secret
from ..core.errors import bad_request
from ..models import AppSetting
from .prompts import DEFAULT_PROMPTS

logger = logging.getLogger(__name__)

ALLOWED_PROVIDERS: dict[str, set[str]] = {
    "asr": {"mock", "dashscope_realtime", "dashscope_file", "openai"},
    # v0.3.5: realtime ASR is configured independently from the file/upload ASR.
    # "auto" reuses the file ASR DashScope key/base when realtime.api_key is empty.
    "realtime_asr": {"auto", "mock", "dashscope_realtime"},
    "llm": {"mock", "openai"},
    "embeddings": {"none", "openai", "dashscope", "mock"},
}

DEFAULT_BASE_URLS: dict[str, dict[str, str]] = {
    "asr": {
        "dashscope_realtime": "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
        "dashscope_file": "https://dashscope.aliyuncs.com",
        "openai": "https://api.openai.com/v1",
    },
    "realtime_asr": {
        "auto": "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
        "dashscope_realtime": "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
        "mock": "",
    },
    "llm": {"openai": "https://api.openai.com/v1"},
    "embeddings": {
        "openai": "https://api.openai.com/v1",
        "dashscope": "https://dashscope.aliyuncs.com",
    },
}

DEFAULT_MODELS: dict[str, dict[str, str]] = {
    "asr": {
        "dashscope_realtime": "paraformer-realtime-v2",
        "dashscope_file": "paraformer-v2",
        "openai": "whisper-1",
    },
    "realtime_asr": {
        "auto": "paraformer-realtime-v2",
        "dashscope_realtime": "paraformer-realtime-v2",
        "mock": "mock-realtime",
    },
    "llm": {"openai": "qwen-plus"},
    "embeddings": {
        "openai": "text-embedding-3-small",
        "dashscope": "text-embedding-v3",
        "mock": "mock-embedding",
    },
}

SETTING_KEYS = ("ai.asr", "ai.realtime_asr", "ai.llm", "ai.embeddings", "prompts", "system")


@dataclass
class ASRConfig:
    provider: str = "mock"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    language: str = "zh"
    diarization: bool = True


@dataclass
class RealtimeASRConfig:
    provider: str = "auto"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    language: str = "zh"


@dataclass
class LLMConfig:
    provider: str = "mock"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float = 0.3


@dataclass
class EmbeddingConfig:
    provider: str = "none"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    dimensions: int = 1024


@dataclass
class AIConfig:
    asr: ASRConfig = field(default_factory=ASRConfig)
    realtime_asr: RealtimeASRConfig = field(default_factory=RealtimeASRConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    embeddings: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    prompts: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PROMPTS))
    system: dict[str, Any] = field(default_factory=dict)

    @property
    def demo_mode(self) -> bool:
        return self.asr.provider == "mock" or self.llm.provider == "mock"


def _env_defaults() -> dict[str, dict[str, Any]]:
    return {
        "ai.asr": {
            "provider": settings.asr_provider,
            "base_url": settings.asr_base_url,
            "model": settings.asr_model,
            "api_key_enc": encrypt_secret(settings.asr_api_key),
            "language": settings.asr_language,
            "diarization": settings.asr_diarization,
        },
        "ai.realtime_asr": {
            "provider": settings.realtime_asr_provider,
            "base_url": settings.realtime_asr_base_url,
            "model": settings.realtime_asr_model,
            "api_key_enc": encrypt_secret(settings.realtime_asr_api_key),
            "language": settings.realtime_asr_language,
        },
        "ai.llm": {
            "provider": settings.llm_provider,
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "api_key_enc": encrypt_secret(settings.llm_api_key),
            "temperature": settings.llm_temperature,
        },
        "ai.embeddings": {
            "provider": settings.embeddings_provider,
            "base_url": settings.embeddings_base_url,
            "model": settings.embeddings_model,
            "api_key_enc": encrypt_secret(settings.embeddings_api_key),
            "dimensions": settings.embeddings_dimensions,
        },
        "prompts": {},
        "system": {
            "auto_process": True,
            "default_language": "zh",
            "max_upload_mb": settings.max_upload_mb,
            "allow_registration": settings.allow_registration,
        },
    }


def _get_row(db, key: str) -> AppSetting | None:
    return db.get(AppSetting, key)


def _get_value(db, key: str) -> dict[str, Any]:
    row = _get_row(db, key)
    if row is None:
        return {}
    import json

    try:
        data = json.loads(row.value_json or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _set_value(db, key: str, value: dict[str, Any]) -> None:
    import json

    row = _get_row(db, key)
    text = json.dumps(value, ensure_ascii=False)
    if row is None:
        db.add(AppSetting(key=key, value_json=text))
    else:
        row.value_json = text
    db.commit()


def ensure_default_settings(db) -> None:
    defaults = _env_defaults()
    for key in SETTING_KEYS:
        if _get_row(db, key) is None:
            _set_value(db, key, defaults[key])
            logger.info("initialized setting %s", key)


def _apply_provider_defaults(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    if kind == "embeddings":
        fallback = "none"
    elif kind == "realtime_asr":
        fallback = "auto"
    else:
        fallback = "mock"
    provider = data.get("provider") or fallback
    if not data.get("base_url"):
        data["base_url"] = DEFAULT_BASE_URLS.get(kind, {}).get(provider, "")
    if not data.get("model"):
        data["model"] = DEFAULT_MODELS.get(kind, {}).get(provider, "")
    return data


def load_ai_config(db) -> AIConfig:
    ensure_default_settings(db)
    asr_raw = _apply_provider_defaults("asr", dict(_get_value(db, "ai.asr")))
    realtime_raw = _apply_provider_defaults("realtime_asr", dict(_get_value(db, "ai.realtime_asr")))
    llm_raw = _apply_provider_defaults("llm", dict(_get_value(db, "ai.llm")))
    emb_raw = _apply_provider_defaults("embeddings", dict(_get_value(db, "ai.embeddings")))
    system_raw = dict(_get_value(db, "system"))
    prompts_raw = _get_value(db, "prompts")

    asr = ASRConfig(
        provider=asr_raw.get("provider", "mock"),
        base_url=asr_raw.get("base_url", ""),
        model=asr_raw.get("model", ""),
        api_key=decrypt_secret(asr_raw.get("api_key_enc", "")),
        language=asr_raw.get("language") or "zh",
        diarization=bool(asr_raw.get("diarization", True)),
    )
    realtime_asr = RealtimeASRConfig(
        provider=realtime_raw.get("provider", "auto"),
        base_url=realtime_raw.get("base_url", ""),
        model=realtime_raw.get("model", ""),
        api_key=decrypt_secret(realtime_raw.get("api_key_enc", "")),
        language=realtime_raw.get("language") or "zh",
    )
    llm = LLMConfig(
        provider=llm_raw.get("provider", "mock"),
        base_url=llm_raw.get("base_url", ""),
        model=llm_raw.get("model", ""),
        api_key=decrypt_secret(llm_raw.get("api_key_enc", "")),
        temperature=float(llm_raw.get("temperature", 0.3) or 0.3),
    )
    embeddings = EmbeddingConfig(
        provider=emb_raw.get("provider", "none"),
        base_url=emb_raw.get("base_url", ""),
        model=emb_raw.get("model", ""),
        api_key=decrypt_secret(emb_raw.get("api_key_enc", "")),
        dimensions=int(emb_raw.get("dimensions", 1024) or 1024),
    )
    prompts = dict(DEFAULT_PROMPTS)
    for key, value in prompts_raw.items():
        if key in DEFAULT_PROMPTS and isinstance(value, str) and value.strip():
            prompts[key] = value
    system = {
        "auto_process": bool(system_raw.get("auto_process", True)),
        "default_language": system_raw.get("default_language") or "zh",
        "max_upload_mb": int(system_raw.get("max_upload_mb", settings.max_upload_mb) or settings.max_upload_mb),
        "allow_registration": bool(system_raw.get("allow_registration", settings.allow_registration)),
    }
    return AIConfig(
        asr=asr,
        realtime_asr=realtime_asr,
        llm=llm,
        embeddings=embeddings,
        prompts=prompts,
        system=system,
    )


def _provider_out(kind: str, raw: dict[str, Any]) -> dict[str, Any]:
    api_key = decrypt_secret(raw.get("api_key_enc", ""))
    provider = raw.get("provider") or ("none" if kind == "embeddings" else "mock")
    configured = provider != "none" and (provider == "mock" or bool(api_key) or bool(raw.get("base_url")))
    data: dict[str, Any] = {
        "provider": provider,
        "base_url": raw.get("base_url", ""),
        "model": raw.get("model", ""),
        "has_api_key": bool(api_key),
        "api_key_masked": mask_secret(api_key),
        "configured": configured,
    }
    if kind in ("asr", "realtime_asr"):
        data["language"] = raw.get("language") or "zh"
    if kind == "asr":
        data["diarization"] = bool(raw.get("diarization", True))
    if kind == "llm":
        data["temperature"] = float(raw.get("temperature", 0.3) or 0.3)
    if kind == "embeddings":
        data["dimensions"] = int(raw.get("dimensions", 1024) or 1024)
    return data


def masked_settings(db) -> dict[str, Any]:
    ensure_default_settings(db)
    cfg = load_ai_config(db)
    prompts_overrides = _get_value(db, "prompts")
    from .queue import queue_depth
    from ..worker import worker_status

    system = _get_value(db, "system")
    worker = worker_status()
    return {
        "ai": {
            "asr": _provider_out("asr", _get_value(db, "ai.asr")),
            "realtime_asr": _provider_out("realtime_asr", _get_value(db, "ai.realtime_asr")),
            "llm": _provider_out("llm", _get_value(db, "ai.llm")),
            "embeddings": _provider_out("embeddings", _get_value(db, "ai.embeddings")),
        },
        "prompts": {k: v for k, v in cfg.prompts.items()},
        "prompt_overrides": sorted(
            k for k, v in prompts_overrides.items() if k in DEFAULT_PROMPTS and isinstance(v, str) and v.strip()
        ),
        "system": {
            "demo_mode": cfg.demo_mode,
            "auto_process": bool(system.get("auto_process", True)),
            "max_upload_mb": int(system.get("max_upload_mb", settings.max_upload_mb)),
            "default_language": system.get("default_language", "zh"),
            "allow_registration": bool(system.get("allow_registration", settings.allow_registration)),
            "consume": {
                "enabled": bool(settings.consume_enabled),
                "dir": str(settings.consume_path),
                "interval_sec": int(settings.consume_interval_sec),
            },
            "live_enabled": bool(settings.live_enabled),
            "live_merge_mode": settings.live_merge_mode,
            "live_max_minutes": int(settings.live_max_minutes or 60),
            "version": settings.version,
            "worker": {
                "embedded": settings.embedded_worker,
                "running": bool(worker.get("running")),
                "queue_depth": queue_depth_safe(db),
            },
        },
    }


def queue_depth_safe(db) -> int:
    try:
        from .queue import queue_depth

        return queue_depth(db)
    except Exception:  # pragma: no cover - defensive during early bootstrap
        return 0


def _validate_base_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://", "ws://", "wss://")):
        raise bad_request("base_url 必须以 http(s):// 或 ws(s):// 开头")
    if len(value) > 500:
        raise bad_request("base_url 过长")
    return value.rstrip("/")


def _merge_provider(
    kind: str,
    current: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise bad_request(f"{kind} 配置格式错误")
    provider = patch.get("provider", current.get("provider"))
    if provider not in ALLOWED_PROVIDERS[kind]:
        raise bad_request(f"不支持的 {kind} provider：{provider}")
    merged = dict(current)
    merged["provider"] = provider
    if "base_url" in patch:
        merged["base_url"] = _validate_base_url(str(patch.get("base_url") or ""))
    if "model" in patch:
        merged["model"] = str(patch.get("model") or "")[:120]
    if kind in ("asr", "realtime_asr"):
        if "language" in patch:
            merged["language"] = str(patch.get("language") or "zh")[:16]
    if kind == "asr" and "diarization" in patch:
        merged["diarization"] = bool(patch["diarization"])
    if kind == "llm" and "temperature" in patch:
        try:
            temperature = float(patch["temperature"])
        except (TypeError, ValueError):
            raise bad_request("temperature 必须是数字")
        merged["temperature"] = min(2.0, max(0.0, temperature))
    if kind == "embeddings" and "dimensions" in patch:
        try:
            dimensions = int(patch["dimensions"])
        except (TypeError, ValueError):
            raise bad_request("dimensions 必须是整数")
        if not 16 <= dimensions <= 8192:
            raise bad_request("dimensions 必须在 16-8192 之间")
        merged["dimensions"] = dimensions
    api_key = patch.get("api_key", None)
    if api_key == "__clear__":
        merged["api_key_enc"] = ""
    elif isinstance(api_key, str) and api_key.strip():
        merged["api_key_enc"] = encrypt_secret(api_key.strip())
    merged = _apply_provider_defaults(kind, merged)
    return merged


def update_settings(db, payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise bad_request("请求体格式错误")
    ai = payload.get("ai") or {}
    if ai:
        for kind in ("asr", "realtime_asr", "llm", "embeddings"):
            patch = ai.get(kind)
            if not patch:
                continue
            if patch.get("provider") == "__clear__":
                continue
            current = _get_value(db, f"ai.{kind}")
            merged = _merge_provider(kind, current, patch)
            _set_value(db, f"ai.{kind}", merged)
    prompts = payload.get("prompts")
    if prompts is not None:
        if not isinstance(prompts, dict):
            raise bad_request("prompts 格式错误")
        overrides = _get_value(db, "prompts")
        for key, value in prompts.items():
            if key not in DEFAULT_PROMPTS:
                raise bad_request(f"未知提示词：{key}")
            if not isinstance(value, str):
                raise bad_request("提示词必须是字符串")
            if len(value) > 12000:
                raise bad_request("提示词过长（最多 12000 字符）")
            if value.strip() == "" or value == DEFAULT_PROMPTS[key]:
                overrides.pop(key, None)
            else:
                overrides[key] = value
        _set_value(db, "prompts", overrides)
    system = payload.get("system")
    if system is not None:
        if not isinstance(system, dict):
            raise bad_request("system 配置格式错误")
        current = _get_value(db, "system")
        merged = {**_env_defaults()["system"], **current}
        if "auto_process" in system:
            merged["auto_process"] = bool(system["auto_process"])
        if "default_language" in system:
            merged["default_language"] = str(system["default_language"] or "zh")[:16]
        if "max_upload_mb" in system:
            try:
                limit = int(system["max_upload_mb"])
            except (TypeError, ValueError):
                raise bad_request("max_upload_mb 必须是整数")
            if not 1 <= limit <= 10240:
                raise bad_request("max_upload_mb 必须在 1-10240 之间")
            merged["max_upload_mb"] = limit
        if "allow_registration" in system:
            merged["allow_registration"] = bool(system["allow_registration"])
        _set_value(db, "system", merged)


def reset_prompts(db) -> None:
    _set_value(db, "prompts", {})


def current_prompts(db) -> dict[str, str]:
    return load_ai_config(db).prompts


def effective_realtime_config(cfg: AIConfig) -> RealtimeASRConfig:
    """Resolve ``ai.realtime_asr`` including ``auto`` fallback to the file ASR.

    ``auto`` (default) uses DashScope realtime when either the realtime slot or
    the file-transcription slot has a DashScope API key; otherwise mock. An
    explicitly configured realtime provider wins, and an empty realtime key
    falls back to the file ASR key/base (same Bailian account is the common case).
    """
    realtime = cfg.realtime_asr
    provider = (realtime.provider or "auto").strip() or "auto"
    file_dashscope = cfg.asr.provider in ("dashscope_file", "dashscope_realtime")
    if provider == "auto":
        # A DashScope file-ASR slot implies the same Bailian account should power
        # realtime; a missing key is reported later at session creation, not here.
        use_dashscope = bool(realtime.api_key) or file_dashscope
        provider = "dashscope_realtime" if use_dashscope else "mock"
    if provider not in ("dashscope_realtime", "mock"):
        provider = "mock"
    api_key = realtime.api_key
    base_url = realtime.base_url
    if provider == "dashscope_realtime" and not api_key and file_dashscope:
        api_key = cfg.asr.api_key
        if not base_url or "/compatible-mode" in base_url:
            base_url = cfg.asr.base_url or base_url
    if provider == "dashscope_realtime" and base_url and "/compatible-mode" in base_url:
        base_url = ""
    model = realtime.model or DEFAULT_MODELS["realtime_asr"].get(provider, "paraformer-realtime-v2")
    return RealtimeASRConfig(
        provider=provider,
        base_url=base_url,
        model=model or "paraformer-realtime-v2",
        api_key=api_key,
        language=realtime.language or cfg.asr.language or "zh",
    )


__all__ = [
    "RealtimeASRConfig",
    "effective_realtime_config",
    "ASRConfig",
    "LLMConfig",
    "EmbeddingConfig",
    "AIConfig",
    "ALLOWED_PROVIDERS",
    "DEFAULT_PROMPTS",
    "ensure_default_settings",
    "load_ai_config",
    "masked_settings",
    "update_settings",
    "reset_prompts",
    "current_prompts",
]
