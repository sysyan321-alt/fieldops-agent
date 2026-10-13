#!/usr/bin/env python3
# scripts/build_quick_index.py
# 从 data/sop/*.md 生成快通道索引：章节标题 → 标准问法 + 相似问法 + 关键词 + 原文片段
#
# 用法：
#   python scripts/build_quick_index.py              # 用 LLM 生成相似问法（推荐，1 篇 1 次调用）
#   python scripts/build_quick_index.py --no-llm     # 不调 LLM，用规则模板生成（离线可用）
#   python scripts/build_quick_index.py --overlay-only  # 只把人工补充问法合并进已有 entries.json（不调 LLM）
#
# data/quick_index/overlay.json 存放人工标注的补充问法/关键词：LLM 批量生成会有遗漏，
# 而这些遗漏一旦被回归用例发现（见 golden.jsonl），就在这里按 id 补，保证重新生成时不被覆盖。

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT_DIR = Path(__file__).resolve().parent.parent
SOP_DIR = ROOT_DIR / "data" / "sop"
OUT_PATH = ROOT_DIR / "data" / "quick_index" / "entries.json"
SYNONYMS_PATH = ROOT_DIR / "data" / "quick_index" / "synonyms.json"
OVERLAY_PATH = ROOT_DIR / "data" / "quick_index" / "overlay.json"

MIN_BODY_CHARS = 40          # 正文太短的章节不进索引
SNIPPET_CHARS = 400          # 返回给用户的原文片段长度

HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")


