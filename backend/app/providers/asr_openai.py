"""OpenAI Whisper compatible ASR (``POST /v1/audio/transcriptions``).

Works with OpenAI, faster-whisper-server, FunASR OpenAI-compatible servers and
similar.  Uses ``verbose_json`` with segment timestamps; tolerates servers that
do not support ``timestamp_granularities[]`` (retries without it), servers that
return ``diarized_json`` style ``speaker`` fields, and plain ``json`` responses
(falls back to duration-based synthetic segments).
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

import httpx

from ..core import media
from ..core.errors import ProviderError
from ._shared import audio_transcriptions_url, auth_headers, json_or_error, request
from .base import ASRResult, ASRSegment

logger = logging.getLogger(__name__)

_EXT_CONTENT_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
}


def _content_type(path: Path) -> str:
    return _EXT_CONTENT_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _to_ms(value: object) -> int:
    try:
        return max(0, int(round(float(value) * 1000)))
    except (TypeError, ValueError):
        return 0


def _parse_segments(data: dict, duration_ms: int) -> tuple[list[ASRSegment], list[str]]:
    """Convert verbose_json / diarized_json / json payloads to ASRSegments."""
    raw_segments = data.get("segments") or []
    segments: list[ASRSegment] = []
    labels: list[str] = []
    for index, raw in enumerate(raw_segments):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        speaker = raw.get("speaker")
        speaker_label = None
        if speaker is not None and str(speaker).strip():
            speaker_label = f"说话人{str(speaker).strip()}"
        start = _to_ms(raw.get("start"))
        end = _to_ms(raw.get("end"))
        if end <= start:
            end = start + 1000
        confidence = raw.get("avg_logprob")
        try:
            confidence = max(0.0, min(1.0, 1.0 + float(confidence))) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None
        segments.append(
            ASRSegment(
                start_ms=start,
                end_ms=end,
                text=text,
                speaker_label=speaker_label,
                confidence=confidence,
            )
        )
        if speaker_label and speaker_label not in labels:
            labels.append(speaker_label)

    if not segments and data.get("text"):
        # Server ignored verbose_json / timestamps: split the full text into
        # sentence-ish pieces spread over the media duration.
        full_text = str(data.get("text") or "").strip()
        pieces = [piece for piece in __import__("re").split(r"(?<=[。！？!?；;])", full_text) if piece.strip()]
        if not pieces:
            pieces = [full_text]
        total = duration_ms or max(1000, len(pieces) * 4000)
        slot = max(500, total // len(pieces))
        for index, piece in enumerate(pieces):
            segments.append(
                ASRSegment(
                    start_ms=min(total, index * slot),
                    end_ms=min(total, (index + 1) * slot) or (index + 1) * slot,
                    text=piece.strip(),
                    speaker_label=None,
                )
            )

    if not segments:
        raise ProviderError("ASR 服务未返回任何转录内容，请检查音频是否包含语音", provider="openai-asr")

    if not labels:
        labels = ["说话人1"]
        for segment in segments:
            segment.speaker_label = "说话人1"
    else:
        # Segments without a speaker label inherit the first discovered speaker.
        for segment in segments:
            if not segment.speaker_label:
                segment.speaker_label = labels[0]
    return segments, labels


class OpenAISRProvider:
    name = "openai"

    def __init__(self, base_url: str, model: str, api_key: str = "", *, language: str = "zh") -> None:
        self.base_url = base_url
        self.model = model or "whisper-1"
        self.api_key = api_key
        self.language = language or "zh"

    async def transcribe(
        self,
        audio_path: Path,
        *,
        language: str = "zh",
        diarization: bool = True,
        progress=None,
    ) -> ASRResult:
        audio_path = Path(audio_path)
        if not audio_path.exists():
            raise ProviderError(f"找不到待转写的音频文件：{audio_path.name}", provider="openai-asr")
        url = audio_transcriptions_url(self.base_url)
        headers = auth_headers(self.api_key)
        duration_ms = media.media_duration_ms(audio_path)
        content_type = _content_type(audio_path)

        async with httpx.AsyncClient() as client:
            data, response = await self._post(client, url, headers, audio_path, content_type, language, diarization, with_granularities=True)
            if response.status_code == 400:
                # Some compatible servers reject unknown form fields: retry minimal.
                logger.info("ASR server rejected timestamp_granularities, retrying without it")
                data, response = await self._post(client, url, headers, audio_path, content_type, language, diarization, with_granularities=False)
        payload = json_or_error(response, "openai-asr")
        segments, labels = _parse_segments(payload, duration_ms)
        if progress:
            progress(100)
        text = str(payload.get("text") or "").strip() or "".join(segment.text for segment in segments)
        return ASRResult(
            text=text,
            segments=segments,
            speakers=labels,
            language=str(payload.get("language") or language or self.language),
            provider=self.name,
            model=self.model,
        )

    async def _post(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        audio_path: Path,
        content_type: str,
        language: str,
        diarization: bool,
        *,
        with_granularities: bool,
    ) -> tuple[dict, httpx.Response]:
        # NOTE: httpx treats a list-of-tuples `data` as raw content and then
        # ignores `files`; a dict is required for multipart form encoding.
        form: dict[str, str] = {
            "model": self.model,
            "response_format": "verbose_json",
            "temperature": "0",
        }
        lang = language or self.language
        if lang:
            form["language"] = lang
        if with_granularities:
            form["timestamp_granularities[]"] = "segment"
        try:
            with audio_path.open("rb") as handle:
                response = await request(
                    client,
                    "POST",
                    url,
                    provider="openai-asr",
                    timeout=1800.0,
                    headers=headers,
                    data=form,
                    files={"file": (audio_path.name, handle, content_type)},
                )
        except OSError as exc:
            raise ProviderError(f"读取音频文件失败：{exc}", provider="openai-asr") from exc
        if response.status_code >= 400 and with_granularities:
            # Defer raising: the caller retries without timestamp_granularities.
            return {}, response
        return json_or_error(response, "openai-asr"), response


__all__ = ["OpenAISRProvider"]
