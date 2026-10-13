# backend/agents/alert/review_hitl.py
# 告警研判 Agent 的 Human-in-the-Loop 测试文件
# 展示 interrupt/resume 模式（approve 和 modify 两个用例）

import asyncio
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command  # 恢复运行

from backend.agents.alert.state import AlertState
from backend.agents.alert.nodes import supervisor_review_node


# 公共基础 state（supervisor_review_node 用到的所有字段）
BASE_STATE = {
    "alert_id": "alert-test-001",
    "site_id": "e-test-001",
    "alert_type": "heart_rate_abnormal",
    "alert_timestamp": "2026-09-18 10:30:00",
    "device_readings": [
        {"device_id": "dev-001", "device_type": "配电监测",
         "reading_value": "135", "reading_unit": "bpm",
         "timestamp": "2026-09-18 10:30:00", "is_abnormal": True},
    ],
    "service_records": [
        {"record_id": "rec-001", "site_id": "e-test-001",
         "service_type": "巡视", "content": "点位安静休息",
         "worker_id": "cg-001", "timestamp": "2026-09-18 09:30:00"},
    ],
    "health_indicators": [
        {"indicator_name": "设备温度", "value": "62", "unit": "℃",
         "reference_range": "≤55", "status": "high",
         "timestamp": "2026-09-18 10:30:00"},
    ],
    "risk_assessment": {
        "risk_level": "high",
        "risk_score": 85,
        "reasoning": "柜内接点温升持续偏高（62℃），且 B 相电流达额定 92%，需立即处置",
        "recommended_actions": [
            "立即派人现场查看该点位状况",
            "通知值班主管评估是否需停电检修",
            "持续监测接点温度与三相电流",
            "检查近期负荷变化与散热情况",
        ],
        "confidence": 0.9,
    },
}


def _build_review_graph():
    """构造只含 supervisor_review_node 的最小测试图"""
    builder = StateGraph(AlertState)
    builder.add_node("supervisor_review", supervisor_review_node)
    builder.add_edge(START, "supervisor_review")
    builder.add_edge("supervisor_review", END)
    return builder.compile(checkpointer=MemorySaver())


# ── 用例①：值班主管直接批准 AI 风险评估 ─────────────────────────
async def atest_review_approve():
    print("\n" + "=" * 55)
    print("【supervisor_review_node】用例① approve")
    print("=" * 55)

    graph = _build_review_graph()
    config = {"configurable": {"thread_id": "test-approve-001"}}

    # 第一次 invoke：图在 interrupt() 处暂停，返回 display_data
    snapshot = await graph.ainvoke(BASE_STATE, config=config)
    print(f"图已暂停，展示给值班主管的数据: {snapshot}")

    # 值班主管选择"批准"，不修改任何措施
    decision = {
        "action": "approve",
        "modified_actions": [],
        "supervisor_id": "sup-001",
        "comment": "同意 AI 建议，立即处置",
    }
    final = await graph.ainvoke(Command(resume=decision), config=config)
    print("*" * 80)
    print(f"final: {final}")
    print("*" * 80)
    print(f"supervisor_decision: {final['supervisor_decision']}")
    print("✅ approve 用例通过")


# ── 用例②：值班主管修改部分处置措施 ─────────────────────────────
async def atest_review_modify():
    print("\n" + "=" * 55)
    print("【supervisor_review_node】用例② modify")
    print("=" * 55)

    graph = _build_review_graph()
    config = {"configurable": {"thread_id": "test-modify-001"}}

    await graph.ainvoke(BASE_STATE, config=config)

    # 值班主管修改处置措施：增加一条，去掉一条
    decision = {
        "action": "modify",
        "modified_actions": [
            "立即派人现场查看该点位状况",
            "通知值班主管评估是否需停电检修",
            "持续监测接点温度与三相电流",
            "检查近期负荷变化与散热情况",
            "准备急救药品待命",  # 新增
        ],
        "supervisor_id": "sup-001",
        "comment": "增加急救药品准备，其他措施不变",
    }
    final = await graph.ainvoke(Command(resume=decision), config=config)
    print(f"final: {final}")
    print("*" * 80)
    td = final["supervisor_decision"]
    print(f"supervisor_decision: {td}")
    print("✅ modify 用例通过")


if __name__ == "__main__":
    # asyncio.run(atest_review_approve())
    asyncio.run(atest_review_modify())
