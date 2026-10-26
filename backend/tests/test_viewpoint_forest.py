"""Viewpoint forest (v0.3.4): long meetings produce multiple independent topic trees."""

from __future__ import annotations

import pytest

from app.api.insights import _parse_viewpoint_tree
from app.services.analysis import AnalysisError, sanitize_viewpoint_tree


def _forest_payload() -> dict:
    return {
        "roots": [
            {
                "id": "trace",
                "label": "Trace 采集方案对比",
                "detail": "比较预测式 eBPF 与静态 JIT 插装",
                "kind": "topic",
                "time_ms": 1000,
                "speaker": "吴克强",
                "children": [
                    {
                        "label": "预测式 eBPF 开销更低",
                        "detail": "仅关键路径采样，避免全量插装",
                        "kind": "point",
                        "time_ms": 4200,
                        "speaker": "吴克强",
                        "children": [{"label": "硬件事件采样", "kind": "point"}],
                    }
                ],
            },
            {
                "id": "icpe",
                "label": "ICPE 流程与风险",
                "detail": "讨论 ICPE 流程局限与后续验证",
                "kind": "topic",
                "time_ms": 80000,
                "children": [
                    {"label": "随机程序预测效果差", "detail": "仍需传统手段辅助", "kind": "risk"},
                    {"label": "补齐实验评估维度", "detail": "下周对齐", "kind": "task"},
                ],
            },
        ]
    }


def test_sanitize_viewpoint_forest():
    result = sanitize_viewpoint_tree(
        _forest_payload(), 3_600_000, speakers=["吴克强", "王品呈"]
    )
    roots = result["roots"]
    assert len(roots) == 2
    assert [root["id"] for root in roots] == ["tree-1", "tree-2"]
    assert all(root["kind"] == "topic" for root in roots)
    assert roots[0]["children"][0]["kind"] == "point"
    assert roots[1]["children"][1]["kind"] == "task"
    # nested third level kept, ids namespaced by tree root
    assert roots[0]["children"][0]["children"][0]["label"] == "硬件事件采样"
    # time clamped to the recording duration
    assert roots[1]["children"][1].get("time_ms") is None
    assert roots[1]["time_ms"] == 80000


def test_sanitize_legacy_single_root_still_works():
    legacy = {
        "root": {
            "label": "会议主题",
            "children": [
                {"label": "主题一", "children": [{"label": "观点一"}]},
                {"label": "主题二", "children": []},
            ],
        }
    }
    result = sanitize_viewpoint_tree(legacy, 60_000)
    assert "root" in result and "roots" not in result
    assert result["root"]["children"][0]["label"] == "主题一"


def test_sanitize_trees_alias_and_limits():
    payload = {"trees": _forest_payload()["roots"] * 3}
    result = sanitize_viewpoint_tree(payload, 100_000)
    assert 1 <= len(result["roots"]) <= 24
    assert len({root["id"] for root in result["roots"]}) == len(result["roots"])


def test_sanitize_empty_forest_raises():
    with pytest.raises(AnalysisError):
        sanitize_viewpoint_tree({"roots": []}, 1000)
    with pytest.raises(AnalysisError):
        sanitize_viewpoint_tree({}, 1000)


def test_api_parser_returns_forest():
    parsed = _parse_viewpoint_tree(_forest_payload())
    assert parsed is not None
    assert parsed.root is None
    assert len(parsed.roots) == 2
    assert parsed.roots[0].label == "Trace 采集方案对比"
    assert parsed.roots[1].children[1].kind == "task"

    single = _parse_viewpoint_tree({"roots": [_forest_payload()["roots"][0]]})
    assert single is not None and single.root is not None and len(single.roots) == 1

    legacy = _parse_viewpoint_tree({"root": {"label": "主题", "children": []}})
    assert legacy is not None and legacy.root is not None and legacy.roots == []
