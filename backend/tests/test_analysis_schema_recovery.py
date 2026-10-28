"""LLM analysis schema-correction + graceful fallback tests (v0.3.14)."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db import Base, SessionLocal, init_db
from app.models import Recording, Speaker, TranscriptSegment
from app.services import analysis as analysis_service
from app.services.prompts import DEFAULT_PROMPTS


@pytest.fixture()
def database():
    init_db()
    with SessionLocal() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    with SessionLocal() as session:
        yield session


def _seed_transcript(db) -> Recording:
    recording = Recording(
        uuid="schema-recovery-test",
        title="Schema 恢复测试",
        status="ready",
        stage="done",
        progress=100,
        storage_dir="media/schema-recovery-test",
        media_type="audio",
        language="zh",
        duration_ms=60_000,
    )
    db.add(recording)
    db.commit()
    db.refresh(recording)
    speaker = Speaker(recording_id=recording.id, label="说话人1", display_name="吴克强")
    db.add(speaker)
    db.commit()
    db.refresh(speaker)
    for idx, text in enumerate(
        [
            "我们先确认摘要两周内投出，正文再三周。",
            "实验评估维度下周对齐，图表规划同时推进。",
            "今天先到这里，风险项后续验证。",
        ]
    ):
        db.add(
            TranscriptSegment(
                recording_id=recording.id,
                idx=idx,
                speaker_id=speaker.id,
                start_ms=idx * 15_000,
                end_ms=(idx + 1) * 15_000,
                text=text,
            )
        )
    db.commit()
    return recording


class FakeLLM:
    """Returns per-task JSON; counts calls and captures the system prompt."""

    def __init__(self, responses):
        self.responses = responses
        self.calls: list[str] = []

    async def complete(self, messages, *, json_mode=False, temperature=0.3, max_tokens=4096):
        system = messages[0]["content"] if messages else ""
        task = (re.search(r"TASK=([a-z_]+)", system) or [None, "unknown"])[1]
        self.calls.append(task)
        return json.dumps(self.responses(task, self.calls.count(task)), ensure_ascii=False)


def _config():
    return SimpleNamespace(
        llm=SimpleNamespace(provider="fake", model="fake-model", temperature=0.0),
        prompts=dict(DEFAULT_PROMPTS),
    )


VALID = {
    "chapters": {
        "chapters": [
            {"title": "进度与计划", "start_ms": 0, "end_ms": 30_000, "summary": "摘要", "bullets": ["两周"]},
            {"title": "实验评估", "start_ms": 30_000, "end_ms": 60_000, "summary": "评估", "bullets": ["下周"]},
        ]
    },
    "viewpoint_tree": {
        "roots": [
            {"label": "进度", "detail": "交付计划", "kind": "topic", "children": [{"label": "摘要两周", "kind": "point"}]}
        ]
    },
    "suggested_questions": {"questions": ["摘要什么时候投？"]},
}


async def test_minutes_schema_retry_then_fallback_summary(monkeypatch, database):
    recording = _seed_transcript(database)

    def responses(task, count):
        if task == "minutes":
            # Valid JSON but missing the required summary_md structure every time.
            return {"insights": [{"title": "进度", "content": "摘要两周", "category": "decision"}]}
        return VALID[task]

    fake = FakeLLM(responses)
    monkeypatch.setattr("app.providers.registry.build_llm_provider", lambda _cfg: fake)
    result = await analysis_service.generate_analyses(database, recording, _config())

    assert fake.calls.count("minutes") == 2, "minutes task should get one schema-correction retry"
    assert "minutes" in result["failed"]
    summary = database.scalar(
        select(analysis_service.Analysis).where(
            analysis_service.Analysis.recording_id == recording.id,
            analysis_service.Analysis.kind == "summary",
        )
    )
    assert summary is not None
    payload = json.loads(summary.payload_json)
    assert "AI 模型未返回完整的结构化纪要" in payload["markdown"]
    assert "摘要两周内投出" in payload["markdown"]
    # Other analyses still succeed and the recording remains usable.
    assert "chapters" in result["written"]
    assert "viewpoint_tree" in result["written"]


async def test_chapters_schema_retry_uses_corrected_structure(monkeypatch, database):
    recording = _seed_transcript(database)
    attempts = {"n": 0}

    def responses(task, count):
        if task == "minutes":
            return {"summary_md": "## 会议概述\n确定摘要两周、正文三周。"}
        if task == "chapters":
            attempts["n"] += 1
            if attempts["n"] == 1:
                return {"chapters": []}  # validates as empty -> schema retry
            return {
                "outline": [
                    {
                        "heading": "进度确认",
                        "start_time": 0,
                        "end_time": 30_000,
                        "description": "确认摘要与正文时间",
                        "key_points": ["摘要两周", "正文三周"],
                    }
                ]
            }
        return VALID[task]

    fake = FakeLLM(responses)
    monkeypatch.setattr("app.providers.registry.build_llm_provider", lambda _cfg: fake)
    result = await analysis_service.generate_analyses(database, recording, _config())

    assert fake.calls.count("chapters") == 2
    assert "chapters" in result["written"]
    chapter_row = database.scalar(
        select(analysis_service.Analysis).where(
            analysis_service.Analysis.recording_id == recording.id,
            analysis_service.Analysis.kind == "chapters",
        )
    )
    assert chapter_row is not None
    chapters = json.loads(chapter_row.payload_json)
    assert chapters[0]["title"] == "进度确认"
    assert chapters[0]["bullets"] == ["摘要两周", "正文三周"]


def test_summary_accepts_nested_wrappers():
    assert analysis_service.sanitize_summary({"summary": {"markdown": "# 概述\n内容"}}) == {
        "markdown": "# 概述\n内容"
    }
    assert analysis_service.sanitize_summary({"summary_md": "纯文本"}) == {"markdown": "纯文本"}
    assert analysis_service.sanitize_summary("## 直接字符串") == {"markdown": "## 直接字符串"}
    assert analysis_service.sanitize_summary({"unrelated": "x"}) is None
