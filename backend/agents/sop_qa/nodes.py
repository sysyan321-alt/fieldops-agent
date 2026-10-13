# backend/agents/sop_qa/nodes.py
# 护理 SOP 问答 Agent 的所有节点函数

import asyncio
import json

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from backend.agents.sop_qa.state import SOPQAState
from backend.agents.sop_qa.prompts import (
    SYSTEM_PROMPT,
    HYDE_PROMPT,
    MULTI_QUERY_PROMPT,
    RAG_ANSWER_PROMPT,
    DIRECT_ANSWER_PROMPT,
    GENERAL_ANSWER_PROMPT,
    CLASSIFY_PROMPT,
)
from backend.core.llm_factory import get_llm
from backend.core.web_search import format_search_results
from backend.core.memory import (
    trim_messages_to_window,
    should_trigger_summary,
    compress_to_summary,
)
from backend.core.logger import get_logger

logger = get_logger(__name__)

# ── 检索相关常量 ───────────────────────────────────────────────
MAX_BROAD_QUERIES = 3      # BROAD 分支最多并行的子 Query 数
RERANK_TOP_K = 3           # 精排后保留的最终 chunk 数
RECALL_TOP_K = 10          # Hybrid 召回数量（送给精排）
HIGH_CONFIDENCE_THRESHOLD = 0.75   # 高置信度阈值，>= 此值走 RAG 分支


def _get_message_content(msg) -> str:
    """统一获取消息文本（兼容 .text 属性和 .content 属性）"""
    if hasattr(msg, "text") and not callable(getattr(msg, "text", None)):
        return msg.text
    if isinstance(msg.content, str):
        return msg.content
    return str(msg.content)


def _format_history_for_prompt(messages: list) -> str:
    """把最近几轮对话格式化为 Prompt 可用的文本"""
    lines = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            lines.append(f"护理员：{_get_message_content(msg)}")
        elif isinstance(msg, AIMessage):
            lines.append(f"AI：{_get_message_content(msg)[:200]}...")
    return "\n".join(lines) if lines else "（无历史对话）"


def _build_system_content(summary: str | None = None, high_risk: bool = False) -> str:
    """构建注入了历史摘要（必要时还有应急引导）的 SystemMessage 内容。"""
    content = SYSTEM_PROMPT
    if summary:
        content += f"\n\n【护理员历史摘要】\n{summary}"
    if high_risk:
        # 高危问法：先给应急处置方向，再讲 SOP 细节，并明确升级路径
        content += (
            "\n\n【本次提问涉及应急情形】请按以下顺序作答："
            "先给最优先的现场处置动作（保持气道通畅、判断意识与呼吸、呼叫支援），"
            "再给 SOP 细节；结尾必须提示立即联系值班医生或拨打 120，不要只给一条护理操作。"
        )
    return content


# ──────────────────────────────────────────────────────────────
# 节点1：classify_query — 查询分类
# ──────────────────────────────────────────────────────────────

# 护理 SOP 高频专有名词：命中即判 PRECISE，跳过 LLM 分类
_PRECISE_KEYWORDS = frozenset({
    # 护理操作
    "翻身", "压疮", "鼻饲", "吸痰", "导尿", "灌肠", "热敷", "冷敷",
    "口腔护理", "皮肤护理", "会阴护理", "伤口换药", "胃管", "尿管",
    "静脉输液", "皮试", "注射", "给药", "发药", "服药",
    # 评估/指标
    "跌倒风险", "营养评估", "认知评估", "吞咽功能", "肌力", "关节活动",
    "血压", "血糖", "体温", "心率", "血氧", "意识状态",
    # 照护阶段
    "入院评估", "出院评估", "日常照护", "阶段复评", "异常处理",
    "晨间护理", "晚间护理", "临终关怀", "康复训练",
    # 制度/流程
    "交接班", "护理记录", "不良事件", "应急预案", "隔离", "消毒",
})

# 模糊/宽泛特征词：命中即判 VAGUE/BROAD，跳过 LLM 分类
_VAGUE_KEYWORDS = frozenset({"怎么办", "怎么处理", "有什么", "是什么样的", "什么是"})


