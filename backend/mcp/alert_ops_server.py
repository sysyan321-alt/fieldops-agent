# backend/mcp/alert_ops_server.py
# 告警运营 MCP Server（写操作，需 Token 鉴权）
#
# 端点：挂载在 /mcp/alert-ops（POST /mcp/alert-ops/mcp），外层套 BearerTokenMiddleware
# 独立运行：python -m backend.mcp.alert_ops_server（默认 8014 端口，仍需自行加门禁）
#
# 工具能力与 REST 接口一一对应，落地逻辑全部复用现有实现，不另写一份：
#   POST /api/v1/alert/trigger          → trigger_alert
#   GET  /api/v1/alert/{id}/review      → get_alert_review_data
#   POST /api/v1/alert/{id}/confirm     → confirm_alert
#   （新增）列出待审核告警              → list_pending_alerts

import uuid

from langgraph.types import Command
from mcp.server.fastmcp import FastMCP
from sqlalchemy import text

from backend.core.logger import get_logger
from backend.core.memory import build_config
from backend.core.orchestrator import AgentRequest, get_orchestrator
from backend.db.repositories import get_alert_review as fetch_alert_review
from backend.dependencies import AsyncSessionLocal
from backend.mcp.auth import assert_tenant_allowed

logger = get_logger(__name__)

