# scripts/test_work_flow.py
# 作业流程 Agent 端到端测试（状态机范式）
# 测试 3 个场景：① 单轮日常巡检 ② 多轮触发阶段流转 ③ FINISHED 触发报告生成
#
# 用法：cd <项目根目录> && PYTHONPATH=. python scripts/test_work_flow.py

import asyncio

from langchain_core.messages import HumanMessage

from backend.agents.work_flow.graph import build_work_flow_graph
from backend.agents.work_flow.state import WorkStage
from backend.core.memory import build_config
from backend.core.logger import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)

SITE_ID = "s001"


def _build_initial_state(session_id: str, stage: str = None, max_turns: int = 40,
                         stage_turn: int = 0, total_turn: int = 0) -> dict:
    """构造初始 State。"""
    return {
        "messages":              [HumanMessage(content="")],  # 占位，调用方覆盖
        "site_id":               SITE_ID,
        "tenant_id":              "tenant_default",
        "session_id":             session_id,
        "service_plan_id":           f"plan-{session_id}",
        "current_stage":          stage or WorkStage.ONBOARDING_ASSESSMENT.value,
        "stage_turn_count":       stage_turn,
        "total_turn_count":       total_turn,
        "max_turns":              max_turns,
        "service_plan":              None,
        "service_history":           [],
        "last_response_quality":  "",
        "followup_count":         0,
        "existing_summary":       None,
        "should_summarize":       False,
        "report":                 None,
        "fallback_used":          False,
        "structured_output":      None,
    }


def _last_ai_content(result: dict) -> str:
    """取 result 中最后一条 AI 消息文本。"""
    msgs = result.get("messages", [])
    for m in reversed(msgs):
        if getattr(m, "type", "") == "ai":
            content = m.content
            return content if isinstance(content, str) else str(content)
    return "（无 AI 消息）"


# ──────────────────────────────────────────────────────────────
# 用例①：单轮日常巡检（建档评估阶段）
# ──────────────────────────────────────────────────────────────

async def run_single_turn_work():
    print("\n" + "=" * 60)
    print("  作业流程 Agent - 用例①：单轮日常巡检（建档评估阶段）")
    print("=" * 60)

    graph = await build_work_flow_graph()
    session_id = "work-single"
    config = build_config(SITE_ID, session_id, agent_type="work_flow")

    state = _build_initial_state(session_id)
    state["messages"] = [HumanMessage(content="点位今天精神状态不错，早班巡检已完成，进食量正常。")]

    print(f"\n[1] 作业人员输入：{state['messages'][0].content}")
    print(f"    初始阶段：{state['current_stage']}")

    result = await graph.ainvoke(state, config=config)

    print(f"\n[2] AI 响应：")
    print(f"    {_last_ai_content(result)[:400]}")

    print(f"\n[3] 状态更新：")
    print(f"    current_stage:          {result.get('current_stage')}")
    print(f"    stage_turn_count:       {result.get('stage_turn_count')}")
    print(f"    total_turn_count:       {result.get('total_turn_count')}")
    print(f"    last_response_quality: {result.get('last_response_quality')}")
    cp = result.get("service_plan") or {}
    print(f"    service_plan.level:        {cp.get('risk_tier')}")

    print("\n" + "=" * 60)
    print("  ✅ 单轮日常巡检链路跑通！")
    print("=" * 60)


# ──────────────────────────────────────────────────────────────
# 用例②：多轮触发阶段流转（用小 max_turns 快速触发）
# ──────────────────────────────────────────────────────────────

async def run_stage_transition():
    print("\n" + "=" * 60)
    print("  作业流程 Agent - 用例②：多轮触发阶段流转")
    print("=" * 60)

    graph = await build_work_flow_graph()
    session_id = "work-transition"
    config = build_config(SITE_ID, session_id, agent_type="work_flow")

    # 用极小 max_turns=2 触发流转
    base = _build_initial_state(session_id, max_turns=2)

    turns = [
        "点位今早生命体征平稳，已完成入住体检。",
        "个人物品已核对，家属已介绍完毕。",
        "建档评估完成，请进入下一阶段。",   # 这轮 check_stage 会触发流转
    ]

    for i, content in enumerate(turns, 1):
        state = {**base, "messages": [HumanMessage(content=content)]}
        print(f"\n[第 {i} 轮] 作业人员：{content}")
        print(f"    （传入 stage={state['current_stage']}, "
              f"stage_turn={state['stage_turn_count']}, "
              f"total_turn={state['total_turn_count']}）")

        result = await graph.ainvoke(state, config=config)

        ai = _last_ai_content(result)
        print(f"        AI：{ai[:200]}{'...' if len(ai) > 200 else ''}")
        print(f"        阶段: {result.get('current_stage')}, "
              f"阶段轮数: {result.get('stage_turn_count')}, "
              f"总轮数: {result.get('total_turn_count')}")

        # 更新 base 供下一轮传入（模拟跨轮状态保持）
        base["current_stage"] = result.get("current_stage", base["current_stage"])
        base["stage_turn_count"] = result.get("stage_turn_count", base["stage_turn_count"])
        base["total_turn_count"] = result.get("total_turn_count", base["total_turn_count"])

    print(f"\n[结果] 最终阶段：{base['current_stage']}")
    print("  （期望：从 onboarding_assessment 流转到 daily_inspection）")

    print("\n" + "=" * 60)
    print("  ✅ 阶段流转链路跑通！")
    print("=" * 60)


# ──────────────────────────────────────────────────────────────
# 用例③：直接进入 FINISHED 阶段，触发报告生成分支
# ──────────────────────────────────────────────────────────────

async def run_report_generation():
    print("\n" + "=" * 60)
    print("  作业流程 Agent - 用例③：FINISHED 触发报告生成")
    print("=" * 60)

    graph = await build_work_flow_graph()
    session_id = "work-report"
    config = build_config(SITE_ID, session_id, agent_type="work_flow")

    state = _build_initial_state(
        session_id,
        stage=WorkStage.FINISHED.value,   # 关键：直接 FINISHED
        total_turn=20,
    )
    state["messages"] = [HumanMessage(content="作业流程已全部完成。")]

    print(f"\n[1] 直接进入 FINISHED 阶段，触发报告生成分支")
    print(f"    current_stage: {state['current_stage']}")

    result = await graph.ainvoke(state, config=config)

    print(f"\n[2] generate_report 产出：")
    report = result.get("report") or {}
    print(f"    overall_score:    {report.get('overall_score')}")
    print(f"    next_review_date: {report.get('next_review_date')}")
    print(f"    ── 维度评估 ──")
    for d in report.get("dimensions", []):
        print(f"    - {d.get('dimension','')}: {d.get('score')} 分 | {d.get('comment','')}")
    print(f"    ── 核心优势 ──")
    for s in report.get("strengths", [])[:3]:
        print(f"    - {s}")
    print(f"    ── 改进方向 ──")
    for im in report.get("improvements", [])[:3]:
        print(f"    - {im}")
    print(f"    ── 服务建议 ──")
    for cs in report.get("service_suggestions", [])[:3]:
        print(f"    - {cs}")

    print(f"\n[3] structured_output:")
    so = result.get("structured_output") or {}
    print(f"    service_plan_id: {so.get('service_plan_id')}")
    print(f"    site_id:     {so.get('site_id')}")

    print("\n" + "=" * 60)
    print("  ✅ 报告生成分支跑通！")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_single_turn_work())
    asyncio.run(run_stage_transition())
    asyncio.run(run_report_generation())
