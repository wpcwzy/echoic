"""AI analysis: transcript formatting, prompt rendering, JSON extraction,
structure validation/clamping and persistence (docs/PIPELINE.md §5).

The four LLM tasks are:

============== ==================== ==========================================
task           prompt key           persisted analysis kinds
============== ==================== ==========================================
``minutes``    ``summary``          summary / insights / action_items / keywords
``chapters``   ``chapters``         chapters
``viewpoint_tree`` ``viewpoint_tree`` viewpoint_tree
``suggested_questions`` ``suggested_questions`` suggested_questions
============== ==================== ==========================================
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.utils import format_timestamp, truncate
from ..models import Analysis, Recording, Speaker, TranscriptSegment
from ..providers.base import LLMProvider

logger = logging.getLogger(__name__)

MAX_TRANSCRIPT_CHARS = 48000
FRONT_RATIO = 0.7
OMIT_MARKER = "\n……（中间内容因长度省略）……\n"

VALID_CATEGORIES = {"decision", "risk", "insight", "opportunity"}
VALID_NODE_KINDS = {"topic", "point", "task", "risk"}
MAX_VIEWPOINT_NODES = 60

TASK_SPECS: list[dict[str, Any]] = [
    {
        "task": "minutes",
        "prompt_key": "summary",
        "kinds": ["summary", "insights", "action_items", "keywords"],
        "critical": True,
    },
    {"task": "chapters", "prompt_key": "chapters", "kinds": ["chapters"], "critical": False},
    {
        "task": "viewpoint_tree",
        "prompt_key": "viewpoint_tree",
        "kinds": ["viewpoint_tree"],
        "critical": False,
    },
    {
        "task": "suggested_questions",
        "prompt_key": "suggested_questions",
        "kinds": ["suggested_questions"],
        "critical": False,
    },
]


class AnalysisError(Exception):
    """Raised when an analysis task cannot produce a usable result."""


class AnalysisCancelled(Exception):
    """Raised when the recording was cancelled while analyzing."""


# ------------------------------------------------------------- RAG chunking
CHUNK_MAX_MS = 45_000
CHUNK_MAX_CHARS = 280
CHUNK_HARD_LIMIT = 1200




def build_chunk_payloads(segment_rows: list[dict[str, Any]], duration_ms: int) -> list[dict[str, Any]]:
    """Build ``{"start_ms","end_ms","text"}`` chunk payloads from segment dicts.

    ``segment_rows`` items must provide ``start_ms``, ``end_ms``, ``speaker`` and
    ``text`` keys.
    """
    chunks: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        start = int(current[0]["start_ms"])
        end = max(int(current[-1]["end_ms"]), start + 1000)
        text = "\n".join(
            f"[{format_timestamp(item['start_ms'])}] {item['speaker']}：{item['text']}" for item in current
        )
        chunks.append(
            {
                "start_ms": start,
                "end_ms": min(end, duration_ms) if duration_ms else end,
                "text": text[:CHUNK_HARD_LIMIT],
            }
        )
        current = []

    for segment in segment_rows:
        if current:
            span = int(segment["end_ms"]) - int(current[0]["start_ms"])
            chars = sum(len(item["text"]) for item in current)
            if span >= CHUNK_MAX_MS or chars >= CHUNK_MAX_CHARS:
                flush()
        current.append(segment)
    flush()

    final: list[dict[str, Any]] = []
    for chunk in chunks:
        text = chunk["text"]
        if len(text) <= CHUNK_HARD_LIMIT:
            final.append(chunk)
            continue
        parts = [text[i : i + CHUNK_HARD_LIMIT - 1] for i in range(0, len(text), CHUNK_HARD_LIMIT - 1)]
        span = max(1, int(chunk["end_ms"]) - int(chunk["start_ms"]))
        for index, part in enumerate(parts):
            final.append(
                {
                    "start_ms": int(chunk["start_ms"]) + int(span * index / len(parts)),
                    "end_ms": int(chunk["start_ms"]) + int(span * (index + 1) / len(parts)),
                    "text": part,
                }
            )
    return final


# ------------------------------------------------------------------ transcript
def build_transcript_text(db: Session, recording: Recording) -> str:
    """Render ``[mm:ss] 说话人：文本`` lines, truncating long transcripts."""
    segments = db.scalars(
        select(TranscriptSegment)
        .where(TranscriptSegment.recording_id == recording.id)
        .order_by(TranscriptSegment.idx)
    ).all()
    speakers = {
        speaker.id: (speaker.display_name or speaker.label)
        for speaker in db.scalars(select(Speaker).where(Speaker.recording_id == recording.id)).all()
    }
    lines = []
    for segment in segments:
        speaker_name = speakers.get(segment.speaker_id) or "说话人"
        text = (segment.text or "").strip()
        if not text:
            continue
        lines.append(f"[{format_timestamp(segment.start_ms)}] {speaker_name}：{text}")
    text = "\n".join(lines)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        front = int(MAX_TRANSCRIPT_CHARS * FRONT_RATIO)
        tail = MAX_TRANSCRIPT_CHARS - front
        text = text[:front] + OMIT_MARKER + text[-tail:]
        logger.info("transcript truncated for analysis: %s -> %s chars", len("\n".join(lines)), len(text))
    return text


def render_prompt(template: str, transcript: str) -> str:
    """Render a prompt template, tolerating custom prompts with stray braces."""
    try:
        return template.format(transcript=transcript)
    except (KeyError, ValueError, IndexError):
        return (template or "").replace("{transcript}", transcript)


# --------------------------------------------------------------- JSON parsing
def _balanced_json_candidates(text: str, limit: int = 40) -> list[str]:
    """Return balanced ``{...}`` / ``[...]`` substrings (string/escape aware)."""
    candidates: list[str] = []
    for start, opener in enumerate(text):
        if opener not in "{[": 
            continue
        stack: list[str] = []
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char in "{[(":
                stack.append(char)
            elif char in "}])":
                if not stack:
                    break
                expected = "}" if stack[-1] == "{" else "]" if stack[-1] == "[" else ")"
                if char != expected:
                    break
                stack.pop()
                if not stack:
                    candidates.append(text[start : index + 1])
                    break
        if len(candidates) >= limit:
            break
    # Longest first: the real JSON document is normally the largest candidate.
    return sorted(set(candidates), key=len, reverse=True)


def _repair_json(text: str) -> Any | None:
    """Tolerant fallback for common LLM JSON mistakes (trailing commas, raw newlines…)."""
    try:
        import json_repair

        value = json_repair.loads(text)
    except Exception:  # noqa: BLE001 - repair is best effort
        return None
    return value if isinstance(value, (dict, list)) and value else None


def extract_json(text: str | None) -> Any | None:
    """Best effort JSON extraction: raw → balanced objects → json-repair."""
    if not text:
        return None
    candidate = str(text).strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", candidate)
        candidate = re.sub(r"\s*```\s*$", "", candidate).strip()

    def _valid(value: Any) -> bool:
        return (isinstance(value, dict) and bool(value)) or (isinstance(value, list) and bool(value))

    # 1) Whole text is already valid JSON (object or top-level array).
    try:
        value = json.loads(candidate)
    except (ValueError, TypeError):
        value = None
    if _valid(value):
        return value

    balanced = _balanced_json_candidates(candidate)
    list_fallback: Any | None = None

    # 2) Strict balanced sub-documents: prefer an object over nested arrays.
    for attempt in balanced:
        try:
            value = json.loads(attempt)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and value:
            return value
        if isinstance(value, list) and value and list_fallback is None:
            list_fallback = value

    # 3) Repair balanced sub-documents (trailing commas, raw newlines, fences…).
    for attempt in balanced:
        repaired = _repair_json(attempt)
        if repaired is None:
            continue
        if isinstance(repaired, dict) and repaired:
            logger.info(
                "extract_json recovered malformed LLM JSON via json-repair (len=%s)", len(attempt)
            )
            return repaired
        if isinstance(repaired, list) and repaired and list_fallback is None:
            list_fallback = repaired

    # 4) Last resort: repair the whole text (e.g. a truncated final object).
    repaired = _repair_json(candidate)
    if isinstance(repaired, dict) and repaired:
        logger.info("extract_json recovered malformed LLM JSON via json-repair (len=%s)", len(candidate))
        return repaired
    if isinstance(repaired, list) and repaired and list_fallback is None:
        list_fallback = repaired
    return list_fallback


# ----------------------------------------------------------------- validation
def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "list", "data"):
            if isinstance(value.get(key), list):
                return value[key]
    return []


def _clamp_ms(value: Any, duration_ms: int) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(int(duration_ms or 0), number))


def sanitize_summary(payload: dict) -> dict | None:
    data = _as_dict(payload)
    markdown = _as_text(data.get("summary_md") or data.get("markdown") or data.get("summary"))
    if not markdown:
        summary = data.get("summary")
        if isinstance(summary, dict):
            markdown = _as_text(summary.get("markdown") or summary.get("content"))
    if not markdown:
        return None
    return {"markdown": markdown}


def sanitize_insights(payload: Any) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(_as_list(payload)[:8]):
        item = _as_dict(raw)
        title = _as_text(item.get("title") or item.get("name"))
        content = _as_text(item.get("content") or item.get("detail") or item.get("text"))
        if not title and not content:
            continue
        title = truncate(title or content, 30)
        if title in seen:
            continue
        seen.add(title)
        category = _as_text(item.get("category")).lower()
        if category not in VALID_CATEGORIES:
            category = "insight"
        items.append(
            {
                "id": _as_text(item.get("id")) or f"ins-{index + 1}",
                "title": title,
                "content": truncate(content, 200),
                "category": category,
            }
        )
    return items


def sanitize_action_items(payload: Any) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(_as_list(payload)[:12]):
        item = _as_dict(raw)
        title = _as_text(item.get("title") or item.get("content") or item.get("task"))
        if not title:
            continue
        if title in seen:
            continue
        seen.add(title)
        status = _as_text(item.get("status")).lower()
        items.append(
            {
                "id": _as_text(item.get("id")) or f"act-{index + 1}",
                "title": truncate(title, 60),
                "owner": truncate(_as_text(item.get("owner")), 40),
                "due": truncate(_as_text(item.get("due")), 40),
                "status": "done" if status == "done" else "open",
                "detail": truncate(_as_text(item.get("detail")), 200),
            }
        )
    return items


def sanitize_keywords(payload: Any) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for raw in _as_list(payload)[:12]:
        if isinstance(raw, str):
            name, weight = raw.strip(), 0.6
        else:
            item = _as_dict(raw)
            name = _as_text(item.get("name") or item.get("keyword") or item.get("word"))
            try:
                weight = float(item.get("weight", 0.6))
            except (TypeError, ValueError):
                weight = 0.6
        if not name or name in seen:
            continue
        seen.add(name)
        items.append({"name": truncate(name, 30), "weight": round(max(0.0, min(1.0, weight)), 3)})
    items.sort(key=lambda item: item["weight"], reverse=True)
    return items[:10]


def sanitize_chapters(payload: Any, duration_ms: int) -> list[dict]:
    raw_chapters = _as_list(payload)
    if isinstance(payload, dict) and not raw_chapters:
        raw_chapters = _as_list(payload.get("chapters") or payload.get("items"))
    duration_ms = max(1, int(duration_ms or 0))
    chapters: list[dict] = []
    for index, raw in enumerate(raw_chapters):
        item = _as_dict(raw)
        title = _as_text(item.get("title"))
        start = _clamp_ms(item.get("start_ms", item.get("start", 0)), duration_ms)
        end = _clamp_ms(item.get("end_ms", item.get("end", start + 60000)), duration_ms)
        if end <= start:
            end = min(duration_ms, start + 60000)
        if not title:
            title = f"第 {index + 1} 部分"
        bullets = [truncate(_as_text(value), 80) for value in _as_list(item.get("bullets"))[:5] if _as_text(value)]
        chapters.append(
            {
                "id": _as_text(item.get("id")) or f"ch-{index + 1}",
                "title": truncate(title, 40),
                "start_ms": start,
                "end_ms": end,
                "summary": truncate(_as_text(item.get("summary")), 300),
                "bullets": bullets,
            }
        )
    if not chapters:
        raise AnalysisError("章节结果为空")
    chapters.sort(key=lambda item: (item["start_ms"], item["end_ms"]))
    # Clamp into [0, duration], then enforce ascending non-overlapping ranges by
    # trimming the previous chapter's end to the next chapter's start.
    for chapter in chapters:
        chapter["start_ms"] = max(0, min(duration_ms, chapter["start_ms"]))
        chapter["end_ms"] = max(0, min(duration_ms, chapter["end_ms"]))
        if chapter["end_ms"] <= chapter["start_ms"]:
            chapter["end_ms"] = min(duration_ms, chapter["start_ms"] + 1000)
    chapters[0]["start_ms"] = 0
    for index in range(len(chapters) - 1):
        if chapters[index]["end_ms"] > chapters[index + 1]["start_ms"]:
            chapters[index]["end_ms"] = chapters[index + 1]["start_ms"]
    chapters = [chapter for chapter in chapters if chapter["end_ms"] > chapter["start_ms"]]
    if not chapters:
        raise AnalysisError("章节时间范围无效")
    chapters[0]["start_ms"] = 0
    chapters[-1]["end_ms"] = max(chapters[-1]["start_ms"] + 1, duration_ms)
    return chapters[:12]


def sanitize_viewpoint_tree(payload: Any, duration_ms: int, speakers: list[str] | None = None) -> dict:
    data = _as_dict(payload)
    roots_raw = data.get("roots") or data.get("trees") or data.get("forest")
    root = data.get("root")
    if not isinstance(root, dict):
        root = data if data.get("label") or data.get("children") else {}
    if not root and not (isinstance(roots_raw, list) and roots_raw):
        raise AnalysisError("观点树结果为空")
    speakers_set = set(speakers or [])
    counter = {"total": 0}

    def build_node(raw: dict, *, node_id: str, depth: int) -> dict:
        counter["total"] += 1
        label = truncate(_as_text(raw.get("label") or raw.get("name") or raw.get("title")), 40)
        detail = truncate(_as_text(raw.get("detail") or raw.get("content")), 160)
        kind = _as_text(raw.get("kind")).lower()
        if kind not in VALID_NODE_KINDS:
            kind = "topic" if depth == 0 else "point"
        node: dict[str, Any] = {
            "id": _as_text(raw.get("id")) or node_id,
            "label": label or ("会议主题" if depth == 0 else "要点"),
            "detail": detail,
            "kind": kind,
            "children": [],
        }
        if raw.get("time_ms") is not None:
            node["time_ms"] = _clamp_ms(raw.get("time_ms"), duration_ms)
        speaker = _as_text(raw.get("speaker"))
        if speaker and (not speakers_set or speaker in speakers_set):
            node["speaker"] = truncate(speaker, 40)
        if depth < 2:
            children = _as_list(raw.get("children"))[:8]
            for index, child in enumerate(children):
                if counter["total"] >= MAX_VIEWPOINT_NODES:
                    break
                if not isinstance(child, dict):
                    continue
                child_id = _as_text(child.get("id")) or f"{node['id']}-{index + 1}"
                node["children"].append(build_node(child, node_id=child_id, depth=depth + 1))
        return node

    # v0.3.4: viewpoint forest — long meetings produce multiple independent topic trees.
    if isinstance(roots_raw, list) and roots_raw:
        forest: list[dict[str, Any]] = []
        for index, raw_root in enumerate(roots_raw[:8]):
            if not isinstance(raw_root, dict):
                continue
            if counter["total"] >= MAX_VIEWPOINT_NODES:
                break
            tree = build_node(
                raw_root,
                node_id=_as_text(raw_root.get("id")) or f"tree-{index + 1}",
                depth=0,
            )
            # Forest roots are always topics and get stable, unique ids.
            tree["id"] = f"tree-{index + 1}"
            tree["kind"] = "topic"
            forest.append(tree)
        if not forest:
            raise AnalysisError("观点森林结果为空")
        return {"roots": forest}

    root_node = build_node(root, node_id=_as_text(root.get("id")) or "root", depth=0)
    root_node["id"] = "root"
    root_node["kind"] = "topic"
    if not root_node["children"]:
        raise AnalysisError("观点树没有分支节点")
    return {"root": root_node}


def sanitize_questions(payload: Any) -> list[str]:
    raw = _as_list(payload)
    if isinstance(payload, dict) and not raw:
        raw = _as_list(payload.get("questions") or payload.get("items"))
    questions: list[str] = []
    for value in raw:
        question = _as_text(value.get("question") if isinstance(value, dict) else value)
        if not question:
            continue
        if question in questions:
            continue
        questions.append(truncate(question, 80))
    if not questions:
        raise AnalysisError("猜你想问结果为空")
    return questions[:8]


# ---------------------------------------------------------------- persistence
def upsert_analysis(
    db: Session,
    recording_id: int,
    kind: str,
    payload: Any,
    provider: str,
    model: str,
) -> None:
    row = db.scalar(
        select(Analysis).where(Analysis.recording_id == recording_id, Analysis.kind == kind)
    )
    text = json.dumps(payload, ensure_ascii=False)
    if row is None:
        db.add(
            Analysis(
                recording_id=recording_id,
                kind=kind,
                payload_json=text,
                provider=provider,
                model=model,
            )
        )
    else:
        row.payload_json = text
        row.provider = provider
        row.model = model
    db.commit()


def delete_analyses(db: Session, recording_id: int, kinds: list[str] | None = None) -> None:
    statement = select(Analysis).where(Analysis.recording_id == recording_id)
    if kinds:
        statement = statement.where(Analysis.kind.in_(kinds))
    for row in db.scalars(statement).all():
        db.delete(row)
    db.commit()


# ------------------------------------------------------------------ LLM calls
def _task_messages(prompt: str, extra: str = "") -> list[dict[str, str]]:
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "请严格按要求只输出 JSON，不要输出解释文字或 Markdown 代码围栏。"},
    ]
    if extra:
        messages.append({"role": "user", "content": extra})
    return messages


async def _complete_json(llm: LLMProvider, prompt: str, task: str, temperature: float) -> Any:
    """Call the LLM (json_mode) and parse; one repair attempt with error feedback."""
    last_error = ""
    raw = ""
    for attempt in range(2):
        messages = _task_messages(prompt)
        if attempt == 1:
            messages = _task_messages(
                prompt,
                "上一次的输出无法解析为要求的 JSON。错误：{error}\n上一次输出（截断）：{raw}\n"
                "请重新严格按 JSON 结构输出，不要包含任何其他文字。".format(
                    error=last_error[:300], raw=raw[:4000]
                ),
            )
        try:
            raw = await llm.complete(
                messages,
                json_mode=True,
                temperature=temperature,
                max_tokens=8192 if task in ("chapters", "viewpoint_tree") else 4096,
            )
        except Exception as exc:  # provider errors are retried once here too
            last_error = str(exc)
            logger.warning("analysis task %s LLM call failed (attempt %s): %s", task, attempt + 1, exc)
            if attempt == 0:
                continue
            raise AnalysisError(f"模型调用失败：{last_error[:200]}") from exc
        parsed = extract_json(raw)
        if parsed is None:
            last_error = "输出不是合法 JSON"
            logger.warning("analysis task %s returned unparsable JSON (len=%s)", task, len(raw or ""))
            continue
        return parsed
    raise AnalysisError(f"模型输出无法解析为 JSON：{last_error[:120]}")


def _persist_task_results(
    db: Session,
    recording_id: int,
    task: str,
    payload: Any,
    *,
    provider: str,
    model: str,
    duration_ms: int,
    speakers: list[str],
) -> list[str]:
    payload_dict = _as_dict(payload)
    written: list[str] = []
    if task == "minutes":
        summary = sanitize_summary(payload_dict)
        if not summary:
            # Validate before writing the partial kinds: a missing summary makes
            # the whole recording fail, so avoid leaving orphan analysis rows.
            raise AnalysisError("AI 纪要缺少 summary_md 字段")
        upsert_analysis(db, recording_id, "summary", summary, provider, model)
        written.append("summary")
        insights = sanitize_insights(payload_dict.get("insights"))
        if insights:
            upsert_analysis(db, recording_id, "insights", insights, provider, model)
            written.append("insights")
        actions = sanitize_action_items(payload_dict.get("action_items"))
        if actions:
            upsert_analysis(db, recording_id, "action_items", actions, provider, model)
            written.append("action_items")
        keywords = sanitize_keywords(payload_dict.get("keywords"))
        if keywords:
            upsert_analysis(db, recording_id, "keywords", keywords, provider, model)
            written.append("keywords")
    elif task == "chapters":
        chapters = sanitize_chapters(payload_dict, duration_ms)
        upsert_analysis(db, recording_id, "chapters", chapters, provider, model)
        written.append("chapters")
    elif task == "viewpoint_tree":
        tree = sanitize_viewpoint_tree(payload_dict, duration_ms, speakers)
        upsert_analysis(db, recording_id, "viewpoint_tree", tree, provider, model)
        written.append("viewpoint_tree")
    elif task == "suggested_questions":
        questions = sanitize_questions(payload_dict)
        upsert_analysis(db, recording_id, "suggested_questions", questions, provider, model)
        written.append("suggested_questions")
    return written


async def generate_analyses(
    db: Session,
    recording: Recording,
    cfg,
    *,
    progress: Callable[[int], None] | None = None,
    check_cancel: Callable[[], bool] | None = None,
) -> dict[str, list[str]]:
    """Run the four analysis tasks and persist results.

    Returns ``{"written": [kinds], "failed": [task names]}``.  A failure of the
    critical ``minutes`` task raises :class:`AnalysisError`; other tasks keep
    going (their kinds simply stay missing).
    """
    from ..providers.registry import build_llm_provider

    transcript = build_transcript_text(db, recording)
    if not transcript.strip():
        raise AnalysisError("转录稿为空，无法生成 AI 纪要")
    duration_ms = int(recording.duration_ms or 0)
    speakers = [
        speaker.display_name or speaker.label
        for speaker in db.scalars(select(Speaker).where(Speaker.recording_id == recording.id)).all()
    ]
    llm = build_llm_provider(cfg.llm)
    provider_name = cfg.llm.provider or "mock"
    model_name = cfg.llm.model or ""
    written: list[str] = []
    failed: list[str] = []
    task_count = len(TASK_SPECS)

    for index, spec in enumerate(TASK_SPECS):
        if check_cancel is not None and check_cancel():
            raise AnalysisCancelled()
        task = spec["task"]
        prompt_key = spec["prompt_key"]
        template = cfg.prompts.get(prompt_key) or ""
        prompt = render_prompt(template, transcript)
        if not prompt.strip():
            prompt = render_prompt(
                "TASK={task}\n请基于以下转录稿生成 JSON。\n{transcript}".replace("{task}", task), transcript
            )
        try:
            payload = await _complete_json(llm, prompt, task, float(cfg.llm.temperature or 0.3))
            kinds = _persist_task_results(
                db,
                recording.id,
                task,
                payload,
                provider=provider_name,
                model=model_name,
                duration_ms=duration_ms,
                speakers=speakers,
            )
            written.extend(kinds)
            logger.info("analysis task %s ok (kinds=%s)", task, ",".join(kinds))
        except AnalysisCancelled:
            raise
        except AnalysisError as exc:
            if spec["critical"]:
                raise
            failed.append(task)
            logger.warning("analysis task %s failed: %s", task, exc)
        except Exception as exc:  # noqa: BLE001 - never let one kind kill the pipeline
            if spec["critical"]:
                raise AnalysisError(f"AI 纪要生成失败：{type(exc).__name__}") from exc
            failed.append(task)
            logger.warning("analysis task %s failed unexpectedly: %s", task, exc)
        if progress is not None:
            progress(int((index + 1) * 100 / task_count))
    return {"written": written, "failed": failed}


__all__ = [
    "AnalysisCancelled",
    "CHUNK_HARD_LIMIT",
    "CHUNK_MAX_CHARS",
    "CHUNK_MAX_MS",
    "build_chunk_payloads",
    "AnalysisError",
    "MAX_TRANSCRIPT_CHARS",
    "TASK_SPECS",
    "build_transcript_text",
    "delete_analyses",
    "extract_json",
    "generate_analyses",
    "render_prompt",
    "sanitize_action_items",
    "sanitize_chapters",
    "sanitize_insights",
    "sanitize_keywords",
    "sanitize_questions",
    "sanitize_summary",
    "sanitize_viewpoint_tree",
    "upsert_analysis",
]
