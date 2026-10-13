# backend/api/v1/alert.py
# 告警研判 Agent API 路由（HitL：trigger / review / confirm）

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from langgraph.types import Command

from backend.dependencies import get_current_user
from backend.core.orchestrator import get_orchestrator, AgentRequest
from backend.core.memory import build_config
from backend.core.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/alert")


class AlertTriggerRequest(BaseModel):
    """告警触发请求。"""
    site_id:        str
    device_id:       str
    alert_type:      str = "heart_rate_abnormal"
    alert_timestamp: str = ""


class AlertConfirmRequest(BaseModel):
    """站长确认请求。"""
    action:           str   # approve / modify / dismiss
    modified_actions: list[dict] = []
    supervisor_id:    str
    comment:          str = ""


class AlertIngestRequest(BaseModel):
    """机构业务系统侧报警记录的下发请求。

    报警的权威源在业务系统（设备告警记录由它维护），care 只做研判与处置建议；
    处置终态经发件箱回传，业务系统按 `external_alert_id` 关联并回写处置结果。
    字段名（如报警内容、报警位置、处理人）请按你自己的后端契约调整。
    """
    external_alert_id: str           # 业务系统的报警记录 ID（回写关联与幂等键）
    device_id:       str
    site_id:        str
    device_type:     str = ""             # SaaS 的设备类型（中文名或编码，care 侧解析）
    alert_content:   str = ""             # 报警内容
    location:        str = ""             # 报警位置
    alert_time:      str = ""             # 报警时间
    alert_type:      str = ""             # 可选：SaaS 已归类时直接带编码


def _infer_alert_type(device_type: str, alert_content: str) -> str:
    """SaaS 只给设备类型与报警内容时，推一个 care 的告警类型编码。

    规则简单、可解释：先按设备类型查它可能产生哪些告警，再用报警内容里的
    关键词收敛到具体一种；都判不出就用设备类型的第一种可能，最后兜底 heart_rate_abnormal。
    """
    from backend.core.device_types import device_types_for_alert, resolve_device_type

    text = f"{alert_content or ''}"
    keyword_map = {
        "heart_rate_abnormal": ("心率", "心跳", "脉搏"),
        "breath_abnormal": ("呼吸", "喘息"),
        "off_bed_timeout": ("离床", "下床", "不在床"),
        "fall_detected": ("跌倒", "摔倒", "坠床"),
        "room_temp_abnormal": ("温度", "高温", "低温", "湿度"),
        "sos_pressed": ("SOS", "sos", "呼叫", "求助"),
        "wander_detected": ("外出", "走失", "离院", "出院门"),
        "vital_abnormal": ("血压", "血氧", "体温"),
        "activity_low": ("活动量", "久坐", "不动"),
        "crowd_density": ("拥挤", "聚集", "人流"),
    }
    for code, words in keyword_map.items():
        if any(w in text for w in words):
            return code

    dt = resolve_device_type(device_type)
    if dt and dt.alert_types:
        return dt.alert_types[0]
    return "heart_rate_abnormal"


@router.post("/trigger")
async def trigger_alert(
    req: AlertTriggerRequest,
    current_user: dict = Depends(get_current_user),
):
    """触发告警研判，返回 interrupt 暂停点数据。"""
    if current_user["role"] not in ("worker", "supervisor", "admin"):
        raise HTTPException(status_code=403, detail="无权限")

    alert_id = f"alert-{uuid.uuid4().hex[:12]}"
    session_id = alert_id

    orchestrator = get_orchestrator()
    request = AgentRequest(
        staff_id=current_user["user_id"],
        site_id=req.site_id,
        tenant_id=current_user["tenant_id"],
        session_id=session_id,
        agent_type="alert",
        mode="single",
        context={
            "alert_id": alert_id,
            "device_id": req.device_id,
            "alert_type": req.alert_type,
            "alert_timestamp": req.alert_timestamp,
        },
    )

    result = await orchestrator.execute(request)

    # 图在 interrupt 处暂停，返回 display_data
    return {
        "alert_id": alert_id,
        "session_id": session_id,
        "status": "awaiting_review",
        "review_data": result,
    }


async def _run_alert_assessment(
    *,
    alert_id: str,
    session_id: str,
    site_id: str,
    device_id: str,
    alert_type: str,
    alert_timestamp: str,
    current_user: dict,
    source: str,
    external_alert_id: str = "",
    alert_location: str = "",
    device_type: str = "",
) -> dict:
    """触发一次告警研判（trigger 与 SaaS 下行 ingest 共用）。"""
    orchestrator = get_orchestrator()
    request = AgentRequest(
        staff_id=current_user["user_id"],
        site_id=site_id,
        tenant_id=current_user["tenant_id"],
        session_id=session_id,
        agent_type="alert",
        mode="single",
        context={
            "alert_id": alert_id,
            "device_id": device_id,
            "alert_type": alert_type,
            "alert_timestamp": alert_timestamp,
            "source": source,
            "external_alert_id": external_alert_id,
            "alert_location": alert_location,
            "device_type": device_type,
        },
    )
    return await orchestrator.execute(request)


