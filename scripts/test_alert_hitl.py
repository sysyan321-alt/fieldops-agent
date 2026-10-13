# scripts/test_alert_hitl.py
# 告警研判 Agent 端到端 HitL 测试
# 完整跑通：四轨并行采集 → 风险评估 → interrupt暂停 → 值班主管确认 → 派发工单
#
# 用法：cd <项目根目录> && python scripts/test_alert_hitl.py

import asyncio
import json
from datetime import datetime

from langgraph.types import Command

from backend.agents.alert.graph import build_alert_graph
from backend.core.memory import build_config
from backend.core.logger import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)


# ── 模拟告警初始状态 ──────────────────────────────────────────

ALERT_ID = "alert-test-001"
ELDER_ID = "s001"
SESSION_ID = ALERT_ID  # 用 alert_id 同时做 session_id，保证 thread_id 一致

INITIAL_STATE = {
    "messages": [],
    "site_id":        ELDER_ID,
    "tenant_id":       "tenant_default",
    "alert_id":        ALERT_ID,
    "session_id":      SESSION_ID,
    "device_id":       "dev-001",
    "alert_type":      "heart_rate_abnormal",
    "alert_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "device_readings":    [],
    "service_records":       [],
    "health_indicators":  [],
    "risk_assessment":    None,
    "supervisor_notified": False,
    "supervisor_decision": None,
    "dispatch_order":     None,
    "published":          False,
    "fallback_used":      False,
    "structured_output":  None,
}

# thread_id 必须在 trigger 和 confirm 之间保持一致
CONFIG = build_config(ELDER_ID, SESSION_ID, agent_type="alert")


async def run_full_hitl_flow():
    """完整跑通告警研判 HitL 链路。"""

    print("\n" + "=" * 60)
    print("  告警研判 Agent 端到端 HitL 测试")
    print("=" * 60)

    # ── 第一步：构建图并首次调用，跑到 interrupt() 暂停 ──────

    graph = await build_alert_graph()
    print(f"\n[1] 图构建成功，开始首次调用（trigger）...")
    print(f"    alert_id={ALERT_ID}")
    print(f"    thread_id={CONFIG['configurable']['thread_id']}")

    result = await graph.ainvoke(INITIAL_STATE, config=CONFIG)

    # 图在 supervisor_review_node 的 interrupt() 处暂停
    # ainvoke 返回的是当前 State 快照（包含 interrupt 暴露的数据）
    print(f"\n[2] 图已在 interrupt() 处暂停，等待值班主管确认")
    print(f"    当前 State 关键字段：")

    risk = result.get("risk_assessment", {})
    print(f"    - risk_level:      {risk.get('risk_level', 'N/A')}")
    print(f"    - risk_score:      {risk.get('risk_score', 'N/A')}")
    print(f"    - reasoning:       {risk.get('reasoning', 'N/A')}")
    print(f"    - recommended_actions:")
    for i, action in enumerate(risk.get("recommended_actions", []), 1):
        print(f"        {i}. {action}")
    print(f"    - confidence:      {risk.get('confidence', 'N/A')}")
    print(f"    - supervisor_notified: {result.get('supervisor_notified', False)}")

    # ── 第二步：值班主管确认（approve），用 Command(resume=...) 恢复 ──

    decision = {
        "action":           "approve",
        "modified_actions": [],
        "supervisor_id":    "sup-001",
        "comment":          "同意 AI 风险评估，立即派发工单",
    }

    print(f"\n[3] 值班主管确认：approve")
    print(f"    decision={json.dumps(decision, ensure_ascii=False, indent=2)}")

    final = await graph.ainvoke(Command(resume=decision), config=CONFIG)

    # ── 第三步：打印最终结果 ─────────────────────────────────

    print(f"\n[4] 图执行完成，最终结果：")
    print(f"    - published:       {final.get('published', False)}")
    print(f"    - fallback_used:   {final.get('fallback_used', False)}")

    dispatch = final.get("dispatch_order", {})
    print(f"\n    ── 派发工单 ──")
    print(f"    alert_id:    {dispatch.get('alert_id', 'N/A')}")
    print(f"    site_id:    {dispatch.get('site_id', 'N/A')}")
    print(f"    risk_level:  {dispatch.get('risk_level', 'N/A')}")
    print(f"    status:      {dispatch.get('status', 'N/A')}")
    print(f"    supervisor:  {dispatch.get('supervisor_id', 'N/A')}")

    print(f"\n    工单内容：")
    print(f"    {'-' * 50}")
    print(f"    {dispatch.get('order_content', 'N/A')}")

    print(f"\n    最终处置措施：")
    for i, action in enumerate(dispatch.get("actions", []), 1):
        print(f"    {i}. {action}")

    print(f"\n    ── structured_output ──")
    so = final.get("structured_output", {})
    print(f"    alert_id:        {so.get('alert_id', 'N/A')}")
    print(f"    published:       {so.get('published', 'N/A')}")
    if so.get("dispatch_order"):
        print(f"    dispatch_order:  (见上方)")

    print("\n" + "=" * 60)
    print("  ✅ 告警研判 HitL 链路跑通！")
    print("=" * 60)

    return final


async def run_modify_flow():
    """用例②：值班主管修改处置措施后恢复。"""

    print("\n" + "=" * 60)
    print("  告警研判 Agent - 用例②：值班主管修改措施")
    print("=" * 60)

    graph = await build_alert_graph()
    config = build_config(ELDER_ID, "alert-test-002", agent_type="alert")
    state = {**INITIAL_STATE, "alert_id": "alert-test-002", "session_id": "alert-test-002"}

    # 首次调用跑到 interrupt
    result = await graph.ainvoke(state, config=config)

    risk = result.get("risk_assessment", {})
    print(f"\n[1] AI 风险评估结果：{risk.get('risk_level')} (score={risk.get('risk_score')})")
    print(f"    原始建议措施：")
    for i, a in enumerate(risk.get("recommended_actions", []), 1):
        print(f"    {i}. {a}")

    # 值班主管修改：增加一条急救准备
    decision = {
        "action": "modify",
        "modified_actions": risk.get("recommended_actions", []) + ["准备急救药品待命"],
        "supervisor_id": "sup-001",
        "comment": "增加急救药品准备，其他措施不变",
    }

    print(f"\n[2] 值班主管修改措施：增加「准备急救药品待命」")

    final = await graph.ainvoke(Command(resume=decision), config=config)

    dispatch = final.get("dispatch_order", {})
    print(f"\n[3] 最终工单措施：")
    for i, a in enumerate(dispatch.get("actions", []), 1):
        print(f"    {i}. {a}")
    print(f"\n    status: {dispatch.get('status')}")

    print("\n" + "=" * 60)
    print("  ✅ modify 用例跑通！")
    print("=" * 60)


if __name__ == "__main__":
    # 用例①：approve（完整链路）
    asyncio.run(run_full_hitl_flow())

    # 用例②：modify
    asyncio.run(run_modify_flow())
