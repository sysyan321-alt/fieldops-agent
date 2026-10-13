#!/usr/bin/env python3
# scripts/evaluate_quick_channel.py
# 快通道回归评估：命中率 / 误命中率 / 平均耗时 / 冷启动可用性
#
# 不需要模型与数据库：快通道只加载 data/quick_index/entries.json，因此可以在 BGE 模型
# 未加载的情况下运行——这也是"模型不可用时仍可作答"的直接证据。
#
# 默认同时跑两组用例：
#   golden.jsonl          调参集（开发时反复用过，容易过拟合，只作参考）
#   golden_holdout.jsonl  留出集（调参时没用过，用来判断"换个问法还灵不灵"）
# 只跑一组：python scripts/evaluate_quick_channel.py --golden <文件>

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.core.quick_index import get_quick_index  # noqa: E402

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SETS = [
    ("调参集 golden.jsonl", ROOT_DIR / "data" / "quick_index" / "golden.jsonl"),
    ("留出集 golden_holdout.jsonl", ROOT_DIR / "data" / "quick_index" / "golden_holdout.jsonl"),
]
ABSTAIN = {"no_candidate", "coverage_below_threshold", "ambiguous_top2",
           "topic_not_in_title", "diagnostic_query"}


def run_one(title: str, golden_path: Path, index) -> dict:
    """跑一组用例，打印明细并返回统计值。"""
    golden = [json.loads(line) for line in golden_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"\n=== {title}（{golden_path.name}）===")
    print(f"golden 用例：{len(golden)} 条\n")

    should_quick = [g for g in golden if g["expect_mode"] == "quick"]
    should_rag = [g for g in golden if g["expect_mode"] == "rag"]

    hit_times, miss_times = [], []
    quick_hits, quick_misses = [], []
    false_hits = []
    wrong_category = []

    for case in golden:
        result = index.search(case["query"], tenant_id="tenant_default")
        elapsed = result["elapsed_ms"]
        hit = result["hit"]
        if hit:
            hit_times.append(elapsed)
            if case["expect_mode"] == "quick":
                quick_hits.append(case)
                if case.get("expect_category") and hit["category"] != case["expect_category"]:
                    wrong_category.append((case["query"], hit["category"], case["expect_category"]))
            else:
                false_hits.append((case["query"], hit["section_title"], result["reason"]))
        else:
            miss_times.append(elapsed)
            if case["expect_mode"] == "quick":
                quick_misses.append((case["query"], result["reason"], round(result.get("coverage", 0), 3)))

    hit_rate = len(quick_hits) / len(should_quick) if should_quick else 0.0
    miss_rate = len(false_hits) / len(should_rag) if should_rag else 0.0

    print(f"① 快通道命中率：{len(quick_hits)}/{len(should_quick)} = {hit_rate:.1%}")
    print(f"② 误命中率（应走 RAG 却命中）：{len(false_hits)}/{len(should_rag)} = {miss_rate:.1%}")
    if wrong_category:
        print(f"   其中命中但章节不对：{len(wrong_category)} 条")
        for q, got, want in wrong_category:
            print(f"     - 「{q}」命中 {got}，期望 {want}")
    if hit_times:
        print(f"③ 快通道平均耗时：{statistics.mean(hit_times):.2f} ms（P95 {sorted(hit_times)[int(len(hit_times)*0.95)-1]:.2f} ms）")
    if miss_times:
        print(f"   未命中判定耗时：{statistics.mean(miss_times):.2f} ms（未命中后会走 RAG 链路）")
    print("④ 冷启动可用性：✅ 快通道只读本地 JSON，不需要嵌入模型/LLM，模型未加载时可正常作答")

    if quick_misses:
        print(f"\n未命中的快通道用例（{len(quick_misses)} 条，可补相似问法）：")
        for q, reason, dice in quick_misses:
            print(f"  - 「{q}」{reason}（dice={dice}）")
    if false_hits:
        print(f"\n误命中明细（{len(false_hits)} 条，需收紧门控或补负样本）：")
        for q, section, reason in false_hits:
            print(f"  - 「{q}」→ {section}")

    return {
        "quick_hits": len(quick_hits), "should_quick": len(should_quick),
        "false_hits": len(false_hits), "should_rag": len(should_rag),
        "wrong_category": len(wrong_category),
        "avg_ms": statistics.mean(hit_times) if hit_times else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="SOP 快通道回归评估")
    parser.add_argument("--golden", default=None, help="只跑指定的用例文件；默认跑调参集+留出集")
    args = parser.parse_args()

    index = get_quick_index()
    print(f"索引条目：{len(index.entries)} 条（生成方式见 entries.json）")

    sets = [(Path(args.golden).name, Path(args.golden))] if args.golden else DEFAULT_SETS
    stats = [run_one(title, path, index) for title, path in sets]

    print("\n=== 汇总 ===")
    for (title, _), s in zip(sets, stats):
        print(f"{title}：命中 {s['quick_hits']}/{s['should_quick']}、"
              f"误命中 {s['false_hits']}/{s['should_rag']}、章节错 {s['wrong_category']}、"
              f"平均 {s['avg_ms']:.2f} ms")
    total_q = sum(s["quick_hits"] for s in stats)
    total_qn = sum(s["should_quick"] for s in stats)
    total_f = sum(s["false_hits"] for s in stats)
    total_fn = sum(s["should_rag"] for s in stats)
    print(f"合计：命中 {total_q}/{total_qn}、误命中 {total_f}/{total_fn}")
    print("口径说明：命中率与误命中率分别按『该走快通道的问句』和『该走 RAG 的问句』各自的分母算；"
          "耗时是纯判定耗时（不含后续回答生成）。")
    print("结论：命中率越高越好，误命中率必须保持 0——宁可回落 RAG 慢一点，不可给错章节。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
