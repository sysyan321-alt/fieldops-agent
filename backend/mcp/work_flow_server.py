# backend/mcp/work_flow_server.py
# 作业流程 MCP Server（写操作，需 Token 鉴权）
#
# 端点：挂载在 /mcp/work-flow（POST /mcp/work-flow/mcp），外层套 BearerTokenMiddleware
# 独立运行：python -m backend.mcp.work_flow_server（默认 8016 端口，仍需自行加门禁）
#
# 工具能力与 REST 接口一一对应，落地逻辑全部复用现有实现，不另写一份：
#   POST /api/v1/work-flow/start                              → start_work_flow
#   POST /api/v1/work-flow/sessions/{sid}/discharge-order     → send_discharge_order
#   GET  /api/v1/work-flow/active/{site_id}（会话状态）        → get_work_flow_status
#   GET  /api/v1/work-flow/{sid}/report                       → get_work_flow_report
#
# 与 alert-ops 的关键差异：告警是"无状态一次性触发"（trigger_alert 新建 alert_id 再跑图）；
# 作业流程是"有状态多轮会话"，所以 send_discharge_order 只往已有 thread 的检查点写
# state.discharge_order（不跑图、不耗 LLM），等作业人员下一次对话时状态机自然收敛。

import uuid

from mcp.server.fastmcp import FastMCP

from backend.core.logger import get_logger
from backend.mcp.auth import assert_tenant_allowed

logger = get_logger(__name__)

