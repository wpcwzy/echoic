"""OpenAI compatible LLM provider (``POST /chat/completions``).

Works with OpenAI, DashScope compatible mode, DeepSeek, Ollama, vLLM,
LM Studio and similar gateways.  ``json_mode`` starts with
``response_format={"type":"json_object"}`` and retries once without it when the
gateway rejects the parameter with HTTP 400 (docs/PIPELINE.md §4.2).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx

from ..core.errors import ProviderError
from ._shared import (
    auth_headers,
    chat_completions_url,
    extract_openai_content,
    iter_openai_sse,
    json_or_error,
    request,
)
from .base import LLMProvider

logger = logging.getLogger(__name__)


class OpenAILLMProvider:
    name = "openai"

    def __init__(self, base_url: str, model: str, api_key: str = "", *, temperature: float = 0.3) -> None:
        self.base_url = base_url
        self.model = model or "qwen-plus"
        self.api_key = api_key
        self.temperature = temperature

    @property
    def url(self) -> str:
        return chat_completions_url(self.base_url)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **auth_headers(self.api_key)}
        return headers

    def _payload(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool,
        temperature: float,
        max_tokens: int,
        stream: bool = False,
    ) -> dict:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str:
        payload = self._payload(
            messages, json_mode=json_mode, temperature=temperature, max_tokens=max_tokens
        )
        async with httpx.AsyncClient() as client:
            response = await request(
                client, "POST", self.url, provider="openai-llm", timeout=300.0, headers=self._headers(), json=payload
            )
            if json_mode and response.status_code == 400:
                logger.info("LLM gateway rejected response_format json_object (HTTP 400), retrying without it")
                payload.pop("response_format", None)
                response = await request(
                    client,
                    "POST",
                    self.url,
                    provider="openai-llm",
                    timeout=300.0,
                    headers=self._headers(),
                    json=payload,
                )
        data = json_or_error(response, "openai-llm")
        content = extract_openai_content(data)
        if not content:
            raise ProviderError("LLM 返回内容为空，请检查模型配置", provider="openai-llm")
        return content

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        payload = self._payload(
            messages, json_mode=False, temperature=temperature, max_tokens=max_tokens, stream=True
        )
        async for piece in iter_openai_sse(
            "openai-llm", self.url, headers=self._headers(), payload=payload, timeout=600.0
        ):
            yield piece


__all__ = ["OpenAILLMProvider"]
