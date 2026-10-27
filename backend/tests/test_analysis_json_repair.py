"""Tolerant JSON extraction for LLM analysis output (v0.3.9).

Real Qwen responses occasionally contain Markdown fences, prose before/after the
JSON, trailing commas, or literal newlines inside strings. ``extract_json`` must
recover these instead of dropping the whole analysis task.
"""

from __future__ import annotations

from app.services.analysis import extract_json, sanitize_chapters


def test_extract_json_plain():
    assert extract_json('{"chapters": []}') == {"chapters": []}
    assert extract_json(None) is None
    assert extract_json("not json") is None


def test_extract_json_fence_and_prose():
    raw = '好的，下面是结果：\n```json\n{"chapters": [{"title": "a"}]}\n```\n希望有帮助。'
    assert extract_json(raw) == {"chapters": [{"title": "a"}]}


def test_extract_json_trailing_commas():
    raw = '{"chapters": [{"title": "a", "start_ms": 0, "end_ms": 1000, "bullets": ["x"],},],}'
    parsed = extract_json(raw)
    assert isinstance(parsed, dict)
    assert parsed["chapters"][0]["title"] == "a"


def test_extract_json_raw_newlines_inside_string():
    raw = (
        '{"chapters": [{"title": "第一行\n第二行", "start_ms": 0, "end_ms": 1000, '
        '"summary": "摘要第一行\n摘要第二行", "bullets": ["要点\n续行"]}]}'
    )
    parsed = extract_json(raw)
    assert isinstance(parsed, dict)
    chapters = sanitize_chapters(parsed, 10_000)
    assert chapters[0]["summary"].startswith("摘要第一行")


def test_extract_json_does_not_return_nested_empty_array():
    raw = '{"chapters": [{"title": "a", "summary": "第一行\n第二行", "bullets": []}]}'
    parsed = extract_json(raw)
    assert isinstance(parsed, dict), "a nested [] must not be mistaken for the document root"
    assert parsed["chapters"][0]["title"] == "a"


def test_extract_json_prefers_object_when_prose_contains_braces():
    raw = '说明 {不是 JSON} 真正的结果：{"chapters": [{"title": "a"}]} 结束'
    parsed = extract_json(raw)
    assert isinstance(parsed, dict)
    assert parsed["chapters"][0]["title"] == "a"