def load_synonyms() -> dict:
    if not SYNONYMS_PATH.exists():
        return {}
    data = json.loads(SYNONYMS_PATH.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def load_overlay() -> dict:
    """人工补充问法：{entry_id: {"similar_questions": [...], "keywords": [...]}}。"""
    if not OVERLAY_PATH.exists():
        return {}
    data = json.loads(OVERLAY_PATH.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def apply_overlay(entries: list[dict], overlay: dict) -> int:
    """把人工补充问法/关键词追加到对应条目（去重，追加在 LLM 生成的结果之后）。"""
    applied = 0
    for entry in entries:
        extra = overlay.get(entry.get("id"))
        if not extra:
            continue
        for field in ("similar_questions", "keywords"):
            existing = list(entry.get(field) or [])
            for item in extra.get(field) or []:
                if item not in existing:
                    existing.append(item)
            entry[field] = existing
        applied += 1
    return applied


def parse_sop(path: Path) -> list[dict]:
    """把一篇 SOP 拆成章节条目（H2 / H3 级）。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    doc_title = path.stem
    sections: list[dict] = []
    current = None
    for line in lines:
        m = HEADING_RE.match(line.strip())
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            if level == 1:
                doc_title = title
                continue
            if current:
                sections.append(current)
            current = {"level": level, "title": title, "body_lines": []}
        elif current is not None:
            current["body_lines"].append(line)
    if current:
        sections.append(current)

    entries = []
    for sec in sections:
        body = "\n".join(sec["body_lines"]).strip()
        if len(body) < MIN_BODY_CHARS:
            continue
        entries.append({
            "doc_title": doc_title,
            "section_title": sec["title"],
            "body": body,
        })
    return entries


def rule_based_augment(entry: dict, synonyms: dict) -> tuple[list[str], list[str]]:
    """不调 LLM 时的兜底：用模板生成相似问法 + 从标题抽关键词。"""
    title = entry["section_title"]
    clean = re.sub(r"^\d+(\.\d+)*\s*", "", title)
    canon = set(synonyms.values())
    keywords = [term for term in canon if term in clean or term in entry["doc_title"]][:6]
    questions = [
        f"{clean}怎么做？",
        f"{clean}要注意什么？",
        f"{clean}有哪些要求？",
        f"{entry['doc_title']}里{clean}的步骤是什么？",
        f"{clean}的标准流程",
    ]
    return questions, keywords


LLM_PROMPT = """你是项目现场作业培训老师。下面是一篇作业规范《{doc_title}》的章节列表。
请为每一节生成作业人员在手机上最可能提出的问题，以及该节的核心关键词。

要求：
1. 每节生成 5 个问题，口语化、长度 5-20 字，覆盖不同表达习惯（有人叫"配电柜"、有人叫"配电箱"）；
2. 不得改变医学含义，不得引入 SOP 里没有的操作；
3. 关键词每节 3-6 个，用规范术语（如"配电间""胃残余量""压力性损伤"）；
4. 严格输出 JSON 数组，不要解释、不要 Markdown 代码块，格式：
[{{"index": 0, "questions": ["...", "..."], "keywords": ["...", "..."]}}]

章节列表：
{sections}
"""


async def llm_augment(doc_title: str, entries: list[dict]) -> dict[int, dict]:
    """一次调用处理一篇文档的所有章节，失败返回空 dict（调用方回落模板）。"""
    from backend.core.llm_factory import get_llm

    sections_text = "\n".join(
        f"[{i}] {e['section_title']}：{e['body'][:200]}"
        for i, e in enumerate(entries)
    )
    prompt = LLM_PROMPT.format(doc_title=doc_title, sections=sections_text)
    try:
        # 复用 sop_qa 的 LLM 通道（llm_factory 只注册了四个 Agent 通道）
        llm = get_llm("sop_qa", temperature=0.2)
        resp = await llm.ainvoke(prompt)
        text = getattr(resp, "content", str(resp)).strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        data = json.loads(text)
        return {int(item["index"]): item for item in data if "index" in item}
    except Exception as e:
        print(f"  ⚠️  LLM 生成失败（{type(e).__name__}: {str(e)[:80]}），改用模板兜底")
        return {}


async def main() -> int:
    parser = argparse.ArgumentParser(description="生成 SOP 快通道索引")
    parser.add_argument("--no-llm", action="store_true", help="不调 LLM，用规则模板生成相似问法")
    parser.add_argument("--overlay-only", action="store_true",
                        help="不重新生成，只把 overlay.json 的人工补充问法合并进已有 entries.json")
    parser.add_argument("--out", default=str(OUT_PATH))
    args = parser.parse_args()

    overlay = load_overlay()

    if args.overlay_only:
        out_path = Path(args.out)
        if not out_path.exists():
            print(f"❌ {out_path} 不存在，请先运行 python scripts/build_quick_index.py")
            return 1
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        entries = payload.get("entries", [])
        applied = apply_overlay(entries, overlay)
        payload["entry_count"] = len(entries)
        payload["overlay_applied"] = applied
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✅ 合并人工补充问法：{applied} 条 → {out_path}")
        return 0

    synonyms = load_synonyms()
    all_entries: list[dict] = []

    for md in sorted(SOP_DIR.glob("*.md")):
        category = md.stem
        sections = parse_sop(md)
        if not sections:
            continue
        print(f"▶ {md.name}：{len(sections)} 个章节")
        augmented = {} if args.no_llm else await llm_augment(sections[0]["doc_title"], sections)
        for idx, sec in enumerate(sections):
            questions, keywords = rule_based_augment(sec, synonyms)
            item = augmented.get(idx) or {}
            questions = item.get("questions") or questions
            keywords = item.get("keywords") or keywords
            all_entries.append({
                "id": f"{category}-{idx:02d}",
                "tenant_id": "tenant_default",
                "category": category,
                "doc_title": sec["doc_title"],
                "section_title": sec["section_title"],
                "canonical_question": f"{sec['doc_title']}中「{sec['section_title']}」的要求是什么？",
                "similar_questions": questions[:6],
                "keywords": keywords[:6],
                "snippet": sec["body"][:SNIPPET_CHARS],
                "source": f"{category} > {sec['doc_title']} > {sec['section_title']}",
            })

    apply_overlay(all_entries, overlay)

    payload = {
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "generator": "rule" if args.no_llm else "llm",
        "entry_count": len(all_entries),
        "entries": all_entries,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ 共 {len(all_entries)} 条 → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
