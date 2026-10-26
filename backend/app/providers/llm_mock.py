"""Offline mock LLM provider.

No network access. Behaviour (docs/PIPELINE.md §4.2):

* Analysis tasks (``TASK=minutes|chapters|viewpoint_tree|suggested_questions``):
  when the prompt transcript matches the bundled demo fixture, return the
  hand-written analyses; otherwise derive deterministic, structurally valid
  Chinese analyses from the transcript in the prompt.
* Chat (``TASK=chat``): answer from the demo ``chat_examples`` when the question
  matches, otherwise answer from the retrieved context block in the prompt.
  Streams in 20-40 character chunks with a 20 ms pause.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

from ..core.utils import format_timestamp, truncate
from .base import LLMProvider
from .demo_fixtures import (
    demo_insights,
    find_chat_example,
    match_recording_by_summary,
    match_recording_by_text,
)

logger = logging.getLogger(__name__)

TASK_RE = re.compile(r"TASK=([a-zA-Z_]+)")
LINE_RE = re.compile(r"^\[((?:\d{1,2}:)?\d{1,2}:\d{2})\]\s*([^:：\n]{1,24})[:：]\s*(.*)$", re.MULTILINE)
EN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.#_-]{1,}")
CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")

_STOP_CHARS = set(
    "的了是在和与就都也还不没有这那我你他她们个们一下什么可以怎么需要应该现在然后所以但是如果因为"
    "大家我们你们他们时间问题讨论进行一个对把被会让给到从向于之其比更最很太挺非常次每各些种回"
)
_STOP_TERMS = {"会议", "大家", "我们", "你们", "他们", "这个", "那个", "然后", "可以", "需要", "应该", "问题", "时间", "一个"}
# Frequent but non-topical bigrams that would otherwise pollute keywords.
_STOP_WORDS = {
    "今天", "开始", "出来", "群里", "清楚", "时候", "东西", "样子", "一直", "一起", "已经",
    "还是", "什么", "怎么", "因为", "所以", "然后", "如果", "但是", "现在", "我们", "你们",
    "他们", "大家", "自己", "别人", "这样", "那样", "可能", "应该", "需要", "觉得", "感觉",
    "认为", "知道", "看到", "听到", "说到", "做完", "起来", "下去", "上来", "下来", "里面",
    "外面", "前面", "后面", "上面", "下面", "地方", "事情", "情况", "部分", "方面", "方法",
    "方式", "简单", "容易", "麻烦", "只是", "而且", "或者", "不过", "其实", "当然", "比较",
    "进行", "看看", "一下", "有点", "一点", "做好", "弄好", "搞定", "问题", "会议", "第一",
    "第二", "第三", "最后", "之前", "之后", "以后", "以前", "下次", "这次", "上次",
}
_DEFAULT_KEYWORDS = ["会议纪要", "项目进展", "待办事项", "风险", "后续计划"]
_TODO_HINTS = ("要", "需要", "负责", "提交", "完成", "准备", "安排", "跟进", "推进", "确认", "整理", "同步")
_RISK_HINTS = ("风险", "担心", "问题", "延期", "卡住", "不确定性", "失败")
_DECISION_HINTS = ("决定", "确定", "结论", "定为", "通过", "拍板", "敲定")
_DUE_RE = re.compile(r"(下?周[一二三四五六日天]|明天|后天|下周|本周|月底|月底前|\d+\s*天|\d+\s*周|\d+\s*月\s*\d+\s*[日号])")


def _task_of(messages: list[dict[str, str]]) -> str:
    for message in messages:
        match = TASK_RE.search(str(message.get("content") or ""))
        if match:
            return match.group(1)
    return ""


def _prompt_text(messages: list[dict[str, str]]) -> str:
    return "\n".join(str(m.get("content") or "") for m in messages)


def _parse_ts(value: str) -> int:
    parts = [int(p) for p in value.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return (minutes * 60 + seconds) * 1000
    hours, minutes, seconds = parts
    return (hours * 3600 + minutes * 60 + seconds) * 1000


def parse_transcript(prompt: str) -> list[dict[str, Any]]:
    """Extract ``[mm:ss] speaker：text`` lines from a rendered prompt."""
    items: list[dict[str, Any]] = []
    for match in LINE_RE.finditer(prompt or ""):
        text = match.group(3).strip()
        if not text:
            continue
        items.append(
            {
                "start_ms": _parse_ts(match.group(1)),
                "speaker": match.group(2).strip(),
                "text": text,
            }
        )
    return items


def _extract_keywords(text: str, limit: int = 8) -> list[str]:
    english = Counter()
    for token in EN_TOKEN_RE.findall(text or ""):
        english[token] += 1
    bigrams: Counter[str] = Counter()
    trigrams: Counter[str] = Counter()
    for run in CJK_RUN_RE.findall(text or ""):
        for index in range(len(run) - 1):
            gram = run[index : index + 2]
            if any(ch in _STOP_CHARS for ch in gram) or gram in _STOP_TERMS or gram in _STOP_WORDS:
                continue
            bigrams[gram] += 1
        for index in range(len(run) - 2):
            gram = run[index : index + 3]
            if any(ch in _STOP_CHARS for ch in gram) or gram in _STOP_TERMS or gram in _STOP_WORDS:
                continue
            trigrams[gram] += 1
    ranked: list[tuple[int, int, str]] = []
    for term, count in bigrams.items():
        if count >= 2:
            ranked.append((count * 2, len(term), term))
    for term, count in trigrams.items():
        if count >= 2:
            ranked.append((count * 3, len(term), term))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    result: list[str] = []
    for _score, _length, term in ranked:
        if any(term in kept or kept in term for kept in result):
            continue
        result.append(term)
        if len(result) >= limit:
            break
    for term, _count in english.most_common(3):
        if term not in result:
            result.append(term)
            if len(result) >= limit:
                break
    for fallback in _DEFAULT_KEYWORDS:
        if len(result) >= 5:
            break
        if fallback not in result:
            result.append(fallback)
    return result[:10]


def _keyword_payload(lines: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    terms = _extract_keywords(text, limit=10)
    payload = []
    for index, term in enumerate(terms):
        weight = round(max(0.35, 1.0 - index * 0.09), 2)
        payload.append({"name": term, "weight": weight})
    return payload


def _sentences(lines: list[dict[str, Any]], min_len: int = 8) -> list[dict[str, Any]]:
    return [line for line in lines if len(line.get("text") or "") >= min_len]


def _pick(lines: list[dict[str, Any]], hints: tuple[str, ...], count: int) -> list[dict[str, Any]]:
    picked = [line for line in lines if any(hint in line["text"] for hint in hints)]
    if not picked:
        picked = list(lines)
    unique: list[dict[str, Any]] = []
    for line in picked:
        if line["text"] not in {item["text"] for item in unique}:
            unique.append(line)
    return unique[:count]


def _heuristic_minutes(lines: list[dict[str, Any]], text: str) -> dict[str, Any]:
    speakers = list(dict.fromkeys(line["speaker"] for line in lines))
    keywords = _extract_keywords(text, limit=8)
    keyword_text = "、".join(keywords[:4]) or "会议主题"
    topic_lines = _pick(_sentences(lines), _DECISION_HINTS + ("进展", "完成", "确定"), 5)
    if not topic_lines:
        topic_lines = _sentences(lines)[:5] or lines[:3]
    conclusions = "\n".join(
        f"- {line['speaker']}：{truncate(line['text'], 60)}" for line in topic_lines[:5]
    )
    decisions = _pick(lines, _DECISION_HINTS, 3)
    decisions_md = (
        "\n".join(f"- {truncate(line['text'], 60)}" for line in decisions)
        if decisions
        else "无"
    )
    action_lines = _pick(lines, _TODO_HINTS, 8)
    todos = "\n".join(
        f"- [ ] {truncate(line['text'], 50)}（负责人：{line['speaker']}，截止：{_detect_due(line['text'])}）"
        for line in action_lines
    ) or "- [ ] 转录稿未提及明确待办"
    summary_md = (
        f"## 会议概述\n本次会议共 {len(speakers)} 位发言人参与，围绕{keyword_text}等主题展开讨论，"
        f"转录稿共 {len(lines)} 个发言片段。\n\n"
        f"## 关键结论\n{conclusions or '- 转录稿未提及明确结论'}\n\n"
        f"## 达成的决议\n{decisions_md}\n\n"
        f"## 待办事项\n{todos}"
    )

    insights: list[dict[str, Any]] = []
    categories = (
        ("decision", _DECISION_HINTS),
        ("risk", _RISK_HINTS),
        ("opportunity", ("建议", "可以", "优化", "提升", "方案")),
        ("insight", tuple()),
    )
    used: set[str] = set()
    for category, hints in categories:
        for line in _pick(_sentences(lines), hints, 6):
            if line["text"] in used:
                continue
            used.add(line["text"])
            insights.append(
                {
                    "title": truncate(line["text"], 20),
                    "content": truncate(f"{line['speaker']}提到：{line['text']}", 90),
                    "category": category,
                }
            )
            break
    if len(insights) < 3:
        for line in _sentences(lines):
            if len(insights) >= 3:
                break
            if line["text"] in used:
                continue
            used.add(line["text"])
            insights.append(
                {
                    "title": truncate(line["text"], 20),
                    "content": truncate(f"{line['speaker']}提到：{line['text']}", 90),
                    "category": "insight",
                }
            )

    action_items = [
        {
            "title": truncate(line["text"], 28),
            "owner": line["speaker"],
            "due": _detect_due(line["text"]),
            "detail": truncate(line["text"], 80),
        }
        for line in action_lines[:8]
    ]
    if len(action_items) < 2:
        for line in _sentences(lines)[:2]:
            action_items.append(
                {
                    "title": truncate(line["text"], 28),
                    "owner": line["speaker"],
                    "due": "",
                    "detail": truncate(line["text"], 80),
                }
            )
    return {
        "summary_md": summary_md,
        "insights": insights[:6],
        "action_items": action_items[:8],
        "keywords": _keyword_payload(lines, text),
    }


def _detect_due(text: str) -> str:
    match = _DUE_RE.search(text or "")
    return match.group(1) if match else ""


def _split_groups(items: list[dict[str, Any]], groups: int) -> list[list[dict[str, Any]]]:
    if not items:
        return []
    groups = max(1, min(groups, len(items)))
    size = max(1, len(items) // groups)
    result: list[list[dict[str, Any]]] = []
    cursor = 0
    for index in range(groups):
        end = len(items) if index == groups - 1 else min(len(items), cursor + size)
        chunk = items[cursor:end]
        if chunk:
            result.append(chunk)
        cursor = end
        if cursor >= len(items):
            break
    return result


def _heuristic_chapters(lines: list[dict[str, Any]], text: str) -> dict[str, Any]:
    group_count = max(3, min(8, len(lines) // 4 + 1))
    groups = _split_groups(lines, group_count)
    chapters: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        group_text = "".join(item["text"] for item in group)
        keywords = _extract_keywords(group_text, limit=3)
        title = (
            f"{keywords[0]}与{keywords[1]}讨论"
            if len(keywords) >= 2
            else f"{keywords[0] if keywords else '会议内容'}讨论"
        )
        start_ms = group[0]["start_ms"] if index else 0
        next_start = groups[index + 1][0]["start_ms"] if index + 1 < len(groups) else None
        end_ms = (next_start - 1) if next_start and next_start > start_ms else group[-1]["start_ms"] + 120000
        chapters.append(
            {
                "title": truncate(title, 20),
                "start_ms": start_ms,
                "end_ms": max(start_ms, end_ms),
                "summary": truncate(group[0]["text"], 80),
                "bullets": [truncate(item["text"], 40) for item in group[:3]],
            }
        )
    return {"chapters": chapters}


def _node_kind(text: str) -> str:
    if any(hint in text for hint in _RISK_HINTS):
        return "risk"
    if any(hint in text for hint in _TODO_HINTS):
        return "task"
    return "point"


def _heuristic_viewpoint_tree(lines: list[dict[str, Any]], text: str) -> dict[str, Any]:
    keywords = _extract_keywords(text, limit=6)
    root_label = truncate("与".join(keywords[:2]) + "会议" if len(keywords) >= 2 else "会议讨论要点", 16)
    groups = _split_groups(lines, max(3, min(5, len(lines) // 4 + 1)))
    children: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        group_text = "".join(item["text"] for item in group)
        group_keywords = _extract_keywords(group_text, limit=2)
        branch_label = truncate(group_keywords[0] if group_keywords else f"议题{index + 1}", 16)
        points: list[dict[str, Any]] = []
        for point_index, item in enumerate(group[:3]):
            points.append(
                {
                    "id": f"n{index + 1}-{point_index + 1}",
                    "label": truncate(item["text"], 24),
                    "detail": truncate(item["text"], 80),
                    "kind": _node_kind(item["text"]),
                    "time_ms": item["start_ms"],
                    "speaker": item["speaker"],
                    "children": [],
                }
            )
        children.append(
            {
                "id": f"n{index + 1}",
                "label": branch_label,
                "detail": truncate(group[0]["text"], 80) if group else "",
                "kind": "topic",
                "time_ms": group[0]["start_ms"] if group else 0,
                "speaker": group[0]["speaker"] if group else "",
                "children": points,
            }
        )
    return {
        "root": {
            "id": "root",
            "label": root_label,
            "detail": f"会议共 {len(lines)} 段发言，围绕{('、'.join(keywords[:3]) or '多个主题')}展开。",
            "kind": "topic",
            "children": children,
        }
    }


def _heuristic_questions(lines: list[dict[str, Any]], text: str) -> dict[str, Any]:
    keywords = _extract_keywords(text, limit=6)
    speakers = list(dict.fromkeys(line["speaker"] for line in lines))
    questions: list[str] = ["帮我提炼一下这次会议的重点内容"]
    if len(keywords) >= 2:
        questions.append(f"{keywords[0]}和{keywords[1]}之间是什么关系？")
    if speakers:
        questions.append(f"{speakers[0]}负责的部分具体怎么安排？")
    if keywords:
        questions.append(f"{keywords[-1]}这个问题的结论是什么？")
    questions.append("会议确定了哪些时间节点和交付物？")
    questions.append("有哪些风险和待办需要后续跟进？")
    return {"questions": questions[:6]}


def _demo_payload(task: str, fixture: dict[str, Any]) -> dict[str, Any]:
    insights = demo_insights(fixture)
    if task == "minutes":
        return {
            "summary_md": insights.get("summary_md") or "",
            "insights": insights.get("insights") or [],
            "action_items": insights.get("action_items") or [],
            "keywords": insights.get("keywords") or [],
        }
    if task == "chapters":
        return {"chapters": insights.get("chapters") or []}
    if task == "viewpoint_tree":
        return {"root": insights.get("viewpoint_tree") or {}}
    if task == "suggested_questions":
        return {"questions": insights.get("suggested_questions") or []}
    return {}


def _heuristic_payload(task: str, prompt: str) -> dict[str, Any]:
    lines = parse_transcript(prompt)
    if not lines:
        # Called directly in unit tests without a rendered prompt: build a tiny stub.
        lines = [{"start_ms": 0, "speaker": "说话人1", "text": "这是一段测试转录文本。"}]
    text = "".join(line["text"] for line in lines)
    if task == "minutes":
        return _heuristic_minutes(lines, text)
    if task == "chapters":
        return _heuristic_chapters(lines, text)
    if task == "viewpoint_tree":
        return _heuristic_viewpoint_tree(lines, text)
    if task == "suggested_questions":
        return _heuristic_questions(lines, text)
    return {}


def _context_answer(system_prompt: str, question: str) -> str:
    lines = parse_transcript(system_prompt)
    if not lines:
        return "转录稿中没有提到相关信息。"
    question_grams = {question[i : i + 2] for i in range(max(0, len(question) - 1))}
    question_grams = {g for g in question_grams if g.strip()}
    scored: list[tuple[float, dict[str, Any]]] = []
    for line in lines:
        text = line["text"]
        grams = {text[i : i + 2] for i in range(max(0, len(text) - 1))}
        overlap = len(question_grams & grams)
        if overlap:
            scored.append((overlap / max(1, len(question_grams)), line))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return "转录稿中没有提到相关信息。"
    picked: list[dict[str, Any]] = []
    for score, line in scored:
        if score <= 0:
            continue
        if line["text"] in {item["text"] for item in picked}:
            continue
        picked.append(line)
        if len(picked) >= 3:
            break
    if not picked:
        return "转录稿中没有提到相关信息。"
    lines_md = "\n".join(
        f"- [{format_timestamp(line['start_ms'])}] {line['speaker']}：{truncate(line['text'], 100)}"
        for line in picked
    )
    return f"根据转录稿，相关内容如下：\n{lines_md}\n\n以上为转录稿中的原文，更完整的上下文可回听对应时间点。"


def _best_example(examples: dict[str, str], question: str) -> str | None:
    question_grams = _query_grams(question)
    best: tuple[float, str] | None = None
    for raw_question, answer in examples.items():
        if not isinstance(answer, str) or not answer.strip():
            continue
        candidate_grams = _query_grams(str(raw_question))
        if not question_grams or not candidate_grams:
            continue
        overlap = len(question_grams & candidate_grams) / len(question_grams | candidate_grams)
        if best is None or overlap > best[0]:
            best = (overlap, answer)
    if best is not None and best[0] >= 0.15:
        return best[1]
    return None


def _query_grams(text: str) -> set[str]:
    normalized = "".join(ch for ch in (text or "").lower() if ch.strip())
    if len(normalized) <= 1:
        return {normalized} if normalized else set()
    return {normalized[i : i + 2] for i in range(len(normalized) - 1)}


def _answer_chat(messages: list[dict[str, str]]) -> str:
    last_user = ""
    system_text = ""
    for message in messages:
        role = message.get("role")
        content = str(message.get("content") or "")
        if role == "user":
            last_user = content
        elif role == "system":
            system_text = content
    example = find_chat_example(last_user)
    if example is not None:
        return example[1]
    # Demo recording matched via its summary/context: answer with a hand-written
    # demo example when possible, otherwise fall back to its summary.
    fixture = match_recording_by_summary(system_text)
    if fixture is not None:
        examples = fixture.get("chat_examples") or {}
        if isinstance(examples, dict) and examples:
            best = _best_example({str(key): str(value) for key, value in examples.items()}, last_user)
            if best:
                return best
            overview = examples.get("帮我提炼一下重点内容")
            if isinstance(overview, str) and overview.strip():
                return overview
        summary_md = demo_insights(fixture).get("summary_md") or ""
        if summary_md:
            return truncate(str(summary_md), 400)
    return _context_answer(system_text, last_user)


class MockLLMProvider:
    name = "mock"

    def __init__(self, model: str = "mock-llm", temperature: float = 0.3) -> None:
        self.model = model or "mock-llm"
        self.temperature = temperature

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str:
        task = _task_of(messages)
        prompt = _prompt_text(messages)
        if task == "chat":
            await asyncio.sleep(0.01)
            return _answer_chat(messages)
        if task in ("minutes", "chapters", "viewpoint_tree", "suggested_questions"):
            fixture = match_recording_by_text(prompt)
            payload = _demo_payload(task, fixture) if fixture is not None else _heuristic_payload(task, prompt)
            if task == "minutes" and not payload.get("summary_md"):
                payload = _heuristic_payload(task, prompt)
            await asyncio.sleep(0.01)
            return json.dumps(payload, ensure_ascii=False)
        await asyncio.sleep(0.01)
        return json.dumps({}, ensure_ascii=False)

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        task = _task_of(messages)
        if task == "chat":
            answer = _answer_chat(messages)
        elif task:
            answer = await self.complete(messages, json_mode=True, temperature=temperature, max_tokens=max_tokens)
        else:
            answer = _answer_chat(messages)
        rng = random.Random(len(answer) * 31 + sum(ord(ch) for ch in answer[:20]))
        sizes = [24, 31, 27, 36, 22, 29, 34, 26]
        cursor = 0
        index = 0
        while cursor < len(answer):
            size = sizes[index % len(sizes)] + (rng.randint(-2, 4) if index % 3 == 0 else 0)
            size = max(20, min(40, size))
            yield answer[cursor : cursor + size]
            cursor += size
            index += 1
            await asyncio.sleep(0.02)


__all__ = [
    "MockLLMProvider",
    "parse_transcript",
    "_heuristic_minutes",
    "_heuristic_chapters",
    "_heuristic_viewpoint_tree",
    "_heuristic_questions",
]
