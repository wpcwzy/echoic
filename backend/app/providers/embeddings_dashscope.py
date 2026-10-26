"""DashScope native text embedding provider.

``POST {base}/api/v1/services/embeddings/text-embedding/text-embedding`` with at
most 10 texts per batch and a short delay between batches (docs/research/
dashscope-api.md §5.1).  ``text_type`` is set separately for query/document so
retrieval quality matches DashScope guidance.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..core.errors import ProviderError
from ._shared import api_url, auth_headers, json_or_error, request
from .base import EmbeddingProvider

logger = logging.getLogger(__name__)

BATCH_SIZE = 10
BATCH_DELAY_S = 0.25


class DashScopeEmbeddingProvider:
    name = "dashscope"

    def __init__(self, base_url: str, model: str, api_key: str, *, dimensions: int = 1024) -> None:
        self.base_url = (base_url or "https://dashscope.aliyuncs.com").rstrip("/")
        self.model = model or "text-embedding-v3"
        self.api_key = api_key
        self.dim = int(dimensions or 1024)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, text_type="document")

    async def embed_query(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, text_type="query")

    async def _embed(self, texts: list[str], *, text_type: str) -> list[list[float]]:
        if not texts:
            return []
        url = api_url(self.base_url, "/api/v1/services/embeddings/text-embedding/text-embedding")
        headers = {"Content-Type": "application/json", **auth_headers(self.api_key)}
        vectors: list[list[float]] = []
        async with httpx.AsyncClient() as client:
            for offset in range(0, len(texts), BATCH_SIZE):
                batch = [text or " " for text in texts[offset : offset + BATCH_SIZE]]
                payload = {
                    "model": self.model,
                    "input": {"texts": batch},
                    "parameters": {"dimension": self.dim, "text_type": text_type, "output_type": "dense"},
                }
                response = await request(
                    client,
                    "POST",
                    url,
                    provider="dashscope-embeddings",
                    timeout=120.0,
                    headers=headers,
                    json=payload,
                )
                if response.status_code == 400:
                    # Older models reject text_type/output_type; retry minimal payload.
                    logger.info("dashscope embeddings rejected optional parameters, retrying minimal payload")
                    payload = {
                        "model": self.model,
                        "input": {"texts": batch},
                        "parameters": {"dimension": self.dim},
                    }
                    response = await request(
                        client,
                        "POST",
                        url,
                        provider="dashscope-embeddings",
                        timeout=120.0,
                        headers=headers,
                        json=payload,
                    )
                data = json_or_error(response, "dashscope-embeddings")
                output = data.get("output") or {}
                items = output.get("embeddings") or output.get("embedding") or []
                for item in items:
                    if isinstance(item, dict) and isinstance(item.get("embedding"), list):
                        vectors.append([float(value) for value in item["embedding"]])
                if offset + BATCH_SIZE < len(texts):
                    await asyncio.sleep(BATCH_DELAY_S)
        if len(vectors) != len(texts):
            raise ProviderError(
                f"DashScope embedding 返回数量不匹配（期望 {len(texts)}，实际 {len(vectors)}）",
                provider="dashscope-embeddings",
            )
        if vectors:
            self.dim = len(vectors[0])
        return vectors


__all__ = ["DashScopeEmbeddingProvider"]
