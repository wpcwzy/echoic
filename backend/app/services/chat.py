"""RAG chat over a processed recording (docs/PIPELINE.md §6, docs/API.md §7).

``stream_chat`` is an async iterator of event dicts consumed by the SSE API:

    {"type": "start", "message_id": N}
    {"type": "delta", "text": "..."}
    {"type": "citations", "citations": [Citation, ...]}
    {"type": "done", "message_id": N}
    {"type": "error", "message": "中文提示"}

Retrieval uses numpy cosine over chunk embeddings when available, otherwise
CJK bigram + English token lexical scoring.  Both the user message and the
(possibly partial) assistant message are persisted, including on disconnect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections import Counter
from typing import Any

import numpy as np
from sqlalchemy import select

from ..core.errors import ProviderError
from ..core.utils import format_timestamp, load_json, truncate, utcnow
from ..models import (
    Analysis,
    ChatMessage,
    Chunk,
    Recording,
    RecordingStatus,
    Speaker,
    TranscriptSegment,
)
from ..providers.registry import build_embedding_provider, build_llm_provider
from .settings_store import load_ai_config

logger = logging.getLogger(__name__)

TOP_CHUNKS = 8
MAX_CONTEXT_CHARS = 14_000
SUMMARY_BUDGET = 1_500
CHAPTER_BUDGET = 2_500
HISTORY_ROUNDS = 6
HISTORY_MESSAGE_LIMIT = 1_500
CITATION_LIMIT = 5

CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
EN_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+.#_-]{1,}")


class ChatError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# ------------------------------------------------------------------ retrieval
def _text_tokens(text: str) -> set[str]:
    text = (text or "").lower()
    tokens: set[str] = set()
    for run in CJK_RUN_RE.findall(text):
        for index in range(len(run) - 1):
            tokens.add(run[index : index + 2])
    for token in EN_TOKEN_RE.findall(text):
        tokens.add(token)
    return tokens


def _lexical_scores(question: str, chunks: list[Chunk]) -> list[float]:
    query_tokens = _text_tokens(question)
    if not query_tokens or not chunks:
        return [0.0] * len(chunks)
    document_tokens = [_text_tokens(chunk.text or "") for chunk in chunks]
    doc_freq = Counter()
    for tokens in document_tokens:
        for token in tokens & query_tokens:
            doc_freq[token] += 1
    total = len(chunks)
    idf = {token: math.log(1 + total / (1 + frequency)) for token, frequency in doc_freq.items()}
    normalized_question = re.sub(r"\s+", "", question or "")
    scores: list[float] = []
    for chunk, tokens in zip(chunks, document_tokens):
        shared = query_tokens & tokens
        score = sum(idf.get(token, 1.0) for token in shared) / math.sqrt(len(tokens) + 1)
        if normalized_question and normalized_question in re.sub(r"\s+", "", chunk.text or ""):
            score += 2.0
        scores.append(score)
    return scores


def _embedding_scores(question_vector: list[float], chunks: list[Chunk]) -> list[float]:
    query = np.asarray(question_vector, dtype=np.float32)
    norm = float(np.linalg.norm(query))
    if norm <= 0:
        return [0.0] * len(chunks)
    query = query / norm
    scores: list[float] = []
    for chunk in chunks:
        if not chunk.embedding or not chunk.dim:
            scores.append(0.0)
            continue
        try:
            vector = np.frombuffer(chunk.embedding, dtype=np.float32)
        except (ValueError, TypeError):
            scores.append(0.0)
            continue
        if vector.size != query.size:
            scores.append(0.0)
            continue
        vector_norm = float(np.linalg.norm(vector))
        if vector_norm <= 0:
            scores.append(0.0)
            continue
        scores.append(float(np.dot(query, vector / vector_norm)))
    return scores


async def _retrieve(
    cfg,
    chunks: list[Chunk],
    segments: list[TranscriptSegment],
    speaker_names: dict[int, str],
    summary_payload: dict | None,
    chapters_payload: list | None,
    question: str,
) -> tuple[str, list[dict]]:
    """Return (context block, citations) for the question."""
    scores: list[float] = [0.0] * len(chunks)
    has_vectors = any(chunk.embedding for chunk in chunks) and (cfg.embeddings.provider or "none") != "none"
    if has_vectors and chunks:
        try:
            provider = build_embedding_provider(cfg.embeddings)
            if provider is not None:
                embed_query = getattr(provider, "embed_query", None)
                if callable(embed_query):
                    vectors = await embed_query([question])
                else:
                    vectors = await provider.embed([question])
                if vectors:
                    scores = _embedding_scores(vectors[0], chunks)
        except Exception as exc:  # noqa: BLE001 - fall back to lexical retrieval
            logger.warning("embedding query failed, falling back to lexical retrieval: %s", exc)
            has_vectors = False
    if not has_vectors:
        scores = _lexical_scores(question, chunks)

    ranked = sorted(range(len(chunks)), key=lambda index: scores[index], reverse=True)
    selected = [index for index in ranked[:TOP_CHUNKS] if scores[index] > 0] or ranked[: min(3, len(ranked))]

    # Context budget: summary → chapter directory → transcript chunks.
    parts: list[str] = []
    used = 0
    summary_text = ""
    if isinstance(summary_payload, dict):
        summary_text = str(summary_payload.get("markdown") or "").strip()
    if summary_text:
        block = "【会议纪要摘要】\n" + truncate(summary_text, SUMMARY_BUDGET)
        parts.append(block)
        used += len(block)
    chapter_lines: list[str] = []
    if isinstance(chapters_payload, list):
        for chapter in chapters_payload[:12]:
            if not isinstance(chapter, dict):
                continue
            title = str(chapter.get("title") or "").strip()
            if not title:
                continue
            chapter_lines.append(f"- [{format_timestamp(chapter.get('start_ms') or 0)}] {title}")
    if chapter_lines:
        block = "【章节目录】\n" + "\n".join(chapter_lines)
        block = truncate(block, CHAPTER_BUDGET)
        parts.append(block)
        used += len(block)

    chunk_blocks: list[str] = []
    for rank, index in enumerate(selected):
        chunk = chunks[index]
        block = f"【转录片段 {rank + 1}】\n{chunk.text}"
        if used + len(block) > MAX_CONTEXT_CHARS:
            remaining = MAX_CONTEXT_CHARS - used
            if remaining > 120:
                block = truncate(block, remaining)
            else:
                break
        chunk_blocks.append(block)
        used += len(block)
    if chunk_blocks:
        parts.append("\n\n".join(chunk_blocks))

    # No chunks at all (defensive): fall back to raw transcript segments.
    if not chunk_blocks and segments:
        fallback_lines = []
        for segment in segments[:200]:
            speaker = speaker_names.get(segment.speaker_id) or "说话人"
            fallback_lines.append(
                f"[{format_timestamp(segment.start_ms)}] {speaker}：{segment.text}"
            )
        fallback = "\n".join(fallback_lines)
        parts.append("【转录片段】\n" + truncate(fallback, MAX_CONTEXT_CHARS - used))
    context = "\n\n".join(parts) if parts else "（无可用转录片段）"

    citations = _build_citations(chunks, selected, segments, speaker_names)
    return context, citations


def _build_citations(
    chunks: list[Chunk],
    selected: list[int],
    segments: list[TranscriptSegment],
    speaker_names: dict[int, str],
) -> list[dict]:
    citations: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for index in selected:
        if len(citations) >= CITATION_LIMIT:
            break
        chunk = chunks[index]
        overlapping = [
            segment
            for segment in segments
            if segment.end_ms > chunk.start_ms and segment.start_ms < chunk.end_ms
        ]
        speaker = ""
        segment_id = None
        start_ms = int(chunk.start_ms)
        end_ms = int(chunk.end_ms)
        if overlapping:
            first = overlapping[0]
            speaker = speaker_names.get(first.speaker_id) or ""
            segment_id = first.id
            start_ms = int(first.start_ms)
            end_ms = int(first.end_ms)
        key = (start_ms, end_ms)
        if key in seen:
            continue
        seen.add(key)
        citations.append(
            {
                "recording_id": int(chunk.recording_id),
                "segment_id": segment_id,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "speaker": speaker,
                "text": truncate(chunk.text or "", 120),
            }
        )
    if len(citations) < 3 and len(chunks) > len(citations):
        # Fill up to 3 citations with the remaining top chunks.
        for index in selected:
            if len(citations) >= 3:
                break
            chunk = chunks[index]
            if any(citation["start_ms"] == int(chunk.start_ms) for citation in citations):
                continue
            citations.append(
                {
                    "recording_id": int(chunk.recording_id),
                    "segment_id": None,
                    "start_ms": int(chunk.start_ms),
                    "end_ms": int(chunk.end_ms),
                    "speaker": "",
                    "text": truncate(chunk.text or "", 120),
                }
            )
    return citations[:CITATION_LIMIT]


# --------------------------------------------------------------- DB lifecycle
def _create_messages(db_factory, recording_id: int, question: str) -> dict[str, Any]:
    with db_factory() as db:
        recording = db.get(Recording, recording_id)
        if recording is None:
            raise ChatError("录音不存在或已被删除")
        if recording.status != RecordingStatus.READY:
            raise ChatError("录音尚未处理完成，请等待处理完成后再提问")
        cfg = load_ai_config(db)
        previous = list(
            db.scalars(
                select(ChatMessage)
                .where(ChatMessage.recording_id == recording_id)
                .order_by(ChatMessage.id.desc())
                .limit(HISTORY_ROUNDS * 2)
            ).all()
        )
        previous.reverse()
        history = [
            {"role": message.role, "content": truncate(message.content, HISTORY_MESSAGE_LIMIT)}
            for message in previous
            if message.role in ("user", "assistant") and (message.content or "").strip()
        ]
        user_row = ChatMessage(recording_id=recording_id, role="user", content=question)
        assistant_row = ChatMessage(recording_id=recording_id, role="assistant", content="")
        db.add(user_row)
        db.add(assistant_row)
        db.commit()
        assistant_id = int(assistant_row.id)
        user_id = int(user_row.id)
        duration_ms = int(recording.duration_ms or 0)
        chunks = list(
            db.scalars(
                select(Chunk).where(Chunk.recording_id == recording_id).order_by(Chunk.idx)
            ).all()
        )
        segments = list(
            db.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.recording_id == recording_id)
                .order_by(TranscriptSegment.idx)
            ).all()
        )
        speaker_names = {
            speaker.id: (speaker.display_name or speaker.label)
            for speaker in db.scalars(
                select(Speaker).where(Speaker.recording_id == recording_id)
            ).all()
        }
        summary_payload = None
        chapters_payload = None
        for analysis in db.scalars(
            select(Analysis).where(
                Analysis.recording_id == recording_id, Analysis.kind.in_(["summary", "chapters"])
            )
        ).all():
            payload = load_json(analysis.payload_json)
            if analysis.kind == "summary":
                summary_payload = payload if isinstance(payload, dict) else None
            elif analysis.kind == "chapters":
                chapters_payload = payload if isinstance(payload, list) else None
        return {
            "cfg": cfg,
            "history": history,
            "assistant_id": assistant_id,
            "user_id": user_id,
            "duration_ms": duration_ms,
            "chunks": chunks,
            "segments": segments,
            "speaker_names": speaker_names,
            "summary_payload": summary_payload,
            "chapters_payload": chapters_payload,
        }


def _save_assistant(db_factory, assistant_id: int, content: str, citations: list[dict]) -> None:
    try:
        with db_factory() as db:
            row = db.get(ChatMessage, assistant_id)
            if row is None:
                return
            row.content = content or ""
            row.citations_json = json.dumps(citations or [], ensure_ascii=False) if citations else None
            db.commit()
    except Exception:  # pragma: no cover - never mask the streaming error
        logger.exception("failed to persist assistant chat message %s", assistant_id)


def _build_messages(context: str, history: list[dict], question: str, chat_prompt: str) -> list[dict[str, str]]:
    system = (chat_prompt or "TASK=chat\n你是 Echoic 的私人会议助理。").strip()
    system = f"{system}\n\n参考资料：\n{context}"
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    messages.extend(history[-(HISTORY_ROUNDS * 2) :])
    messages.append({"role": "user", "content": question})
    return messages


# -------------------------------------------------------------------- public
async def stream_chat(db_factory, recording_id: int, user_message: str) -> Any:
    """Async generator of SSE event dicts (see module docstring)."""
    question = (user_message or "").strip()
    if not question:
        yield {"type": "error", "message": "问题不能为空"}
        return
    try:
        prepared = _create_messages(db_factory, int(recording_id), question)
    except ChatError as exc:
        yield {"type": "error", "message": exc.message}
        return
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to prepare chat for recording %s", recording_id)
        yield {"type": "error", "message": "问答服务初始化失败，请稍后重试"}
        return

    assistant_id = prepared["assistant_id"]
    yield {"type": "start", "message_id": assistant_id}

    cfg = prepared["cfg"]
    citations: list[dict] = []
    answer = ""
    try:
        context, citations = await _retrieve(
            cfg,
            prepared["chunks"],
            prepared["segments"],
            prepared["speaker_names"],
            prepared["summary_payload"],
            prepared["chapters_payload"],
            question,
        )
        messages = _build_messages(context, prepared["history"], question, cfg.prompts.get("chat") or "")
        llm = build_llm_provider(cfg.llm)
        async for piece in llm.stream(messages, temperature=float(cfg.llm.temperature or 0.3)):
            if not piece:
                continue
            answer += piece
            yield {"type": "delta", "text": piece}
        _save_assistant(db_factory, assistant_id, answer, citations)
        yield {"type": "citations", "citations": citations}
        yield {"type": "done", "message_id": assistant_id}
    except asyncio.CancelledError:
        # Client disconnected: keep whatever was generated.
        _save_assistant(db_factory, assistant_id, answer, citations)
        raise
    except ProviderError as exc:
        _save_assistant(db_factory, assistant_id, answer, citations)
        yield {"type": "error", "message": exc.message}
    except Exception:  # noqa: BLE001
        logger.exception("chat streaming failed for recording %s", recording_id)
        _save_assistant(db_factory, assistant_id, answer, citations)
        yield {"type": "error", "message": "回答生成失败，请稍后重试"}


__all__ = ["ChatError", "stream_chat"]
