# tests/test_quick_index.py
# SOP 快通道回归测试：命中 / 回落 / 误命中三件事，不依赖数据库与模型。
#
# 为什么单独测：快通道的价值全在"敢不敢短路"这个平衡上——
# 命中率掉下去只是慢一点，误命中会直接给错答案，所以下面用 golden 集把
# "该走快通道的必须命中、该走 RAG 的一条都不许命中"钉死在测试里。

import json
from pathlib import Path

import pytest

from backend.core.quick_index import get_quick_index

ROOT_DIR = Path(__file__).resolve().parent.parent
GOLDEN_DIR = ROOT_DIR / "data" / "quick_index"


def _load(name: str) -> list[dict]:
    path = GOLDEN_DIR / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


GOLDEN_CASES = _load("golden.jsonl") + _load("golden_holdout.jsonl")
QUICK_CASES = [c for c in GOLDEN_CASES if c["expect_mode"] == "quick"]
RAG_CASES = [c for c in GOLDEN_CASES if c["expect_mode"] == "rag"]


@pytest.fixture(scope="module")
def index():
    return get_quick_index()


def test_index_loaded(index):
    """索引必须真的加载到条目——否则下面的断言会因为"永远回落"而假通过。"""
    assert len(index.entries) >= 60
    assert all(entry.get("similar_questions") for entry in index.entries)


@pytest.mark.parametrize("case", QUICK_CASES, ids=[c["query"] for c in QUICK_CASES])
def test_should_hit_quick_channel(index, case):
    result = index.search(case["query"])
    assert result["hit"] is not None, f"「{case['query']}」应命中快通道，实际回落：{result['reason']}"
    if case.get("expect_category"):
        assert result["hit"]["category"] == case["expect_category"]


@pytest.mark.parametrize("case", RAG_CASES, ids=[c["query"] for c in RAG_CASES])
def test_must_not_hit_quick_channel(index, case):
    """负样本：宁可回落到 RAG，也不许猜一个章节给护理员。"""
    result = index.search(case["query"])
    assert result["hit"] is None, f"「{case['query']}」误命中 {result['hit']['section_title']}"


def test_quick_hit_carries_source_and_is_fast(index):
    """命中时必须带来源章节（回答要能溯源），且判定耗时在毫秒级。"""
    result = index.search("压疮分几期")
    assert result["hit"] is not None
    assert result["hit"]["source"] and result["hit"]["snippet"]
    assert result["elapsed_ms"] < 50