mcp = FastMCP(
    name="FieldOps-WorkFlow",
    instructions=(
        "FieldOps 作业能力工具 · 作业流程（v1.0，写工具，需 Authorization: Bearer <MCP_ADMIN_TOKEN>）。"
        "工具：start_work_flow（启动作业流程会话）、send_discharge_order（向已有会话下发退住工单，"
        "不推进对话、待作业人员下次发言时生效）、get_work_flow_status（查会话阶段）、"
        "get_work_flow_report（取服务/出院报告）、confirm_service_plan_item（护工确认某项服务任务执行完成）。"
        "写工具（start_work_flow / send_discharge_order / confirm_service_plan_item）"
        "必须传 tenant_id 且在 MCP_WRITE_TENANTS 白名单内。"
    ),
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
async def start_work_flow(
    site_id: str,
    tenant_id: str,
    service_plan_id: str = "",
    initial_stage: str = "onboarding_assessment",
    discharge_order: dict | None = None,
) -> dict:
    """启动一场作业流程会话（建档评估 → 日常巡检 → 阶段复评 → 退场评估）。

    对应 REST：POST /api/v1/work-flow/start

    Args:
        site_id:        点位 ID
        tenant_id:       租户 ID，必填且必须在 MCP_WRITE_TENANTS 白名单内
        service_plan_id:    服务计划 ID（可选）
        initial_stage:   起始阶段，默认 onboarding_assessment
        discharge_order: 可选退住工单（入住时已知要出院可一并下发）

    Returns:
        {session_id, current_stage, ai_message}
    """
    assert_tenant_allowed(tenant_id)

    from backend.core.orchestrator import AgentRequest, get_orchestrator
    from backend.db.repositories import create_work_flow_session

    session_id = f"cf-{uuid.uuid4().hex[:12]}"
    orchestrator = get_orchestrator()
    result = await orchestrator.execute(AgentRequest(
        staff_id="mcp-client",
        site_id=site_id,
        tenant_id=tenant_id,
        session_id=session_id,
        agent_type="work_flow",
        mode="single",
        context={
            "service_plan_id": service_plan_id,
            "current_stage": initial_stage,
            "discharge_order": discharge_order,
        },
    ))

    current_stage = result.get("current_stage", initial_stage)
    # 会话登记（业务侧权威记录）：失败只记日志，不阻断流程发起
    await create_work_flow_session(
        session_id=session_id,
        site_id=site_id,
        staff_id="mcp-client",
        current_stage=current_stage,
        tenant_id=tenant_id,
    )

    messages = result.get("messages", [])
    ai_message = ""
    if messages:
        last = messages[-1]
        if hasattr(last, "content"):
            ai_message = last.content if isinstance(last.content, str) else str(last.content)

    logger.info("mcp.work_flow.started", session_id=session_id, site_id=site_id,
                tenant_id=tenant_id, stage=current_stage)
    return {
        "session_id": session_id,
        "current_stage": current_stage,
        "ai_message": ai_message,
    }


@mcp.tool()
async def send_discharge_order(
    session_id: str,
    tenant_id: str,
    order: dict,
) -> dict:
    """向进行中的作业流程会话下发退住工单（SaaS 主动通知）。

    对应 REST：POST /api/v1/work-flow/sessions/{session_id}/discharge-order

    只更新该会话检查点里的 state.discharge_order，**不推进对话**：作业人员下一次发言时，
    check_stage_node 会识别工单并触发阶段收敛（当前阶段达 min 即并入 复评 → 退场评估）。

    Args:
        session_id: 作业流程会话 ID（由 start_work_flow 或 GET /active/{site_id} 获得）
        tenant_id:  租户 ID，必填且必须在白名单内
        order:      退住工单 dict，如 {"order_type": "discharge", "reason": "转院"}

    Returns:
        {found, session_id, status, message}；found=False 表示会话不存在/跨租户/已结束。
    """
    assert_tenant_allowed(tenant_id)

    from backend.agents.work_flow.discharge import deliver_discharge_order
    result = await deliver_discharge_order(session_id, tenant_id, order)
    logger.info("mcp.work_flow.discharge_order", session_id=session_id,
                tenant_id=tenant_id, status=result.get("status"))
    return result


@mcp.tool()
async def get_work_flow_status(session_id: str, tenant_id: str) -> dict:
    """查询作业流程会话的当前状态（阶段 / 轮数 / 是否进行中）。

    Args:
        session_id: 作业流程会话 ID
        tenant_id:  租户 ID，必填

    Returns:
        会话状态；会话不存在或跨租户时返回 {"found": false}
    """
    from backend.db.repositories import get_work_flow_session
    row = await get_work_flow_session(session_id, tenant_id=tenant_id)
    if row is None:
        return {"found": False, "session_id": session_id}
    return {"found": True, **row}


@mcp.tool()
async def get_work_flow_report(session_id: str, tenant_id: str) -> dict:
    """取出院/服务评估报告（流程走到 finished 时生成）。

    对应 REST：GET /api/v1/work-flow/{session_id}/report

    Args:
        session_id: 作业流程会话 ID
        tenant_id:  租户 ID，必填

    Returns:
        报告内容；未生成或跨租户时返回 {"found": false}
    """
    from backend.db.repositories import get_work_flow_report as fetch_report
    row = await fetch_report(session_id, tenant_id=tenant_id)
    if row is None:
        return {"found": False, "session_id": session_id}
    return {"found": True, **row}


@mcp.tool()
async def confirm_service_plan_item(
    plan_item_id: str,
    tenant_id: str,
    status: str = "done",
    worker_name: str = "",
    actual_work_time: str = "",
) -> dict:
    """护工确认某项服务任务执行完成（回写执行结果）。

    对应 REST：POST /api/v1/work-flow/plan-items/{plan_item_id}/confirm
    对齐业务系统侧的执行结果回写接口。

    这里回写的是**执行结果**这类事实数据（谁在什么时候做了/没做），
    不是业务规则——服务项目与频次仍由业务侧决定，本服务 不改。
    漏做（status=missed）会进入服务历史的异常记录，供下一轮对话与复评参考。

    Args:
        plan_item_id:     计划条目 ID（来自 get_today_service_plan）
        tenant_id:        租户 ID，必填且必须在白名单内
        status:           done=已完成 / missed=该做没做
        worker_name:       执行人姓名（便于审计，可空）
        actual_work_time: 实际执行时间，如 "08:05"

    Returns:
        {confirmed, plan_item_id, status}；条目不存在或跨租户时 confirmed=False
    """
    assert_tenant_allowed(tenant_id)

    if status not in ("done", "missed"):
        return {"confirmed": False, "plan_item_id": plan_item_id,
                "message": "status 只能是 done 或 missed"}

    from backend.core.service_plan_provider import get_service_plan_provider

    ok = await get_service_plan_provider().confirm_plan_item(
        plan_item_id, tenant_id, status=status, worker_id="mcp-client",
        worker_name=worker_name or "mcp-client", actual_work_time=actual_work_time,
    )
    logger.info("mcp.work_flow.plan_item_confirmed", plan_item_id=plan_item_id,
                tenant_id=tenant_id, status=status, confirmed=ok)
    return {
        "confirmed": ok,
        "plan_item_id": plan_item_id,
        "status": status,
        "message": "" if ok else "计划条目不存在或不属于该租户",
    }


if __name__ == "__main__":
    import uvicorn

    port = 8016
    print(f"作业流程 MCP Server → http://localhost:{port}/mcp（独立运行时请自行加鉴权）")
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=port)
