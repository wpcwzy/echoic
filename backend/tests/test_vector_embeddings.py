"""R-20 mock embedding provider + registry tests (no network)."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.errors import ProviderError
from app.providers.embeddings_mock import (
    DEFAULT_DIM,
    MockEmbeddingProvider,
    embedding_tokens,
)
from app.providers.registry import build_embedding_provider
from app.services.settings_store import EmbeddingConfig

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _embed_blocking(text: str) -> list[float]:
    import asyncio

    provider = MockEmbeddingProvider()
    return asyncio.run(provider.embed([text]))[0]


def test_mock_provider_shape_and_determinism():
    provider = MockEmbeddingProvider()
    assert provider.name == "mock"
    assert provider.model == "mock-embedding"
    assert provider.dim == DEFAULT_DIM == 256

    vectors = _embed_blocking("两周摘要")
    assert len(vectors) == 256
    norm = math.sqrt(sum(value * value for value in vectors))
    assert abs(norm - 1.0) < 1e-6

    # identical text → identical vector; different text → different vector
    assert _embed_blocking("两周摘要") == vectors
    assert _embed_blocking("今天的天气很好") != vectors


def test_mock_provider_semantic_similarity():
    query = _embed_blocking("两周摘要")
    related = _embed_blocking("摘要部分预计还有两周时间完成并交付。")
    unrelated = _embed_blocking("今天天气很好，下午我们一起去公园散步。")

    assert _cosine(query, related) > 0.2
    assert _cosine(query, unrelated) < _cosine(query, related)


def test_embedding_tokens_cjk_bigrams_and_latin_words():
    tokens = embedding_tokens("Graph500 基准测试")
    assert "c2:基准" in tokens
    assert "c1:基" in tokens
    assert "w:graph500" in tokens
    # bigrams weigh more than single characters
    assert tokens["c2:基准"] > tokens["c1:基"]

    latin = embedding_tokens("Text-embedding v3")
    assert "w:text-embedding" in latin
    assert "w:v3" in latin


def test_empty_text_embeds_to_zero_vector_without_error():
    vectors = _embed_blocking("")
    assert len(vectors) == 256
    assert all(value == 0.0 for value in vectors)


def test_mock_embedding_stable_across_processes():
    code = (
        "import asyncio, json\n"
        "from app.providers.embeddings_mock import MockEmbeddingProvider\n"
        "print(json.dumps(asyncio.run(MockEmbeddingProvider().embed(['跨进程稳定性']))[0]))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    remote = json.loads(result.stdout.strip())
    local = _embed_blocking("跨进程稳定性")
    assert len(remote) == len(local) == 256
    assert all(abs(a - b) < 1e-9 for a, b in zip(remote, local))


def test_registry_builds_mock_embedding_provider():
    provider = build_embedding_provider(
        EmbeddingConfig(provider="mock", base_url="", model="", api_key="", dimensions=1024)
    )
    assert isinstance(provider, MockEmbeddingProvider)
    assert provider.dim == 256
    # registry applies DEFAULT_MODELS["embeddings"]["mock"]
    assert provider.model == "mock-embedding"


def test_registry_none_and_unknown_provider():
    assert build_embedding_provider(EmbeddingConfig(provider="none")) is None
    assert build_embedding_provider(EmbeddingConfig(provider="")) is None
    with pytest.raises(ProviderError):
        build_embedding_provider(EmbeddingConfig(provider="bogus"))
