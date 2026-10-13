# backend/core/quick_index.py
# SOP 快通道索引：术语规范化 → BM25 打分 → 词重叠门控
#
# 定位：位于"精确缓存"与"RAG 检索"之间的快通道。命中标准问法时直接返回 SOP 原文与来源章节，
# 不调用嵌入模型与 LLM，因此（1）省掉精排与生成开销；（2）模型未加载/不可用时仍可作答。
#
# 索引数据由 scripts/build_quick_index.py 从 data/sop/*.md 生成（含 LLM 生成的相似问法）。

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Optional

from backend.core.logger import get_logger

logger = get_logger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
ENTRIES_PATH = ROOT_DIR / "data" / "quick_index" / "entries.json"
SYNONYMS_PATH = ROOT_DIR / "data" / "quick_index" / "synonyms.json"

# 门控参数：查询词覆盖率过低、或 Top1/Top2 差距过小，都判为"没把握"，交回 RAG
# 说明：不能用 Dice 类对称指标——索引条目是多句合并的长文本，分母过大必然压低分数；
# 覆盖率（查询 token 被文档覆盖的比例）才是这类"短查询 vs 长条目"的正确判据。
MIN_COVERAGE = 0.35
MIN_MARGIN = 1.25
# Top1 与 Top2 的分数差距足够大时（≥3 倍），即使覆盖率不高也认这条命中：
# 「卫生间要不要装扶手」这类口语问法会插入"要不要"等词，字面覆盖率天然偏低，
# 但只要候选之间差得足够远，就说明问的就是这一节，可以安全短路。
# 阈值定 3.0 是被留出集逼出来的：「怎么跟家属解释他瘦了」对家属教育那节 margin 2.08，
# 2.0 会放行、3.0 会挡住——留着这条误命中会抵消快通道最大的卖点（不误答）。
BIG_MARGIN = 3.0
BM25_K1 = 1.5
BM25_B = 0.75

# 章节标题里的核心词也是主题词（如「5.1 漏服」→「漏服」）。
# 同义词表只覆盖口语↔术语的映射，靠它拿不到这类标题词，
# 会出现"漏服一次药要不要补吃"明明命中标题却因覆盖率太低被回落的情况。
TITLE_SPLIT_RE = re.compile(r"^\d+(?:\.\d+)*[\.、\s]*|[、，,。；;（）()／/与和及\s]+")

# 高危问法：命中就强制走慢链路，不让快通道用"一节 SOP 原文"打发。
# 判据偏保守没关系——误判的代价只是多等几秒，漏判的代价可能是给出一条不完整的应急处置。
# 注意不要把「误吸」「跌倒」这类既是紧急情况又是常规作业话题的词单独放进来，
# 那样会把"误吸怎么预防"这种正常 SOP 提问也拦掉。
HIGH_RISK_KEYWORDS: tuple[str, ...] = (
    # 电气与中毒
    "触电", "高压电弧", "中毒", "一氧化碳", "硫化氢", "气体泄漏", "燃气泄漏",
    # 火灾与爆炸
    "着火", "起火", "火警", "浓烟", "爆炸", "爆燃",
    # 结构与受困
    "坍塌", "倒塌", "坠落", "被压住", "被埋", "被困", "无法撤离",
    # 人身伤害
    "大出血", "出血不止", "停止呼吸", "昏迷",
    # 水患
    "涌水", "水淹",
)


def is_high_risk(query: str) -> bool:
    """判断问句是否属于高危/应急情形（命中任一关键词即为真）。"""
    text = query or ""
    return any(kw in text for kw in HIGH_RISK_KEYWORDS)


# 诊断/归因类问法：**字面命中也不放行**。
#
# 为什么需要这一层：快通道返回的是某节规范的**原文（怎么做）**，而这类问句问的是
# 「为什么会这样 / 是不是这个原因」。答非所问比慢几秒代价大得多——
# 回归集里「最近总是跳闸，是不是线路老化」会字面命中「跳闸」那节、
# 「上次进水的地方又漏了」会命中「进水与受潮」那节，但两节讲的都是处置步骤，
# 不是归因分析。所以这类问句一律回落慢链路，由 LLM 结合上下文回答。
DIAGNOSTIC_KEYWORDS: tuple[str, ...] = (
    "是不是", "为什么", "为啥", "怎么会", "怎么回", "什么原因", "原因是", "什么情况",
    "老是", "总是", "反复", "又漏", "又坏", "又响", "又跳", "又堵", "又出",
    "严重吗", "要紧吗", "有没有问题", "正常吗",
)


