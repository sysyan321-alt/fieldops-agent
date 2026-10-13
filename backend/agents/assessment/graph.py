# backend/agents/assessment/graph.py
# 评估 Agent 的 LangGraph 状态图构建

from langgraph.graph import StateGraph, START, END

from backend.agents.assessment.state import AssessmentState
from backend.agents.assessment.nodes import (
    parse_pdf_node,
    extract_structured_node,
    run_five_dimensions_node,
    diagnose_and_summarize_node,
    save_results_node,
)


def build_assessment_graph():
    """构建并编译评估 Agent 的状态图。

    特点：无分支、无 interrupt、无 checkpointer（一次性任务），
          五维度并行是性能关键路径。

    执行链路（线性，5 节点）：
        START → parse_pdf → extract_structured → run_five_dimensions
        → diagnose_and_summarize → save_results → END

    优化：原 diagnose_issues + generate_summary 两步串行合并为单次 LLM 调用。
    """
    builder = StateGraph(AssessmentState)

    # 注册 5 个节点（原 6 个合并为 5 个）
    builder.add_node("parse_pdf",          parse_pdf_node)
    builder.add_node("extract_structured", extract_structured_node)
    builder.add_node("run_five_dimensions", run_five_dimensions_node)
    builder.add_node("diagnose_and_summarize", diagnose_and_summarize_node)
    builder.add_node("save_results",       save_results_node)

    # 顺次连边
    builder.add_edge(START,                      "parse_pdf")
    builder.add_edge("parse_pdf",                "extract_structured")
    builder.add_edge("extract_structured",       "run_five_dimensions")
    builder.add_edge("run_five_dimensions",      "diagnose_and_summarize")
    builder.add_edge("diagnose_and_summarize",   "save_results")
    builder.add_edge("save_results",             END)

    # 不传 checkpointer：一次性任务，不需要断点恢复
    return builder.compile()


if __name__ == '__main__':
    g = build_assessment_graph()
    nodes = [n for n in g.get_graph().nodes if n not in ("__start__", "__end__")]
    print("图编译成功:", g is not None)
    print("节点数:", len(nodes))
    print("节点:", nodes)