mcp = FastMCP(
    name="FieldOps-AlertOps",
    instructions=(
        "FieldOps 护理能力工具 · 告警触发与派单（v1.0，写工具，需 Authorization: Bearer <MCP_ADMIN_TOKEN>）。"
        "工具：trigger_alert（四轨采集+风险评估，在站长审核处暂停）、get_alert_review_data（读审核数据）、"
        "confirm_alert（站长确认后派发工单）、list_pending_alerts（列待审核告警）。"
        "所有工具必须传 tenant_id 且在 MCP_WRITE_TENANTS 白名单内。"
    ),
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
async def trigger_alert(
    site_id: str,
    device_id: str,
    tenant_id: str,
    alert_type: str = "heart_rate_abnormal",
    alert_timestamp: str = "",
) -> dict:
    """触发告警研判（跑四轨采集 + 风险评估，图会在站长审核处暂停）。

    对应 REST：POST /api/v1/alert/trigger

    Args:
        site_id:        老人 ID
        device_id:       触发告警的设备 ID
        tenant_id:       租户 ID，必填且必须在 MCP_WRITE_TENANTS 白名单内
        alert_type:      告警类型：heart_rate_abnormal / off_bed_timeout /
                         fall_detected / room_temp_abnormal
        alert_timestamp: 告警时间（可选，ISO 字符串）

    Returns:
        {alert_id, session_id, status="awaiting_review", review_data}
        review_data 内含风险评估与四轨采集结果，可直接展示给站长审核。
    """
    assert_tenant_allowed(tenant_id)

    alert_id = f"alert-{uuid.uuid4().hex[:12]}"
    orchestrator = get_orchestrator()
    result = await orchestrator.execute(AgentRequest(
        staff_id="mcp-client",
        site_id=site_id,
        tenant_id=tenant_id,
        session_id=alert_id,
        agent_type="alert",
        mode="single",
        context={
            "alert_id": alert_id,
            "device_id": device_id,
            "alert_type": alert_type,
            "alert_timestamp": alert_timestamp,
        },
    ))

    logger.info("mcp.alert_ops.triggered", alert_id=alert_id, site_id=site_id,
                tenant_id=tenant_id)
    return {
        "alert_id": alert_id,
        "session_id": alert_id,
        "status": "awaiting_review",
        "review_data": result,
    }


@mcp.tool()
async def get_alert_review_data(alert_id: str, tenant_id: str) -> dict:
    """读取告警审核数据（风险评估 + 四轨数据，租户隔离）。

    对应 REST：GET /api/v1/alert/{alert_id}/review

    Args:
        alert_id:  告警 ID
        tenant_id: 租户 ID，必填

    Returns:
        告警详情；告警不存在或跨租户时返回 {"found": false, "alert_id": ...}
    """
    row = await fetch_alert_review(alert_id, tenant_id=tenant_id)
    if row is None:
        return {"found": False, "alert_id": alert_id}

    tracks = row.get("three_tracks") or {}
    return {
        "found": True,
        "alert_id": row["alert_id"],
        "site_id": row["site_id"],
        "device_id": row["device_id"],
        "alert_type": row["alert_type"],
        "status": row["status"],
        "risk_level": row["risk_level"],
        "risk_assessment": row.get("risk_assessment", {}),
        "device_readings": tracks.get("device_readings", []),
        "service_records": tracks.get("service_records", []),
        "health_indicators": tracks.get("health_indicators", []),
        "assessment_snapshot": tracks.get("assessment_snapshot"),
        "created_at": str(row.get("created_at", "")),
    }


@mcp.tool()
async def confirm_alert(
    alert_id: str,
    tenant_id: str,
    action: str,
    modified_actions: list[dict] | None = None,
    comment: str = "",
    supervisor_id: str = "mcp-client",
) -> dict:
    """站长确认告警处置（恢复被 interrupt 暂停的图，生成并派发工单）。

    对应 REST：POST /api/v1/alert/{alert_id}/confirm

    Args:
        alert_id:         告警 ID
        tenant_id:        租户 ID，必填且必须在白名单内
        action:           决策：approve（采用 AI 措施）/ modify（采用修改后措施）/ dismiss（取消处置）
        modified_actions: action=modify 时的措施列表，形如 [{"action": "...", "owner": "..."}]
        comment:          站长备注
        supervisor_id:    操作人标识（MCP 场景默认 mcp-client）

    Returns:
        {alert_id, status, dispatch_order}
    """
    assert_tenant_allowed(tenant_id)
    if action not in ("approve", "modify", "dismiss"):
        raise ValueError(f"action 只能是 approve / modify / dismiss，收到：{action}")

    # 租户守卫：跨租户告警视为不存在，避免通过 MCP 改别人租户的数据
    row = await fetch_alert_review(alert_id, tenant_id=tenant_id)
    if row is None:
        return {"found": False, "alert_id": alert_id, "message": "告警不存在或不属于该租户"}

    from backend.agents.alert.graph import build_alert_graph

    graph = await build_alert_graph()
    decision = {
        "action": action,
        "modified_actions": modified_actions or [],
        "supervisor_id": supervisor_id,
        "comment": comment,
    }
    # thread_id 必须与触发时（Orchestrator 的 build_config(site_id, session_id)）一致，
    # 否则恢复时找不到断点，apply_supervisor_decision / dispatch_order 都不会执行
    # agent_type 同理：它决定 config 里的 checkpoint_ns
    result = await graph.ainvoke(
        Command(resume=decision),
        config=build_config(row["site_id"], alert_id, agent_type="alert"),
    )

    logger.info("mcp.alert_ops.confirmed", alert_id=alert_id, action=action, tenant_id=tenant_id)
    return {
        "found": True,
        "alert_id": alert_id,
        "status": "dismissed" if action == "dismiss" else "dispatched",
        "dispatch_order": result.get("dispatch_order"),
    }


@mcp.tool()
async def list_pending_alerts(tenant_id: str, limit: int = 10) -> list[dict]:
    """列出待站长审核的告警（status = reviewing）。

    Args:
        tenant_id: 租户 ID，必填
        limit:     返回条数，默认 10
    """
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(
            "SELECT alert_id, site_id, device_id, alert_type, risk_level, created_at"
            " FROM alerts WHERE tenant_id = :tid AND status = 'reviewing'"
            " ORDER BY created_at DESC NULLS LAST LIMIT :lim"),
            {"tid": tenant_id, "lim": limit})).fetchall()

    logger.info("mcp.alert_ops.pending_listed", tenant_id=tenant_id, rows=len(rows))
    return [
        {
            "alert_id": r.alert_id,
            "site_id": r.site_id,
            "device_id": r.device_id,
            "alert_type": r.alert_type,
            "risk_level": r.risk_level,
            "created_at": str(r.created_at),
        }
        for r in rows
    ]


if __name__ == "__main__":
    import uvicorn

    port = 8014
    print(f"告警运营 MCP Server → http://localhost:{port}/mcp（独立运行时请自行加鉴权）")
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=port)