def is_diagnostic(query: str) -> bool:
    """问句是否属于诊断/归因类（求解释而非求步骤）。"""
    text = query or ""
    return any(kw in text for kw in DIAGNOSTIC_KEYWORDS)


def _char_ngrams(text: str, n: int = 2) -> list[str]:
    """中文按 2-gram 切分（不引入分词依赖，短文本效果足够）。"""
    cleaned = "".join(ch for ch in text if ch.strip())
    if len(cleaned) < n:
        return [cleaned] if cleaned else []
    return [cleaned[i:i + n] for i in range(len(cleaned) - n + 1)]


class QuickIndex:
    """SOP 快通道索引（进程内单例，数据量小，直接内存计算 BM25）。"""

    def __init__(self, entries_path: Path = ENTRIES_PATH, synonyms_path: Path = SYNONYMS_PATH):
        self.entries: list[dict] = []
        self.synonyms: dict[str, str] = {}
        self.terms: list[str] = []
        self._docs_tokens: list[list[str]] = []
        self._df: dict[str, int] = {}
        self._avg_len = 0.0
        self.loaded_at = 0.0
        self.load(entries_path, synonyms_path)

    # ── 加载与预处理 ──────────────────────────────────────────

    def load(self, entries_path: Path = ENTRIES_PATH, synonyms_path: Path = SYNONYMS_PATH) -> None:
        self.entries = []
        if entries_path.exists():
            raw = json.loads(entries_path.read_text(encoding="utf-8"))
            self.entries = raw.get("entries", raw if isinstance(raw, list) else [])
        else:
            logger.warning("quick_index.entries_missing", path=str(entries_path))

        self.synonyms = {}
        if synonyms_path.exists():
            data = json.loads(synonyms_path.read_text(encoding="utf-8"))
            self.synonyms = {k: v for k, v in data.items() if not k.startswith("_")}

        # 术语表：同义词的键与值都视为领域术语（用于分词时优先切出）
        term_set = set(self.synonyms) | set(self.synonyms.values())
        self.terms = sorted(term_set, key=len, reverse=True)
        self._title_phrases: list[list[str]] = [
            self._title_terms_of(e.get("section_title", "")) for e in self.entries
        ]
        # 只保留"语料里独一份"的标题词作为主题词：像"记录""上报"这类词在多个章节标题里
        # 都出现（positioning「6. 记录」、pressure_ulcer「6. 记录与上报」），
        # 拿它当主题词会把"帮我把服务记录整理成报告"这种任务型问句也短路掉。
        phrase_df: dict[str, int] = {}
        for phrases in self._title_phrases:
            for p in set(phrases):
                phrase_df[p] = phrase_df.get(p, 0) + 1
        self._topic_phrases: list[list[str]] = [
            [p for p in phrases if phrase_df.get(p, 0) == 1] for phrases in self._title_phrases
        ]

        self._docs_tokens = [self.tokenize(self._entry_text(e)) for e in self.entries]
        self._df = {}
        for tokens in self._docs_tokens:
            for token in set(tokens):
                self._df[token] = self._df.get(token, 0) + 1
        self._avg_len = (
            sum(len(t) for t in self._docs_tokens) / len(self._docs_tokens)
            if self._docs_tokens else 0.0
        )
        self.loaded_at = time.time()
        logger.info("quick_index.loaded", entries=len(self.entries), terms=len(self.terms))

    @staticmethod
    def _title_terms_of(section_title: str) -> list[str]:
        """把章节标题切成可直接比对的主题词（去掉编号，按顿号/与/和等切开）。"""
        cleaned = TITLE_SPLIT_RE.sub(" ", section_title or "")
        return [p for p in cleaned.split() if len(p) >= 2]

    @staticmethod
    def _entry_text(entry: dict) -> str:
        parts = [
            entry.get("canonical_question", ""),
            entry.get("section_title", ""),
            entry.get("doc_title", ""),
            " ".join(entry.get("similar_questions", []) or []),
            " ".join(entry.get("keywords", []) or []),
        ]
        return " ".join(p for p in parts if p)

    # ── 规范化与分词 ──────────────────────────────────────────

    def normalize(self, text: str) -> str:
        """把口语/简称替换成规范术语（最长匹配优先）。"""
        out = text or ""
        for term in self.terms:
            if term in out and self.synonyms.get(term):
                out = out.replace(term, self.synonyms[term])
        return out

    def tokenize(self, text: str) -> list[str]:
        """术语 + 中文 2-gram + 英文数字，构成检索 token。"""
        text = self.normalize(text or "")
        tokens: list[str] = []
        for term in self.terms:                     # 领域术语优先（长度降序）
            if term in text:
                tokens.append(term)
        tokens.extend(_char_ngrams(text, 2))
        for token in _char_ngrams(text, 3):
            if token.isascii():
                tokens.append(token)
        return tokens

    # ── 检索 ──────────────────────────────────────────────────

    @staticmethod
    def _dice(a: set[str], b: set[str]) -> float:
        if not a or not b:
            return 0.0
        return 2 * len(a & b) / (len(a) + len(b))

    @staticmethod
    def _coverage(query_tokens: set[str], doc_tokens: set[str]) -> float:
        """查询词覆盖率：查询里有多少比例的 token 在文档中出现。"""
        if not query_tokens:
            return 0.0
        return len(query_tokens & doc_tokens) / len(query_tokens)

    @staticmethod
    def _keyword_topic_hit(keywords: list[str], query_tokens: set[str], threshold: float = 0.6) -> bool:
        """章节关键词是否被查询"基本覆盖"（按 2-gram）。

        为什么不能只做字符串包含：作业人员的口语问法和术语常有插入词，
        例如关键词「卫生间扶手」对上问句「卫生间要不要装扶手」——字面不连续，
        但 4 个 2-gram 里命中 3 个（卫生 / 生间 / 扶手），足以判定问的就是这一节。
        """
        for kw in keywords or []:
            if len(kw) < 2:
                continue
            kw_tokens = set(_char_ngrams(kw, 2))
            if not kw_tokens:
                continue
            if len(kw_tokens & query_tokens) / len(kw_tokens) >= threshold:
                return True
        return False

    def _bm25(self, query_tokens: list[str], doc_index: int) -> float:
        tokens = self._docs_tokens[doc_index]
        if not tokens:
            return 0.0
        length = len(tokens)
        tf: dict[str, int] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        total_docs = max(len(self._docs_tokens), 1)
        score = 0.0
        for token in query_tokens:
            if token not in tf:
                continue
            df = self._df.get(token, 0) or 1
            idf = math.log(1 + (total_docs - df + 0.5) / (df + 0.5))
            freq = tf[token]
            denom = freq + BM25_K1 * (1 - BM25_B + BM25_B * length / (self._avg_len or 1))
            score += idf * freq * (BM25_K1 + 1) / denom
        return score

    def search(
        self,
        query: str,
        tenant_id: str = "tenant_default",
        category: Optional[str] = None,
        min_coverage: float = MIN_COVERAGE,
        min_margin: float = MIN_MARGIN,
    ) -> dict:
        """快通道检索：返回 {hit, score, dice, margin, reason}。

        hit 为 None 表示"没把握"，调用方应回落到 RAG 链路（宁可回落，不可误答）。
        """
        started = time.perf_counter()

        # 门控 ⓪：诊断/归因类问法直接交回慢链路。
        # 放在最前面——这类问句即使字面高度匹配某节标题，那节给的也是步骤而不是归因，
        # 返回原文属于"答非所问"，违背快通道"宁可回落不可误答"的原则。
        if is_diagnostic(query):
            logger.info("quick_index.bypassed_diagnostic", query=(query or "")[:40])
            return {"hit": None, "score": 0.0, "dice": 0.0, "margin": 0.0,
                    "reason": "diagnostic_query", "coverage": 0.0,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}

        query_tokens = self.tokenize(query)
        query_set = set(query_tokens)
        # 主题门控用**原始问句**的字面：规范化的作用是拉高召回（把"设备温升"并到"压力性损伤"），
        # 但拿规范化后的问句去比对标题会把口语说法本身抹掉（如"血糖高"→"血糖监测高"），
        # 反而制造误命中，所以门控一律看用户原话。
        query_ngrams = set(_char_ngrams(query or "", 2))

        scored: list[tuple[float, float, int]] = []
        for idx, entry in enumerate(self.entries):
            if tenant_id and entry.get("tenant_id") and entry["tenant_id"] != tenant_id:
                continue
            if category and entry.get("category") and entry["category"] != category:
                continue
            score = self._bm25(query_tokens, idx)
            if score <= 0:
                continue
            doc_set = set(self._docs_tokens[idx])
            dice = self._dice(query_set, doc_set)
            coverage = self._coverage(query_set, doc_set)
            scored.append((score, coverage, dice, idx))

        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        if not scored:
            return {"hit": None, "score": 0.0, "dice": 0.0, "margin": 0.0,
                    "reason": "no_candidate", "coverage": 0.0, "elapsed_ms": elapsed_ms}

        scored.sort(key=lambda x: x[0], reverse=True)
        top_score, top_cov, top_dice, top_idx = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        margin = (top_score / second_score) if second_score > 0 else float("inf")

        top_entry = self.entries[top_idx]
        title_scope = f"{top_entry.get('doc_title', '')}{top_entry.get('section_title', '')}"
        title_terms = [t for t in self.terms if t in title_scope]
        title_hit = (
            any(t in query for t in title_terms)
            or any(p in query for p in self._topic_phrases[top_idx])
            or self._keyword_topic_hit(top_entry.get("keywords") or [], query_ngrams)
        )

        # 覆盖率兜不住时（口语问句里"要不要/怎么办"这类词拉低覆盖），
        # 只有当用户原话里出现了该节标题的核心词（如"漏服一次药要不要补吃"里的"漏服"）
        # 才放行——字面命中标题是强信号，比分数差距可靠。
        if top_cov < min_coverage and not any(p in query for p in self._topic_phrases[top_idx]):
            return {"hit": None, "score": top_score, "coverage": top_cov, "dice": top_dice,
                    "margin": margin, "reason": "coverage_below_threshold", "elapsed_ms": elapsed_ms}

        # 误命中修正：覆盖率不够高、且"主题术语没出现在该节标题/文档名"时一律不短路。
        # 例：「血糖高怎么办」正文能对上"胰岛素"这节，但标题里没有血糖主题 → 交回 RAG。
        if top_cov < 0.6 and not title_hit:
            if margin < BIG_MARGIN:
                return {"hit": None, "score": top_score, "coverage": top_cov, "dice": top_dice,
                        "margin": margin, "reason": "topic_not_in_title", "elapsed_ms": elapsed_ms}
        if margin < min_margin and not title_hit:
            return {"hit": None, "score": top_score, "coverage": top_cov, "dice": top_dice,
                    "margin": margin, "reason": "ambiguous_top2", "elapsed_ms": elapsed_ms}

        entry = dict(self.entries[top_idx])
        logger.info("quick_index.hit", section=entry.get("section_title", "")[:40],
                    score=round(top_score, 3), coverage=round(top_cov, 3), elapsed_ms=elapsed_ms)
        return {"hit": entry, "score": top_score, "coverage": top_cov, "dice": top_dice,
                "margin": margin, "reason": "hit", "elapsed_ms": elapsed_ms}


_index: Optional[QuickIndex] = None


def get_quick_index() -> QuickIndex:
    """获取全局快索引（可用 reload_quick_index() 在知识库变更后重建）。"""
    global _index
    if _index is None:
        _index = QuickIndex()
    return _index


def reload_quick_index() -> QuickIndex:
    """知识库变更后重新加载索引。"""
    global _index
    _index = QuickIndex()
    return _index