@router.post("/ingest")
async def ingest_alert(
    req: AlertIngestRequest,
    current_user: dict = Depends(get_current_user),
):
    """机构 SaaS 下发报警记录 → care 研判（下行接入）。

    与 /trigger 的区别：/trigger 是 care 自己造 alert_id（演示/自触发），
    /ingest 接收业务系统的报警记录并保留 external_alert_id，处置终态经发件箱回传
    由业务系统回写——报警的双向闭环。

    幂等：同一 (租户, external_alert_id) 只会研判一次；重复下发直接返回既有研判。
    """
    if current_user["role"] not in ("worker", "supervisor", "admin"):
        raise HTTPException(status_code=403, detail="无权限")

    tenant_id = current_user["tenant_id"]
    from backend.db.repositories import find_alert_by_external_id

    existing = await find_alert_by_external_id(req.external_alert_id, tenant_id=tenant_id)
    if existing:
        logger.info("alert.ingest_duplicate", external_alert_id=req.external_alert_id,
                    alert_id=existing["alert_id"])
        return {
            "alert_id": existing["alert_id"],
            "external_alert_id": req.external_alert_id,
            "status": existing["status"],
            "duplicated": True,
            "message": "该报警已研判过，返回既有结果（幂等）",
        }

    alert_type = req.alert_type or _infer_alert_type(req.device_type, req.alert_content)
    alert_id = f"alert-{uuid.uuid4().hex[:12]}"
    result = await _run_alert_assessment(
        alert_id=alert_id, session_id=alert_id, site_id=req.site_id,
        device_id=req.device_id, alert_type=alert_type,
        alert_timestamp=req.alert_time, current_user=current_user,
        source="saas", external_alert_id=req.external_alert_id, alert_location=req.location,
        device_type=req.device_type,
    )
    logger.info("alert.ingested", external_alert_id=req.external_alert_id, alert_id=alert_id,
                alert_type=alert_type, device_type=req.device_type)
    return {
        "alert_id": alert_id,
        "external_alert_id": req.external_alert_id,
        "alert_type": alert_type,
        "source": "saas",
        "status": "awaiting_review",
        "review_data": result,
    }


@router.get("/{alert_id}/review")
async def get_alert_review(
    alert_id: str,
    current_user: dict = Depends(get_current_user),
):
    """获取告警审核数据（从 alerts 表读取风险评估 + 四轨数据，租户隔离）。"""
    if current_user["role"] not in ("supervisor", "admin"):
        raise HTTPException(status_code=403, detail="仅站长可审核")

    from backend.db.repositories import get_alert_review as fetch_review
    row = await fetch_review(alert_id, tenant_id=current_user["tenant_id"])
    if row is None:
        raise HTTPException(status_code=404, detail=f"告警记录不存在: {alert_id}")

    return {
        "alert_id": row["alert_id"],
        "site_id": row["site_id"],
        "device_id": row["device_id"],
        "alert_type": row["alert_type"],
        "status": row["status"],
        "risk_level": row["risk_level"],
        "risk_assessment": row.get("risk_assessment", {}),
        "device_readings": row.get("three_tracks", {}).get("device_readings", []),
        "service_records": row.get("three_tracks", {}).get("service_records", []),
        "health_indicators": row.get("three_tracks", {}).get("health_indicators", []),
        "assessment_snapshot": row.get("three_tracks", {}).get("assessment_snapshot"),
        # 证据完整性：站长要能一眼看出"这次研判有几轨是真的有数据"
        "device_profile": row.get("three_tracks", {}).get("device_profile") or {},
        "evidence_quality": row.get("three_tracks", {}).get("evidence_quality", ""),
        "evidence_gaps": row.get("three_tracks", {}).get("evidence_gaps", []),
        # 对接机构 SaaS 报警记录的字段（SaaS 下发的报警要能按 external_alert_id 追溯与回写）
        "source": row.get("source", "local"),
        "external_alert_id": row.get("external_alert_id", ""),
        "location": row.get("location", ""),
        "handled_by": row.get("handled_by", ""),
        "handled_at": row.get("handled_at"),
        "created_at": row.get("created_at"),
        "message": "请确认风险评估并派发工单",
    }


@router.post("/{alert_id}/confirm")
async def confirm_alert(
    alert_id: str,
    req: AlertConfirmRequest,
    current_user: dict = Depends(get_current_user),
):
    """站长确认告警处置，恢复图执行。"""
    if current_user["role"] not in ("supervisor", "admin"):
        raise HTTPException(status_code=403, detail="仅站长可确认")

    # 租户守卫：告警不属于当前租户时拒绝（视为不存在，不泄露跨租户数据）
    from backend.db.repositories import get_alert_review as fetch_review
    alert_row = await fetch_review(alert_id, tenant_id=current_user["tenant_id"])
    if alert_row is None:
        raise HTTPException(status_code=404, detail=f"告警记录不存在: {alert_id}")

    orchestrator = get_orchestrator()
    from backend.agents.alert.graph import build_alert_graph
    graph = await build_alert_graph()

    # thread_id 必须与触发时一致：Orchestrator 用 build_config(site_id, session_id) 构造，
    # 而告警场景 session_id == alert_id，所以这里必须传 site_id 而不是 alert_id
    # agent_type 也要一致：它写进 config 的 checkpoint_ns，两端不匹配同样找不到断点
    config = build_config(alert_row["site_id"], alert_id, agent_type="alert")
    decision = {
        "action": req.action,
        "modified_actions": req.modified_actions,
        "supervisor_id": req.supervisor_id or current_user["user_id"],
        "comment": req.comment,
    }

    result = await graph.ainvoke(Command(resume=decision), config=config)

    return {
        "alert_id": alert_id,
        "status": "dispatched" if req.action != "dismiss" else "dismissed",
        "dispatch_order": result.get("dispatch_order"),
        "structured_output": result.get("structured_output"),
    }
