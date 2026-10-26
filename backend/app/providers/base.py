from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..core.errors import ProviderError

__all__ = [
    "ASRSegment",
    "ASRResult",
    "ASRProvider",
    "LLMProvider",
    "EmbeddingProvider",
    "ProviderError",
]


@dataclass
class ASRSegment:
    start_ms: int
    end_ms: int
    text: str
    speaker_label: str | None = None
    confidence: float | None = None


@dataclass
class ASRResult:
    text: str
    segments: list[ASRSegment]
    speakers: list[str]
    language: str
    provider: str
    model: str
    raw: dict | None = field(default=None, repr=False)


@runtime_checkable
class ASRProvider(Protocol):
    name: str

    async def transcribe(
        self,
        audio_path: Path,
        *,
        language: str = "zh",
        diarization: bool = True,
        progress: Callable[[int], None] | None = None,
    ) -> ASRResult: ...


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str: ...

    def stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...
