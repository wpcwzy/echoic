"""Shared HTTP/SSE helpers for the LLM/ASR/Embedding providers.

Rules (docs/PIPELINE.md §4):
* Retry only on 429/5xx/network timeouts, at most 2 extra attempts, backoff 2s/5s.
* Never log or embed API keys in error messages; redact anything that looks like one.
* All failures raise ``ProviderError`` with a user friendly (Chinese) message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..core.errors import ProviderError

logger = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_DELAYS = (2.0, 5.0)
_KEY_PATTERN = re.compile(r"(sk-[A-Za-z0-9_\-]{6,})")
# Anything that may look like an api key echoed back in an error body.
_SECRETISH = re.compile(r"(?i)(api[_-]?key|authorization|bearer)\s*[:=]?\s*\S+")


def redact(text: str, limit: int = 300) -> str:
    """Shorten a provider response snippet and strip credential-looking content."""
    text = _KEY_PATTERN.sub("sk-****", text or "")
    text = _SECRETISH.sub(lambda m: m.group(1) + " ****", text)
    text = " ".join(text.split())
    return text[:limit]


def _error_message(provider: str, status_code: int, body: str) -> str:
    snippet = redact(body)
    hint = ""
    if status_code in (401, 403):
        hint = "，请检查 API Key 是否正确/是否有该模型权限"
    elif status_code == 404:
        hint = "，请检查 base_url 与模型名称"
    elif status_code == 429:
        hint = "，请求过于频繁或额度不足，请稍后重试"
    elif status_code >= 500:
        hint = "，服务商暂时不可用，请稍后重试"
    return f"{provider} 调用失败（HTTP {status_code}{hint}）：{snippet or '无返回内容'}"


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    provider: str,
    timeout: float = 120.0,
    **kwargs: Any,
) -> httpx.Response:
    """Perform an HTTP request with the pipeline retry policy, raising ProviderError."""
    last_error: str | None = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            response = await client.request(method, url, timeout=timeout, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = f"{provider} 网络请求失败：{type(exc).__name__}"
            logger.warning("%s network error on %s (attempt %s): %s", provider, url, attempt + 1, type(exc).__name__)
        else:
            if response.status_code in RETRY_STATUSES and attempt < len(RETRY_DELAYS):
                logger.warning(
                    "%s returned HTTP %s on %s (attempt %s), retrying",
                    provider,
                    response.status_code,
                    redact(url),
                    attempt + 1,
                )
                await asyncio.sleep(RETRY_DELAYS[attempt])
                continue
            return response
        if attempt < len(RETRY_DELAYS):
            await asyncio.sleep(RETRY_DELAYS[attempt])
    raise ProviderError(last_error or f"{provider} 请求失败", provider=provider)


def ensure_success(response: httpx.Response, provider: str) -> httpx.Response:
    if response.status_code >= 400:
        raise ProviderError(
            _error_message(provider, response.status_code, response.text),
            provider=provider,
            status_code=response.status_code,
        )
    return response


def json_or_error(response: httpx.Response, provider: str) -> dict:
    ensure_success(response, provider)
    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError):
        raise ProviderError(f"{provider} 返回内容不是合法 JSON：{redact(response.text)}", provider=provider)
    if not isinstance(data, dict):
        raise ProviderError(f"{provider} 返回结构异常", provider=provider)
    return data


async def post_json(
    provider: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any],
    timeout: float = 120.0,
    client: httpx.AsyncClient | None = None,
) -> dict:
    if client is None:
        async with httpx.AsyncClient() as own_client:
            response = await request(own_client, "POST", url, provider=provider, timeout=timeout, headers=headers, json=payload)
    else:
        response = await request(client, "POST", url, provider=provider, timeout=timeout, headers=headers, json=payload)
    return json_or_error(response, provider)


def auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def api_url(base_url: str, path: str) -> str:
    """Join a base URL (with or without trailing /v1) and an API path."""
    base = (base_url or "").rstrip("/")
    path = path.lstrip("/")
    return f"{base}/{path}"


def add_openai_v1(base_url: str) -> str:
    """Accept ``https://host`` as well as ``https://host/v1`` (and DashScope compatible mode)."""
    base = (base_url or "").rstrip("/")
    if not base:
        return base
    if base.endswith("/v1") or base.endswith("/compatible-mode/v1"):
        return base
    if base.endswith("/compatible-mode"):
        return base + "/v1"
    return base + "/v1"


def chat_completions_url(base_url: str) -> str:
    return add_openai_v1(base_url) + "/chat/completions"


def embeddings_url(base_url: str) -> str:
    return add_openai_v1(base_url) + "/embeddings"


def audio_transcriptions_url(base_url: str) -> str:
    return add_openai_v1(base_url) + "/audio/transcriptions"


def extract_openai_content(data: dict) -> str:
    """Pull ``choices[0].message.content`` tolerating common OpenAI-compatible shapes."""
    choices = data.get("choices") or []
    if not choices:
        raise ProviderError("LLM 返回结果为空（缺少 choices）", provider="openai")
    choice = choices[0] or {}
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # some compatible gateways return content parts
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in ("text", "output_text"):
                parts.append(str(part.get("text") or ""))
        content = "".join(parts)
    if not content:
        # reasoning models may put everything into reasoning_content
        content = message.get("reasoning_content") or choice.get("text") or ""
    return str(content or "")


async def iter_openai_sse(
    provider: str,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float = 300.0,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[str]:
    """Yield text deltas from an OpenAI-compatible SSE chat completion stream."""
    own_client = client is None
    http = httpx.AsyncClient() if own_client else client
    try:
        try:
            async with http.stream("POST", url, headers=headers, json=payload, timeout=timeout) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise ProviderError(
                        _error_message(provider, response.status_code, body),
                        provider=provider,
                        status_code=response.status_code,
                    )
                async for line in response.aiter_lines():
                    line = (line or "").strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_text = line[5:].strip()
                    if data_text in ("[DONE]", "DONE"):
                        return
                    try:
                        event = json.loads(data_text)
                    except ValueError:
                        continue
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0] or {}).get("delta") or {}
                    piece = delta.get("content")
                    if isinstance(piece, list):
                        piece = "".join(
                            str(p.get("text") or "") for p in piece if isinstance(p, dict)
                        )
                    if piece:
                        yield str(piece)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderError(f"{provider} 流式请求失败：{type(exc).__name__}", provider=provider) from exc
    finally:
        if own_client:
            await http.aclose()


def parse_json_strict(text: str) -> Any | None:
    """Best-effort JSON extraction from an LLM answer (single module local helper)."""
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate).strip()
    for attempt in (candidate, None):
        if attempt is None:
            start = candidate.find("{")
            end = candidate.rfind("}")
            if start == -1 or end <= start:
                return None
            attempt = candidate[start : end + 1]
        try:
            return json.loads(attempt)
        except (ValueError, TypeError):
            continue
    return None


__all__ = [
    "add_openai_v1",
    "api_url",
    "audio_transcriptions_url",
    "auth_headers",
    "chat_completions_url",
    "embeddings_url",
    "ensure_success",
    "extract_openai_content",
    "iter_openai_sse",
    "json_or_error",
    "parse_json_strict",
    "post_json",
    "redact",
    "request",
]
