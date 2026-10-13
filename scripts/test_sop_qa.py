# scripts/test_sop_qa.py
# 护理 SOP 问答 Agent 端到端测试（RAG 范式）
# 测试 4 种查询类型分支：PRECISE / VAGUE / BROAD / GENERAL
# 检索节点为占位实现（返回空结果），所有检索类查询都会走 low_direct 兜底
#
# 用法：cd <项目根目录> && PYTHONPATH=. python scripts/test_sop_qa.py

import asyncio

from langchain_core.messages import HumanMessage

from backend.agents.sop_qa.graph import build_sop_qa_graph
from backend.core.memory import build_config
from backend.core.logger import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)

ELDER_ID = "e001"
SESSION_COUNTER = [0]


def _new_session_id() -> str:
    SESSION_COUNTER[0] += 1
    return f"qa-test-{SESSION_COUNTER[0]:03d}"


def _build_initial_state(query: str, session_id: str) -> dict:
    """构造单次查询的初始 State。"""
    return {
        "messages":          [HumanMessage(content=query)],
        "site_id":          ELDER_ID,
        "tenant_id":         "tenant_default",
        "session_id":        session_id,
        "org_id":            None,
        "original_query":    query,
        "query_type":        "",
        "rewritten_queries": [],
        "hyde_document":     None,
        "ranked_chunks":     [],
        "confidence":        0.0,
        "is_high_confidence": False,
        "web_search_results": [],
        "answer":            "",
        "sources":           [],
        "answer_mode":       "",
        "existing_summary":  None,
        "should_summarize":  False,
        "enable_web_search": False,   # 关闭：走 low_direct 兜底
        "fallback_used":     False,
        "structured_output": None,
    }


# ── 4 种查询类型测试用例 ───────────────────────────────────────
TEST_QUERIES = [
    {
        "label": "PRECISE（精确查询 → 检索占位空 → low_direct 兜底）",
        "query": "鼻饲管置管后的日常维护操作步骤是什么？",
        "expected": "retrieve → generate_direct",
    },
    {
        "label": "VAGUE（模糊查询 → HyDE 假设文档 → 检索 → low_direct）",
        "query": "那个压疮的分期我没太懂，能再说说吗",
        "expected": "hyde_generate → retrieve → generate_direct",
    },
    {
        "label": "BROAD（宽泛查询 → Multi-Query 改写 → 检索 → low_direct）",
        "query": "跌倒怎么处理",
        "expected": "multi_query_rewrite → retrieve → generate_direct",
    },
    {
        "label": "GENERAL（闲聊 → 直接 LLM 对话）",
        "query": "你好，你是谁？",
        "expected": "generate_general",
    },
]


async def run_four_query_types():
    """跑 4 种查询类型分支。"""
    print("\n" + "=" * 60)
    print("  护理 SOP 问答 Agent 端到端测试（RAG 范式）")
    print("=" * 60)

    graph = await build_sop_qa_graph()
    print(f"\n图构建成功，节点：{list(graph.nodes.keys())}")

    for i, case in enumerate(TEST_QUERIES, 1):
        print("\n" + "-" * 60)
        print(f"\n[用例 {i}] {case['label']}")
        print(f"  护理员输入：{case['query']}")
        print(f"  期望分支：{case['expected']}")

        session_id = _new_session_id()
        config = build_config(ELDER_ID, session_id, agent_type="sop_qa")
        state = _build_initial_state(case["query"], session_id)
        result = await graph.ainvoke(state, config=config)

        print(f"\n  ── 执行结果 ──")
        print(f"  query_type:        {result.get('query_type', '')}")
        if result.get("hyde_document"):
            hd = result["hyde_document"]
            print(f"  hyde_document 长度: {len(hd)} 字符")
            print(f"  hyde_document 前 120 字：{hd[:120]}...")
        if result.get("rewritten_queries"):
            print(f"  rewritten_queries: {result['rewritten_queries']}")
        print(f"  confidence:        {result.get('confidence', 0)}")
        print(f"  ranked_chunks:     {len(result.get('ranked_chunks', []))} 条")
        print(f"  answer_mode:       {result.get('answer_mode', '')}")
        answer = result.get("answer", "")
        print(f"  answer 长度:       {len(answer)} 字符")
        print(f"  answer 前 300 字：")
        print(f"  {answer[:300]}{'...' if len(answer) > 300 else ''}")

    print("\n" + "=" * 60)
    print("  ✅ SOP 问答 Agent 4 种查询类型分支跑通！")
    print("=" * 60)


async def run_multi_turn_conversation():
    """多轮对话测试（验证 MemorySaver 跨轮记忆）。"""
    print("\n" + "=" * 60)
    print("  SOP 问答 Agent 多轮对话测试（MemorySaver 记忆）")
    print("=" * 60)

    graph = await build_sop_qa_graph()
    session_id = "qa-multi-turn"
    config = build_config(ELDER_ID, session_id, agent_type="sop_qa")

    turns = [
        "鼻饲管置管后第一天需要注意什么？",
        "那第二天的重点观察项呢？",   # 带代词"那"，验证上下文记忆
    ]

    for i, q in enumerate(turns, 1):
        print(f"\n[第 {i} 轮] 护理员：{q}")
        state = _build_initial_state(q, session_id)
        result = await graph.ainvoke(state, config=config)
        answer = result.get("answer", "")
        print(f"        AI：{answer[:200]}{'...' if len(answer) > 200 else ''}")
        msgs = result.get("messages", [])
        print(f"        （会话消息数：{len(msgs)}）")

    print("\n  ✅ 多轮对话记忆链路跑通！")


if __name__ == "__main__":
    asyncio.run(run_four_query_types())
    asyncio.run(run_multi_turn_conversation())
