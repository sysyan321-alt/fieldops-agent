# backend/agents/alert/graph.py
# 告警研判 Agent 的 LangGraph 状态图构建

from langgraph.graph import StateGraph, START, END

from backend.agents.alert.state import AlertState
from backend.agents.alert.nodes import (
    run_four_tracks_node,
    aggregate_tracks_node,
    assess_risk_node,
    notify_supervisor_node,
    supervisor_review_node,
    apply_supervisor_decision_node,
    dispatch_order_node,
)
from backend.core.memory import get_memory_saver


async def build_alert_graph():
    """
    构建并编译告警研判 Agent 的 LangGraph 状态图。

    异步原因：检查点存储是 AsyncPostgresSaver（首次调用要建连接池 + 建表），
    所以图构建本身是 await 的；调用方（编排器 / 确认接口 / MCP 工具）都在异步上下文里。

    执行链路（线性）：
        START → run_four_tracks → aggregate_tracks → assess_risk
        → notify_supervisor → supervisor_review [interrupt]
        → apply_supervisor_decision → dispatch_order → END

    特点：有 checkpointer（interrupt/resume 需要状态持久化）
    """
    builder = StateGraph(AlertState)

    # 注册节点
    builder.add_node("run_four_tracks",          run_four_tracks_node)
    builder.add_node("aggregate_tracks",          aggregate_tracks_node)
    builder.add_node("assess_risk",              assess_risk_node)
    builder.add_node("notify_supervisor",        notify_supervisor_node)
    builder.add_node("supervisor_review",        supervisor_review_node)
    builder.add_node("apply_supervisor_decision", apply_supervisor_decision_node)
    builder.add_node("dispatch_order",           dispatch_order_node)

    # 固定边（线性链）
    builder.add_edge(START,                     "run_four_tracks")
    builder.add_edge("run_four_tracks",         "aggregate_tracks")
    builder.add_edge("aggregate_tracks",         "assess_risk")
    builder.add_edge("assess_risk",              "notify_supervisor")
    builder.add_edge("notify_supervisor",         "supervisor_review")
    builder.add_edge("supervisor_review",         "apply_supervisor_decision")
    builder.add_edge("apply_supervisor_decision", "dispatch_order")
    builder.add_edge("dispatch_order",           END)

    # 编译，绑定 checkpointer（interrupt/resume 需要状态持久化）
    checkpointer = await get_memory_saver("alert")
    return builder.compile(checkpointer=checkpointer)


if __name__ == '__main__':
    import asyncio
    g = asyncio.run(build_alert_graph())
    nodes = [n for n in g.get_graph().nodes if n not in ("__start__", "__end__")]
    print("图编译成功:", g is not None)
    print("节点数:", len(nodes))
    print("节点:", nodes)
