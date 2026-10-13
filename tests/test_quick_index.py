# tests/test_quick_index.py
# 快通道回归测试：命中 / 回落 / 误命中三件事，不依赖数据库与模型。
#
# 为什么单独测：快通道的价值全在"敢不敢短路"这个平衡上——
# 命中率掉下去只是慢一点，**误命中会直接给错答案**，所以这里把两个契约钉死：
#
#   ① 负样本（该走 RAG 的）一条都不许命中          ← 硬契约，全量断言
#   ② 调参集（golden）里该命中快通道的必须命中      ← 硬契约，已针对它调过阈值与相似问法
#
# 留出集（golden_holdout）**刻意不做硬断言**，只做"下限测量"：
# 它存在的意义是回答"换个问法还灵不灵"。一旦拿它调参、或把它改成必须全中，
# 它就退化成第二个调参集，再也没有泛化能力的读数了。
# 当前读数见 scripts/evaluate_quick_channel.py 的输出。

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


GOLDEN = _load("golden.jsonl")
HOLDOUT = _load("golden_holdout.jsonl")

GOLDEN_QUICK = [c for c in GOLDEN if c["expect_mode"] == "quick"]
NEGATIVE = [c for c in GOLDEN + HOLDOUT if c["expect_mode"] == "rag"]
HOLDOUT_QUICK = [c for c in HOLDOUT if c["expect_mode"] == "quick"]


@pytest.fixture(scope="module")
def index():
    return get_quick_index()


def test_index_loaded(index):
    """索引必须真的加载到条目——否则下面的断言会因为"永远回落"而假通过。"""
    assert len(index.entries) >= 60
    assert all(entry.get("similar_questions") for entry in index.entries)


# ── ① 硬契约：该命中快通道的必须命中（调参集）──────────────────

@pytest.mark.parametrize("case", GOLDEN_QUICK, ids=[c["query"] for c in GOLDEN_QUICK])
def test_golden_quick_cases_must_hit(index, case):
    result = index.search(case["query"])
    assert result["hit"] is not None, \
        f"「{case['query']}」应命中快通道，实际回落：{result['reason']}"
    if case.get("expect_category"):
        assert result["hit"]["category"] == case["expect_category"], \
            f"「{case['query']}」命中了 {result['hit']['category']}，期望 {case['expect_category']}"


# ── ② 硬契约：负样本一条都不许命中（调参集 + 留出集）────────────

@pytest.mark.parametrize("case", NEGATIVE, ids=[c["query"] for c in NEGATIVE])
def test_must_not_hit_quick_channel(index, case):
    """宁可回落到 RAG，也不许猜一个章节给作业人员。"""
    result = index.search(case["query"])
    assert result["hit"] is None, \
        f"「{case['query']}」误命中 {result['hit']['section_title']}"


# ── ③ 留出集：只做下限测量，不做硬门 ──────────────────────────

def test_holdout_hit_rate_has_a_floor(index):
    """留出集命中率只设下限（防回归），不设 100%——它的用途是泛化读数。

    下限取 50%：低于这个数说明索引的相似问法覆盖出现明显退化，值得人看一眼。
    """
    hits = [c for c in HOLDOUT_QUICK if index.search(c["query"])["hit"] is not None]
    rate = len(hits) / max(len(HOLDOUT_QUICK), 1)
    misses = [c["query"] for c in HOLDOUT_QUICK if index.search(c["query"])["hit"] is None]
    assert rate >= 0.5, (
        f"留出集命中率降到 {rate:.0%}（{len(hits)}/{len(HOLDOUT_QUICK)}），未命中：{misses}"
    )


def test_holdout_misrouted_sections_do_not_regress(index):
    """留出集允许有未命中，但**不许给错章节**——答错比不答严重。

    当前已知 1 条：「轴承温度多少算异常」会命中配电间那节的温度判据
    （两份规范都写了温度阈值）。这条不靠改问句掩盖，等结构性手段解决：
    给条目加"适用范围"字段，让不同设备的同名判据可区分。
    """
    wrong = []
    for c in HOLDOUT_QUICK:
        res = index.search(c["query"])
        if res["hit"] and c.get("expect_category") \
                and res["hit"]["category"] != c["expect_category"]:
            wrong.append(f"{c['query']} → {res['hit']['category']}")
    assert len(wrong) <= 1, f"章节错变多了：{wrong}"


# ── ④ 命中质量 ────────────────────────────────────────────────

def test_quick_hit_carries_source_and_is_fast(index):
    """命中时必须带来源章节（回答要能溯源），且判定耗时在毫秒级。"""
    result = index.search("灭火器压力表在哪个区间正常")
    assert result["hit"] is not None
    assert result["hit"]["source"] and result["hit"]["snippet"]
    assert result["elapsed_ms"] < 50