async def classify_query_node(state: SOPQAState) -> dict:
    """分类查询类型：PRECISE / VAGUE / BROAD / GENERAL。

    优先用规则匹配绕过 LLM 调用，仅对规则无法覆盖的查询才走 LLM 精细分类。
    """
    query = state["original_query"]

    # 简单规则：极短或含模糊词 → BROAD/VAGUE
    if len(query.strip()) <= 5:
        return {"query_type": "BROAD"}

    # 含闲聊关键词 → GENERAL
    general_hints = ("你好", "谢谢", "再见", "你是谁", "在吗")
    if any(h in query for h in general_hints):
        return {"query_type": "GENERAL"}

    # 命中护理专有名词 → PRECISE（跳过 LLM）
    if any(kw in query for kw in _PRECISE_KEYWORDS):
        logger.info("classify_query.rule_hit", query_type="PRECISE")
        return {"query_type": "PRECISE"}

    # 命中模糊特征词 → VAGUE（跳过 LLM）
    if any(kw in query for kw in _VAGUE_KEYWORDS):
        logger.info("classify_query.rule_hit", query_type="VAGUE")
        return {"query_type": "VAGUE"}

    # 用 LLM 精细分类
    try:
        prompt = CLASSIFY_PROMPT.format(query=query)
        llm = get_llm("sop_qa", temperature=0)
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        result = _get_message_content(resp).strip().upper()
        if result not in ("PRECISE", "VAGUE", "BROAD", "GENERAL"):
            result = "PRECISE"
        logger.info("classify_query.done", query_type=result)
        return {"query_type": result}
    except Exception as e:
        logger.warning("classify_query.fallback", error=str(e))
        return {"query_type": "PRECISE"}


# ──────────────────────────────────────────────────────────────
# 节点2：hyde_generate — HyDE 假设文档生成（VAGUE 分支）
# ──────────────────────────────────────────────────────────────

async def hyde_generate_node(state: SOPQAState) -> dict:
    """为模糊查询生成假设文档，扩充语义后检索。"""
    query = state["original_query"]
    history = _format_history_for_prompt(state.get("messages", []))

    try:
        prompt = HYDE_PROMPT.format(history=history, query=query)
        llm = get_llm("sop_qa", temperature=0.3)
        resp = await llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])
        hyde_doc = _get_message_content(resp).strip()
        logger.info("hyde_generate.done", doc_length=len(hyde_doc))
        return {"hyde_document": hyde_doc}
    except Exception as e:
        logger.warning("hyde_generate.failed", error=str(e))
        return {"hyde_document": query}  # 降级：用原查询当假设文档


# ──────────────────────────────────────────────────────────────
# 节点3：multi_query_rewrite — 多查询改写（BROAD 分支）
# ──────────────────────────────────────────────────────────────

async def multi_query_rewrite_node(state: SOPQAState) -> dict:
    """将宽泛查询改写为多个子查询。"""
    query = state["original_query"]

    try:
        prompt = MULTI_QUERY_PROMPT.format(query=query)
        llm = get_llm("sop_qa", temperature=0.3)
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        raw = _get_message_content(resp).strip()

        # 清理 markdown
        if "```" in raw:
            blocks = raw.split("```")
            for block in blocks[1::2]:
                block = block.strip()
                if block.startswith("json"):
                    block = block[4:].strip()
                if block.startswith("["):
                    raw = block
                    break

        rewritten = json.loads(raw)
        if isinstance(rewritten, list):
            rewritten = [q for q in rewritten if isinstance(q, str)][:MAX_BROAD_QUERIES]
        else:
            rewritten = [query]
        logger.info("multi_query_rewrite.done", count=len(rewritten))
        return {"rewritten_queries": rewritten}
    except Exception as e:
        logger.warning("multi_query_rewrite.failed", error=str(e))
        return {"rewritten_queries": [query]}  # 降级：用原查询


# ──────────────────────────────────────────────────────────────
# 节点4：retrieve — Milvus 混合检索 + BGE-Reranker 精排
# ──────────────────────────────────────────────────────────────

