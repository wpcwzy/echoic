"""Provider registry.

backend-ai: implement the builder functions and ``test_provider`` below.
Every builder applies defaults for empty base_url/model and raises ProviderError
with a friendly Chinese message when configuration is incomplete.
"""

from __future__ import annotations

from ..core.errors import ProviderError
from ..services.settings_store import ASRConfig, EmbeddingConfig, LLMConfig
from .base import ASRProvider, EmbeddingProvider, LLMProvider


def build_asr_provider(cfg: ASRConfig) -> ASRProvider:
    raise ProviderError(f"ASR provider 尚未实现: {cfg.provider}")


def build_llm_provider(cfg: LLMConfig) -> LLMProvider:
    raise ProviderError(f"LLM provider 尚未实现: {cfg.provider}")


def build_embedding_provider(cfg: EmbeddingConfig) -> EmbeddingProvider | None:
    if cfg.provider == "none":
        return None
    raise ProviderError(f"Embedding provider 尚未实现: {cfg.provider}")


async def test_provider(db, kind: str, draft: dict | None = None) -> dict:
    """Return {"ok": bool, "latency_ms": int|None, "message": str, "detail": ...}."""
    raise ProviderError("连接测试尚未实现")
