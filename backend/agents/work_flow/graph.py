# backend/agents/work_flow/graph.py
# 作业流程 Agent 的 LangGraph 状态图构建

from langgraph.graph import StateGraph, START, END

from backend.agents.work_flow.state import (
    WorkFlowState, WorkStage,
)
from backend.agents.work_flow.nodes import (
    load_context_node,
    notify_missing_plan_node,
    check_stage_node,
    evaluate_response_node,
    generate_response_node,
    generate_report_node,
    save_report_node,
    save_memory_node,
)
from backend.core.memory import get_memory_saver


def _route_after_check_stage(state: WorkFlowState) -> list[str] | str:
    """
    check_stage 之后的条件路由。
    FINISHED → generate_report（生成报告）
    其它     → evaluate_response + generate_response（并行，节省 1 轮 LLM 调用）
    """
    current_stage = state.get("current_stage", "")
    if current_stage == WorkStage.FINISHED.value:
        return "generate_report"
    return ["evaluate_response", "generate_response"]


def _route_after_load_context(state: WorkFlowState) -> str:
    """load_context 之后：没有服务计划就不该推进状态机。

    改造前这里会用 mock 计划兜底继续跑，等于让一份编造的服务计划驱动"日常工作"——
    现在改为如实告知并结束本轮，由业务侧去补数据。
    """
    return "notify_missing_plan" if state.get("service_plan_missing") else "check_stage"


async def build_work_flow_graph():
    """
    构建并编译作业流程 Agent 的 LangGraph 状态图。

    图拓扑（状态机模式）：
        START → load_context → check_stage
                                   ├─(FINISHED)→ generate_report → save_report → END
                                   └─(continue)→ ┌ evaluate_response ┐ → save_memory → END
                                                  └ generate_response ┘
    特点：
        - 有 checkpointer（跨轮记忆）
        - evaluate_response 与 generate_response 并行（后者不依赖前者输出）
        - 每轮：check_stage → 并行评估/生成 → save_memory → END
        - END 后通过同 thread_id 再次 ainvoke 可继续下一轮
        - 当 current_stage 变为 FINISHED 时走报告生成分支
    """
    builder = StateGraph(WorkFlowState)

    # 注册节点
    builder.add_node("load_context",     load_context_node)
    builder.add_node("notify_missing_plan", notify_missing_plan_node)
    builder.add_node("check_stage",      check_stage_node)
    builder.add_node("evaluate_response", evaluate_response_node)
    builder.add_node("generate_response", generate_response_node)
    builder.add_node("generate_report",  generate_report_node)
    builder.add_node("save_report",      save_report_node)
    builder.add_node("save_memory",      save_memory_node)

    # 固定边
    builder.add_edge(START,             "load_context")

    # 条件边⓪：计划缺失时不推进阶段（如实告知，不编计划）
    builder.add_conditional_edges(
        "load_context",
        _route_after_load_context,
        {"notify_missing_plan": "notify_missing_plan", "check_stage": "check_stage"},
    )
    builder.add_edge("notify_missing_plan", END)

    # 条件边①：阶段流转路由
    builder.add_conditional_edges(
        "check_stage",
        _route_after_check_stage,
        {
            "generate_report":     "generate_report",
            "evaluate_response":   "evaluate_response",
            "generate_response":   "generate_response",
        },
    )

    # 报告分支
    builder.add_edge("generate_report", "save_report")
    builder.add_edge("save_report",     END)

    # 服务分支：evaluate_response 与 generate_response 并行，汇合到 save_memory
    builder.add_edge("evaluate_response", "save_memory")
    builder.add_edge("generate_response",  "save_memory")
    builder.add_edge("save_memory",        END)

    # 编译（绑定 checkpointer：默认 AsyncPostgresSaver，阶段状态跨重启存活）
    checkpointer = await get_memory_saver("work_flow")
    return builder.compile(checkpointer=checkpointer)


if __name__ == '__main__':
    import asyncio
    g = asyncio.run(build_work_flow_graph())
    nodes = [n for n in g.get_graph().nodes if n not in ("__start__", "__end__")]
    print("图编译成功:", g is not None)
    print("节点数:", len(nodes))
    print("节点:", nodes)