async def retrieve_node(state: SOPQAState) -> dict:
    """
    从 Milvus 向量库检索护理 SOP 文档，并用 BGE-Reranker 精排。

    检索策略按 query_type 分流：
      PRECISE → 用 original_query 直接检索
      VAGUE   → 用 HyDE 生成的假设文档检索（语义更丰富）
      BROAD   → 用 original_query 检索（multi_query 改写主要用于 LLM 上下文增强）

    检索结果经 BGE-Reranker 精排后，按 confidence 阈值（0.75）判断是否高置信度。
    高置信度 → 走 generate_rag 分支；低置信度 → 走 web_search/generate_direct 兜底。

    三层 fallback：
      1. retrieve pipeline 抛异常 → 返回空结果 + confidence 0（走 low_direct）
      2. retrieve 返回空候选 → 同上
      3. reranker 输出极低 → 同上
    """
    query_type = state.get("query_type", "PRECISE")
    original_query = state["original_query"]
    tenant_id = state.get("tenant_id", "tenant_default")
    org_id = state.get("org_id")  # 机构 ID，可选作 course_id 过滤；None = 全库

    # ── 快通道：标准问法命中直接返回 SOP 原文，不加载模型、不调 LLM ──
    # 判定用同义词规范化 + BM25 + 覆盖率/标题术语门控，没把握时返回 None 继续走 RAG。
    from backend.core.quick_index import get_quick_index

    quick = get_quick_index().search(original_query, tenant_id=tenant_id, category=org_id)
    if quick.get("hit"):
        hit = quick["hit"]
        logger.info("retrieve.quick_hit", section=hit.get("section_title", "")[:40],
                    coverage=round(quick.get("coverage", 0), 3),
                    elapsed_ms=quick.get("elapsed_ms"))
        return {
            "ranked_chunks": [{
                "content": hit.get("snippet", ""),
                "source": hit.get("source", ""),
                "score": 1.0,
                "metadata": {"source_name": hit.get("source", ""), "course_id": hit.get("category", "")},
            }],
            "confidence": 1.0,
            "is_high_confidence": True,
            "answer_mode": "quick",
            "quick_hit": hit,
        }

    # ── 确定检索查询文本 ──
    # VAGUE 分支优先用 HyDE 生成的假设文档（语义扩充），失败时回退原查询
    hyde_doc = state.get("hyde_document")
    if query_type == "VAGUE" and hyde_doc:
        search_query = hyde_doc
        logger.info("retrieve.start", query_type=query_type, query_source="hyde",
                    query_preview=search_query[:80])
    else:
        search_query = original_query
        logger.info("retrieve.start", query_type=query_type, query_source="original",
                    query_preview=search_query[:80])

    # ── 调用 retrieve pipeline（内部走线程池；命中 Redis 缓存时跳过编码+检索+精排）──
    try:
        from backend.core.retrieval_cache import cached_retrieve

        ranked_docs, confidence = await cached_retrieve(
            query=search_query,
            tenant_id=tenant_id,
            course_id=org_id,          # None = 全库
            recall_top_k=RECALL_TOP_K,
            rerank_top_k=RERANK_TOP_K,
            key_query=original_query,  # 缓存键用用户原话，避免 HyDE 改写导致永远不命中
        )
    except Exception as e:
        # 三层 fallback 第三层：系统兜底，返回空结果走 low_direct
        logger.error("retrieve.pipeline_failed", error=str(e), query_type=query_type)
        return {
            "ranked_chunks": [],
            "confidence": 0.0,
            "is_high_confidence": False,
            "fallback_used": True,
        }

    # ── 把 RankedDocument 转成 state 期望的 dict 格式 ──
    # generate_rag_node 用 c.get('content', c.get('text', '')) 取正文，
    #                  c.get('source', c.get('title', ...)) 取来源标注
    ranked_chunks: list[dict] = [
        {
            "content": doc.content,
            "source":  doc.metadata.get("source_name", "") or f"文档{i+1}",
            "score":   doc.score,
            "metadata": doc.metadata,
        }
        for i, doc in enumerate(ranked_docs)
    ]

    is_high_confidence = confidence >= HIGH_CONFIDENCE_THRESHOLD

    logger.info(
        "retrieve.done",
        chunks=len(ranked_chunks),
        confidence=round(confidence, 4),
        is_high_confidence=is_high_confidence,
        top_score=round(ranked_docs[0].score, 4) if ranked_docs else 0.0,
    )

    return {
        "ranked_chunks": ranked_chunks,
        "confidence": confidence,
        "is_high_confidence": is_high_confidence,
    }


# ──────────────────────────────────────────────────────────────
# 节点5：generate_rag — RAG 回答（高置信度）
# ──────────────────────────────────────────────────────────────

