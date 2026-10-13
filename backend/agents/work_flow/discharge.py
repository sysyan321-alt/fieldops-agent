# backend/agents/work_flow/discharge.py
# 退住工单投递：把 SaaS 下发的工单写入指定会话的检查点状态（不推进对话）。
#
# 设计取舍：工单是"业务指令"而非对话内容——
#   投递只更新 LangGraph 检查点里的 state.discharge_order，不跑图、不耗 LLM、不生成 AI 回复；
#   等作业人员下一次 /chat 时由 check_stage_node 识别并触发阶段收敛（复评 → 退场评估）。
#
# 这样 SaaS 就能"主动通知"一个进行中的会话，而不必等作业人员开口时把字段捎带进来。
# MCP 工具（/mcp/work-flow 的 send_discharge_order）与 REST 回调
# （POST /api/v1/work-flow/sessions/{sid}/discharge-order）共用本函数，落地逻辑只有一份。

from backend.core.logger import get_logger
from backend.core.memory import build_config
from backend.db.repositories import get_work_flow_session

logger = get_logger(__name__)

# 会话已终结的状态：工单需在新会话中下发，不再写入
_FINISHED_STATUS = ("finished", "completed")


async def deliver_discharge_order(
    session_id: str,
    tenant_id: str,
    order: dict,
) -> dict:
    """向进行中的作业流程会话注入退住工单（只写检查点，不跑图）。

    Args:
        session_id: 作业流程会话 ID
        tenant_id:  租户 ID（强制过滤，跨租户视为不存在）
        order:      退住工单 dict（如 {"order_type": "discharge", "reason": "转院"}），不可为空

    Returns:
        {"found": bool, "session_id": str, "status": str, "message": str, ...}
        found=False 表示会话不存在 / 跨租户 / 已结束 / 写入失败。
    """
    if not order:
        raise ValueError("discharge order 不能为空（dict 需至少含 order_type）")

    row = await get_work_flow_session(session_id, tenant_id=tenant_id)
    if row is None:
        logger.info("work_flow.discharge_order_session_not_found",
                    session_id=session_id, tenant_id=tenant_id)
        return {
            "found": False,
            "session_id": session_id,
            "status": "not_found",
            "message": "会话不存在或不属于该租户",
        }

    if (row.get("status") or "") in _FINISHED_STATUS:
        logger.info("work_flow.discharge_order_session_finished",
                    session_id=session_id, status=row.get("status"))
        return {
            "found": False,
            "session_id": session_id,
            "status": "already_finished",
            "message": "会话已结束，工单需在新会话中下发",
        }

    try:
        # 局部导入：避免模块加载期就构建图（与 alert-ops 的 confirm_alert 同风格）
        from backend.agents.work_flow.graph import build_work_flow_graph
        graph = await build_work_flow_graph()
        # ★ thread_id / checkpoint_ns 必须与 Orchestrator 完全一致
        #   （orchestrator.py 拼键为 build_config(site_id or staff_id, session_id, agent_type)），
        #   否则工单会被写进另一行检查点，作业人员那边永远读不到。
        config = build_config(row["site_id"], session_id, agent_type="work_flow")
        await graph.aupdate_state(config, {"discharge_order": order})
    except Exception as e:
        logger.error("work_flow.discharge_order_delivery_failed",
                     session_id=session_id, error=str(e))
        return {
            "found": False,
            "session_id": session_id,
            "status": "error",
            "message": f"工单写入检查点失败: {e}",
        }

    logger.info("work_flow.discharge_order_delivered",
                session_id=session_id, site_id=row["site_id"],
                tenant_id=tenant_id, stage=row.get("current_stage"))
    return {
        "found": True,
        "session_id": session_id,
        "site_id": row["site_id"],
        "current_stage": row.get("current_stage"),
        "status": "delivered",
        "message": "工单已投递；作业人员下一次对话时由状态机识别并收敛到 复评 → 退场评估",
    }
