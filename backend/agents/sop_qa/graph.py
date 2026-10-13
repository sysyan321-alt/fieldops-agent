# backend/agents/sop_qa/graph.py
# 作业规范 问答 Agent 的 LangGraph 状态图构建

from langgraph.graph import StateGraph, START, END

from backend.agents.sop_qa.state import SOPQAState
from backend.agents.sop_qa.nodes import (
    classify_query_node,
    hyde_generate_node,
    multi_query_rewrite_node,
    retrieve_node,
    generate_rag_node,
    quick_route_node,
    quick_answer_node,
    web_search_node,
    generate_direct_node,
    generate_general_node,
    save_memory_node,
)
from backend.core.memory import get_memory_saver


def _route_by_query_type(state: SOPQAState) -> str:
    """
    classify_query 之后的路由：根据 query_type 分流。
    GENERAL → 直接 LLM 回答
    PRECISE → 直接检索
    VAGUE   → HyDE 生成
    BROAD   → Multi-Query 改写
    """
    qt = state.get("query_type", "PRECISE").upper()
    if qt == "GENERAL" and state.get("enable_web_search", False):
        return "GENERAL_WEB"
    return qt


def _route_by_confidence(state: SOPQAState) -> str:
    if state.get("answer_mode") == "quick":
        return "quick"
    """
    retrieve 之后的路由：根据置信度分流。
    is_high_confidence=True              → "high"：RAG 回答
    is_high_confidence=False
      + enable_web_search=True           → "low_web"：先联网再直答
      + enable_web_search=False          → "low_direct"：直接 LLM 兜底
    """
    if state.get("is_high_confidence", False):
        return "high"
    if state.get("enable_web_search", False):
        return "low_web"
    return "low_direct"


def _route_after_web_search(state: SOPQAState) -> str:
    """web_search 节点被两条路径共用，走完后区分去向。"""
    if state.get("query_type", "").upper() == "GENERAL":
        return "generate_general"
    return "generate_direct"


async def build_sop_qa_graph():
    """
    构建并编译作业规范 问答 Agent 的 LangGraph 状态图。

    图拓扑：
        START → classify_query
                  ├─(GENERAL/GENERAL_WEB)→ web_search → generate_general → save_memory → END
                  │                        └─(GENERAL_WEB)→
                  ├─(PRECISE)→ retrieve
                  ├─(VAGUE)→ hyde_generate → retrieve
                  └─(BROAD)→ multi_query_rewrite → retrieve
                                               retrieve
                                                  ├─(high)→ generate_rag → save_memory → END
                                                  ├─(low_web)→ web_search → generate_direct → save_memory → END
                                                  └─(low_direct)→ generate_direct → save_memory → END
    """
    builder = StateGraph(SOPQAState)

    # 注册节点
    builder.add_node("classify_query",      classify_query_node)
    builder.add_node("hyde_generate",       hyde_generate_node)
    builder.add_node("multi_query_rewrite",  multi_query_rewrite_node)
    builder.add_node("retrieve",            retrieve_node)
    builder.add_node("generate_rag",        generate_rag_node)
    builder.add_node("quick_answer",        quick_answer_node)
    builder.add_node("quick_route",         quick_route_node)
    builder.add_node("web_search",          web_search_node)
    builder.add_node("generate_direct",     generate_direct_node)
    builder.add_node("generate_general",    generate_general_node)
    builder.add_node("save_memory",         save_memory_node)

    # 入口固定边
    builder.add_edge(START, "quick_route")

    # 条件边⓪：入口快通道（命中即毫秒级作答，不经过任何 LLM）
    builder.add_conditional_edges(
        "quick_route",
        lambda s: "quick" if s.get("answer_mode") == "quick" else "normal",
        {"quick": "quick_answer", "normal": "classify_query"},
    )

    # 条件边①：Query 类型路由
    builder.add_conditional_edges(
        "classify_query",
        _route_by_query_type,
        {
            "GENERAL":     "generate_general",
            "GENERAL_WEB": "web_search",
            "PRECISE":     "retrieve",
            "VAGUE":       "hyde_generate",
            "BROAD":       "multi_query_rewrite",
        },
    )

    # VAGUE / BROAD 预处理完成后汇入 retrieve
    builder.add_edge("hyde_generate",       "retrieve")
    builder.add_edge("multi_query_rewrite", "retrieve")

    # 条件边②：置信度路由
    builder.add_conditional_edges(
        "retrieve",
        _route_by_confidence,
        {
            "high":       "generate_rag",
            "quick":      "quick_answer",
            "low_web":    "web_search",
            "low_direct": "generate_direct",
        },
    )

    # 条件边③：web_search 出口路由
    builder.add_conditional_edges(
        "web_search",
        _route_after_web_search,
        {
            "generate_general": "generate_general",
            "generate_direct":  "generate_direct",
        },
    )

    # 各生成节点 → save_memory → END
    builder.add_edge("generate_rag",     "save_memory")
    builder.add_edge("quick_answer",     "save_memory")
    builder.add_edge("generate_general", "save_memory")
    builder.add_edge("generate_direct",  "save_memory")
    builder.add_edge("save_memory",       END)

    # 编译（绑定 checkpointer 实现多轮记忆：默认 AsyncPostgresSaver，落 PostgreSQL）
    checkpointer = await get_memory_saver("sop_qa")
    return builder.compile(checkpointer=checkpointer)


if __name__ == '__main__':
    import asyncio
    graph = asyncio.run(build_sop_qa_graph())
    print('图编译成功')
    print('节点列表：', list(graph.nodes.keys()))