async def quick_answer_node(state: SOPQAState) -> dict:
    """快通道作答：直接用 SOP 原文 + 来源章节回复，不调用 LLM。

    适用场景：标准问法命中（如"压疮分几期"），此时返回规范原文比让模型改写更安全，
    也省掉生成开销；模型未加载时同样可用。
    """
    hit = state.get("quick_hit") or {}
    chunks = state.get("ranked_chunks") or []
    content = hit.get("snippet") or (chunks[0].get("content") if chunks else "")
    source = hit.get("source") or (chunks[0].get("source") if chunks else "")
    answer = f"根据《{source}》：\n\n{content}\n\n（以上为护理 SOP 原文，请按机构规范执行）"
    logger.info("quick_answer.done", source=source[:50])
    return {
        "answer": answer,
        "answer_mode": "quick",
        "sources": [source] if source else [],
        "confidence": 1.0,
    }


async def generate_rag_node(state: SOPQAState) -> dict:
    """基于检索到的 SOP 片段生成带引用的回答。"""
    query = state["original_query"]
    chunks = state.get("ranked_chunks", [])
    existing_summary = state.get("existing_summary")

    # 拼接检索片段
    context = "\n\n".join([
        f"[文档{i+1}] {c.get('content', c.get('text', ''))}"
        for i, c in enumerate(chunks)
    ]) or "（未检索到相关文档）"

    sources = [c.get("source", c.get("title", f"文档{i+1}")) for i, c in enumerate(chunks)]

    prompt = RAG_ANSWER_PROMPT.format(context=context, question=query)

    try:
        llm = get_llm("sop_qa", temperature=0, streaming=True)
        # 用 astream 替代 ainvoke：逐 token 收集，首 token 更快到达
        chunks_text = []
        async for chunk in llm.astream([
            SystemMessage(content=_build_system_content(existing_summary, bool(state.get("high_risk")))),
            HumanMessage(content=prompt),
        ]):
            text = _get_message_content(chunk)
            if text:
                chunks_text.append(text)
        answer = "".join(chunks_text)
    except Exception as e:
        logger.error("generate_rag.failed", error=str(e))
        answer = "护理 SOP 检索回答生成失败，请稍后重试。"

    return {
        "answer": answer,
        "sources": sources,
        "answer_mode": "rag",
        "should_summarize": should_trigger_summary(state.get("messages", []), state.get("summary_message_count", 0)),
    }


# ──────────────────────────────────────────────────────────────
# 节点6：web_search — 联网搜索（占位）
# ──────────────────────────────────────────────────────────────

async def web_search_node(state: SOPQAState) -> dict:
    """联网搜索补充信息（Tavily 优先，DuckDuckGo 免费兜底）。"""
    query = state.get("rewritten_queries") or state.get("original_query", "")
    if isinstance(query, list):
        query = query[0] if query else ""
    logger.info("web_search.start", query=query[:80])

    try:
        from backend.core.web_search import web_search
        results = await web_search(query, max_results=5)
    except Exception as e:
        logger.error("web_search.failed", error=str(e))
        results = []

    logger.info("web_search.done", hits=len(results))
    return {"web_search_results": results}


# ──────────────────────────────────────────────────────────────
# 节点7：generate_direct — 直接 LLM 回答（低置信度兜底）
# ──────────────────────────────────────────────────────────────

async def generate_direct_node(state: SOPQAState) -> dict:
    """低置信度时直接用 LLM 回答（结合联网搜索结果）。"""
    query = state["original_query"]
    existing_summary = state.get("existing_summary")

    prompt = DIRECT_ANSWER_PROMPT.format(
        question=query,
        web_results=format_search_results(state.get("web_search_results", [])),
    )

    try:
        llm = get_llm("sop_qa", temperature=0.3, streaming=True)
        resp = await llm.ainvoke([
            SystemMessage(content=_build_system_content(existing_summary, bool(state.get("high_risk")))),
            HumanMessage(content=prompt),
        ])
        answer = _get_message_content(resp)
    except Exception as e:
        logger.error("generate_direct.failed", error=str(e))
        answer = "护理 SOP 回答生成失败，请稍后重试或查阅纸质护理手册。"

    return {
        "answer": answer,
        "sources": [],
        "answer_mode": "llm_direct",
        "should_summarize": should_trigger_summary(state.get("messages", []), state.get("summary_message_count", 0)),
    }


