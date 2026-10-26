"""Deterministic mock embedding provider (R-20 Phase 1, demo / tests only).

Produces a fixed **256-dim** hashing vector with no network access so the whole
semantic-search path can be demoed and unit-tested offline.  Properties:

* deterministic across processes — tokens are hashed with BLAKE2b, never with
  the randomized builtin ``hash()``;
* L2-normalized output, so cosine similarity is a plain dot product;
* tokenization: CJK runs contribute unigrams (weight 0.5) + bigrams (weight
  1.0), latin runs contribute lowercased word tokens (weight 1.0).  Shared
  bigrams/words therefore give related Chinese/English texts a higher cosine.

The model label defaults to ``mock-embedding`` (kept in sync with
``services.settings_store.DEFAULT_MODELS``); ``embeddings_model`` is written to
``chunks.embedding_model`` by the reindex job so stale detection works.
"""

from __future__ import annotations

import hashlib
import math
import re

DEFAULT_DIM = 256
DEFAULT_MODEL = "mock-embedding"

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_EN_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+.#_-]*")


def embedding_tokens(text: str) -> dict[str, float]:
    """Return ``{token: weight}`` used before hashing.

    Public (without the leading underscore) so tests can assert tokenization
    behaviour without depending on the private vector layout.
    """
    text = text or ""
    tokens: dict[str, float] = {}

    def _add(token: str, weight: float) -> None:
        tokens[token] = tokens.get(token, 0.0) + weight

    for run in _CJK_RUN_RE.findall(text):
        for char in run:
            _add(f"c1:{char}", 0.5)
        for index in range(len(run) - 1):
            _add(f"c2:{run[index:index + 2]}", 1.0)

    for token in _EN_TOKEN_RE.findall(text):
        lowered = token.lower()
        _add(f"w:{lowered}", 1.0)
        # short sub-word pieces make misspelled/compound latin queries a bit
        # more forgiving without hurting determinism.
        if len(lowered) >= 5:
            for index in range(len(lowered) - 2):
                _add(f"t3:{lowered[index:index + 3]}", 0.25)

    return tokens


def _bucket(token: str, dim: int) -> tuple[int, float]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    index = int.from_bytes(digest[:4], "little") % dim
    sign = 1.0 if digest[4] & 1 else -1.0
    return index, sign


class MockEmbeddingProvider:
    """Deterministic hash-embedding provider (``provider="mock"``)."""

    name = "mock"

    def __init__(self, model: str = DEFAULT_MODEL, *, dim: int = DEFAULT_DIM) -> None:
        self.model = model or DEFAULT_MODEL
        self.dim = max(8, int(dim or DEFAULT_DIM))

    def embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token, weight in embedding_tokens(text).items():
            index, sign = _bucket(token, self.dim)
            vector[index] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0:
            return [value / norm for value in vector]
        return vector

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(text or "") for text in texts]

    async def embed_query(self, texts: list[str]) -> list[list[float]]:
        # Symmetric hashing provider: queries and documents use the same space.
        return await self.embed(texts)


__all__ = ["MockEmbeddingProvider", "embedding_tokens", "DEFAULT_DIM", "DEFAULT_MODEL"]
