"""OpenAI compatible embeddings provider (``POST /embeddings``)."""

from __future__ import annotations

import logging

import httpx

from ..core.errors import ProviderError
from ._shared import auth_headers, embeddings_url, json_or_error, request
from .base import EmbeddingProvider

logger = logging.getLogger(__name__)


class OpenAIEmbeddingProvider:
    name = "openai"

    def __init__(self, base_url: str, model: str, api_key: str, *, dimensions: int = 1024) -> None:
        self.base_url = base_url
        self.model = model or "text-embedding-3-small"
        self.api_key = api_key
        self.dim = int(dimensions or 1024)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload: dict = {"model": self.model, "input": texts}
        if self.dim:
            payload["dimensions"] = self.dim
        headers = {"Content-Type": "application/json", **auth_headers(self.api_key)}
        async with httpx.AsyncClient() as client:
            response = await request(
                client,
                "POST",
                embeddings_url(self.base_url),
                provider="openai-embeddings",
                timeout=120.0,
                headers=headers,
                json=payload,
            )
            if response.status_code == 400 and "dimensions" in payload:
                # Compatible gateways may reject `dimensions`; retry without it.
                logger.info("embeddings gateway rejected dimensions parameter, retrying without it")
                fallback = dict(payload)
                fallback.pop("dimensions", None)
                response = await request(
                    client,
                    "POST",
                    embeddings_url(self.base_url),
                    provider="openai-embeddings",
                    timeout=120.0,
                    headers=headers,
                    json=fallback,
                )
        data = json_or_error(response, "openai-embeddings")
        items = data.get("data") or []
        vectors: list[list[float]] = []
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("embedding"), list):
                vectors.append([float(value) for value in item["embedding"]])
        if len(vectors) != len(texts):
            raise ProviderError(
                f"Embedding 返回数量不匹配（期望 {len(texts)}，实际 {len(vectors)}）", provider="openai-embeddings"
            )
        if vectors:
            self.dim = len(vectors[0])
        return vectors


__all__ = ["OpenAIEmbeddingProvider"]