# ──────────────────────────────────────────────────────────────
# 节点8：generate_general — 通用对话回答
# ──────────────────────────────────────────────────────────────

async def generate_general_node(state: SOPQAState) -> dict:
    """通用对话回答。"""
    query = state["original_query"]
    # 窗口裁剪：只把最近 10 轮放进 prompt，避免长会话把上下文撑爆
    recent_messages = trim_messages_to_window(state.get("messages", []), window_size=10)
    history = _format_history_for_prompt(recent_messages)
    existing_summary = state.get("existing_summary")

    prompt = GENERAL_ANSWER_PROMPT.format(
        question=query,
        history=history,
        web_results=format_search_results(state.get("web_search_results", [])),
    )

    try:
        llm = get_llm("sop_qa", temperature=0.5, streaming=True)
        resp = await llm.ainvoke([
            SystemMessage(content=_build_system_content(existing_summary, bool(state.get("high_risk")))),
            HumanMessage(content=prompt),
        ])
        answer = _get_message_content(resp)
    except Exception as e:
        logger.error("generate_general.failed", error=str(e))
        answer = "抱歉，回答生成失败，请稍后重试。"

    return {
        "answer": answer,
        "sources": [],
        "answer_mode": "general",
        "should_summarize": should_trigger_summary(state.get("messages", []), state.get("summary_message_count", 0)),
    }


# ──────────────────────────────────────────────────────────────
# 节点9：save_memory — 记忆持久化（摘要写回 state，随检查点落库）
# ──────────────────────────────────────────────────────────────

async def save_memory_node(state: SOPQAState) -> dict:
    """保存会话记忆：需要时压缩历史并**写回 existing_summary**。

    写回是关键：只生成不写回的话，下一轮 generate_* 节点拿到的还是 None，
    摘要等于没做，白花一次 LLM 调用。写回后它随检查点持久化，服务重启也还在。
    """
    messages = state.get("messages", [])
    update: dict = {}

    if state.get("should_summarize") and messages:
        try:
            summary = await compress_to_summary(messages, state.get("existing_summary"))
            update["existing_summary"] = summary
            update["summary_message_count"] = len(messages)
            logger.info("save_memory.summary_saved", length=len(summary),
                        covered_messages=len(messages))
        except Exception as e:
            # 摘要失败不影响回答：旧摘要继续用，下一轮再试
            logger.warning("save_memory.summary_failed", error=str(e))

    # 构造结构化输出
    structured_output = {
        "answer": state.get("answer", ""),
        "answer_mode": state.get("answer_mode", ""),
        "confidence": state.get("confidence", 0.0),
        "sources": state.get("sources", []),
    }

    update["structured_output"] = structured_output
    return update


async def quick_route_node(state: SOPQAState) -> dict:
    """入口快通道：在调用任何 LLM 之前先判能不能用 SOP 原文直接回答。

    两道判断：先看是不是高危问法（是则强制走慢链路），再看本地索引是否命中。
    顺序不能反——高危问法即使侥幸命中索引，也不该用一节 SOP 原文作答。
    """
    from backend.core.quick_index import get_quick_index, is_high_risk
    q = state.get("original_query", "")

    if is_high_risk(q):
        # 强制慢链路：走查询分类 → 检索/联网 → 置信度分流，拿完整答案 + 兜底
        logger.warning("quick_route.bypassed_high_risk", query=q[:40])
        return {"answer_mode": "rag", "high_risk": True}

    tenant = state.get("tenant_id", "tenant_default")
    res = get_quick_index().search(q, tenant_id=tenant, category=state.get("org_id"))
    if not res.get("hit"):
        return {"answer_mode": "rag"}
    hit = res["hit"]
    logger.info("quick_route.hit", section=hit.get("section_title", "")[:40],
                elapsed_ms=res.get("elapsed_ms"))
    return {"answer_mode": "quick", "quick_hit": hit, "confidence": 1.0,
            "is_high_confidence": True,
            "ranked_chunks": [{"content": hit.get("snippet", ""), "source": hit.get("source", ""),
                               "score": 1.0, "metadata": {"source_name": hit.get("source", "")}}]}
