# backend/api/v1/work_flow.py
# 照护流程 Agent API 路由

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend.dependencies import get_current_user
from backend.core.orchestrator import get_orchestrator, AgentRequest
from backend.core.logger import get_logger

router = APIRouter(prefix="/work-flow")
logger = get_logger(__name__)


class WorkFlowStartRequest(BaseModel):
    """启动照护流程请求。"""
    site_id:     str
    service_plan_id: str = ""
    initial_stage: str = "onboarding_assessment"
    discharge_order: dict | None = None  # SaaS 退住工单（入住时已知要出院可注入；None=续住）


class WorkFlowChatRequest(BaseModel):
    """照护流程对话请求。"""
    session_id: str
    message:    str
    discharge_order: dict | None = None  # SaaS 随时下发的退住工单（阶段复评时据此决定出院 or 续住）


class DischargeOrderRequest(BaseModel):
    """SaaS 主动下发退住工单（不推进对话）。"""
    order: dict  # 如 {"order_type": "discharge", "reason": "转院"}


class WebhookConfigRequest(BaseModel):
    """租户出站回调配置（Agent → SaaS 的通知地址）。"""
    url:     str                # SaaS 侧接收回调的 HTTP(S) 地址
    secret:  str | None = None  # HMAC 密钥：省略/None=不改动；""=清空（不签名）；非空=覆盖
    enabled: bool = True        # 关闭后事件只落发件箱、不投递


def _require_admin(current_user: dict) -> None:
    """出站回调配置是管理操作（会决定数据流向哪个外部地址），限 supervisor / admin。"""
    if (current_user.get("role") or "") not in ("supervisor", "admin"):
        raise HTTPException(status_code=403, detail="需要 supervisor / admin 角色")


def _mask_secret(secret: str) -> str:
    """密钥只回显前缀，避免接口把完整密钥吐回前端。"""
    if not secret:
        return ""
    return f"{secret[:4]}****" if len(secret) > 4 else "****"


@router.post("/start")
async def start_work_flow(
    req: WorkFlowStartRequest,
    current_user: dict = Depends(get_current_user),
):
    """启动照护流程会话。"""
    session_id = f"cf-{uuid.uuid4().hex[:12]}"

    orchestrator = get_orchestrator()
    request = AgentRequest(
        staff_id=current_user["user_id"],
        site_id=req.site_id,
        tenant_id=current_user["tenant_id"],
        session_id=session_id,
        agent_type="work_flow",
        mode="single",
        context={
            "service_plan_id": req.service_plan_id,
            "current_stage": req.initial_stage,
            "discharge_order": req.discharge_order,
        },
    )
    result = await orchestrator.execute(request)

    # 会话登记（业务侧权威记录）：失败只记日志，不阻断流程发起
    from backend.db.repositories import create_work_flow_session
    await create_work_flow_session(
        session_id=session_id,
        site_id=req.site_id,
        staff_id=current_user["user_id"],
        current_stage=result.get("current_stage", req.initial_stage),
        tenant_id=current_user["tenant_id"],
    )

    messages = result.get("messages", [])
    ai_message = ""
    if messages:
        last = messages[-1]
        if hasattr(last, "content"):
            ai_message = last.content if isinstance(last.content, str) else str(last.content)

    return {
        "session_id": session_id,
        "current_stage": result.get("current_stage", req.initial_stage),
        "ai_message": ai_message,
        "structured_output": result.get("structured_output"),
    }


@router.post("/chat")
async def work_flow_chat(
    req: WorkFlowChatRequest,
    current_user: dict = Depends(get_current_user),
):
    """照护流程对话（SSE 流式响应）。"""
    async def event_generator():
        yield {"event": "session", "data": json.dumps({"session_id": req.session_id}, ensure_ascii=False)}

        try:
            orchestrator = get_orchestrator()
            # 从会话登记表找回 site_id：/chat 请求不带 site_id，
            # 缺了它 thread_id 会退到 staff_id 拼键，与 /start 写入的检查点对不上。
            from backend.db.repositories import get_work_flow_session
            session_row = await get_work_flow_session(
                req.session_id, tenant_id=current_user["tenant_id"])
            request = AgentRequest(
                staff_id=current_user["user_id"],
                site_id=(session_row or {}).get("site_id", ""),
                tenant_id=current_user["tenant_id"],
                session_id=req.session_id,
                agent_type="work_flow",
                mode="single",
                query=req.message,
                context={"discharge_order": req.discharge_order} if req.discharge_order else {},
            )
            result = await orchestrator.execute(request)

            # 每轮同步会话进度到登记表（失败只记日志，不阻断对话）
            from backend.db.repositories import update_work_flow_session_progress
            await update_work_flow_session_progress(
                session_id=req.session_id,
                current_stage=result.get("current_stage", ""),
                total_turns=result.get("total_turn_count", 0),
                tenant_id=current_user["tenant_id"],
            )

            messages = result.get("messages", [])
            ai_message = ""
            if messages:
                last = messages[-1]
                if hasattr(last, "content"):
                    ai_message = last.content if isinstance(last.content, str) else str(last.content)

            yield {
                "event": "message",
                "data": json.dumps({
                    "content": ai_message,
                    "current_stage": result.get("current_stage", ""),
                    "daily_mode": result.get("daily_mode", "normal"),
                    "total_turn_count": result.get("total_turn_count", 0),
                }, ensure_ascii=False),
            }

            if result.get("current_stage") == "finished":
                yield {
                    "event": "report",
                    "data": json.dumps(result.get("report", {}), ensure_ascii=False),
                }
        except Exception as e:
            yield {"event": "error", "data": json.dumps({"error": str(e)}, ensure_ascii=False)}

        yield {"event": "done", "data": ""}

    return EventSourceResponse(event_generator())


@router.get("/sessions/{session_id}/plan-overview")
async def get_plan_overview(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    """照护计划总览：护理等级 + 服务项 + 当天计划与执行进度（一个接口给足前端）。

    为什么单独做一个聚合接口而不是让前端拼三个：
      ① 前端拿到的"计划来源"必须和后端 Agent 用的是同一个来源标记
         （service_items / ai_assessment / merged / missing），拼不出来；
      ② plan_missing 时要给引导，前端需要一眼判断，避免在缺计划时渲染空表格。

    注意：这里**不跑 LangGraph**（不调 LLM），只读 Provider，所以很快；
    计划缺失时返回 plan_missing=true + 空列表，由前端展示引导。
    """
    from backend.core.service_plan_provider import ServicePlanProviderError, get_service_plan_provider
    from backend.db.repositories import daily_plan_progress, get_work_flow_session

    session_row = await get_work_flow_session(session_id, tenant_id=current_user["tenant_id"])
    if not session_row:
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")

    site_id = session_row.get("site_id", "")
    tenant_id = current_user["tenant_id"]
    provider = get_service_plan_provider()

    plan, plan_source, service_items, today_plan = None, "missing", [], []
    try:
        bundle = await provider.fetch(site_id, tenant_id)
        plan, plan_source = bundle.plan, bundle.plan_source
        service_items = (plan or {}).get("service_items") or []
    except ServicePlanProviderError as e:
        logger.error("work_flow.plan_overview_provider_failed", site_id=site_id, error=str(e))
    except Exception as e:  # noqa: BLE001
        logger.warning("work_flow.plan_overview_failed", site_id=site_id, error=str(e))

    plan_missing = not plan
    if not plan_missing:
        try:
            today_plan = await provider.today_plan(site_id, tenant_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("work_flow.plan_overview_today_failed", site_id=site_id, error=str(e))

    return {
        "session_id": session_id,
        "site_id": site_id,
        "plan_missing": plan_missing,
        "plan_source": plan_source,
        "risk_tier": (plan or {}).get("risk_tier", ""),
        "special_notes": (plan or {}).get("special_notes", ""),
        "service_items": service_items,
        "today_plan": today_plan,
        "progress": daily_plan_progress(today_plan),
        "message": "" if not plan_missing else (
            "该老人当前没有照护计划：请先完成能力评估，或在机构侧登记照护服务项。"
        ),
    }


@router.get("/sessions/{session_id}/today-plan")
async def get_today_plan(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    """查该会话对应老人**当天**的照护计划与执行进度。

    用途：护理员的手持机/前端进日常巡检时先拉一次，知道"今天还剩哪几项没做"；
    care 侧对计划只读（生成规则在业务侧），只有 confirm 才回写。
    """
    from backend.db.repositories import daily_plan_progress, get_work_flow_session
    from backend.core.service_plan_provider import get_service_plan_provider

    session_row = await get_work_flow_session(session_id, tenant_id=current_user["tenant_id"])
    if not session_row:
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")

    site_id = session_row.get("site_id", "")
    plan = await get_service_plan_provider().today_plan(site_id, current_user["tenant_id"])
    return {
        "session_id": session_id,
        "site_id": site_id,
        "plan": plan,
        "progress": daily_plan_progress(plan),
    }


class PlanItemConfirmRequest(BaseModel):
    """护工执行回写（对应业务系统侧的执行结果回写接口）。"""
    status:           str = "done"   # done=完成 / missed=该做没做
    worker_name:       str = ""
    actual_care_time: str = ""


@router.post("/plan-items/{plan_item_id}/confirm")
async def confirm_plan_item(
    plan_item_id: str,
    req: PlanItemConfirmRequest,
    current_user: dict = Depends(get_current_user),
):
    """护工确认某项照护任务执行完成 → 回写状态与执行人。

    两种 Provider 的落点不同：本地实现写 care 自己的 daily_work_plans；
    SaaS 实现调主系统的「执行结果回写」接口。写的是"执行结果"这类事实数据，
    不涉及业务规则，所以不违背"care 不重复实现计划生成"的分工。
    """
    from backend.core.service_plan_provider import get_service_plan_provider

    if req.status not in ("done", "missed"):
        raise HTTPException(status_code=400, detail="status 只能是 done 或 missed")
    ok = await get_service_plan_provider().confirm_plan_item(
        plan_item_id, current_user["tenant_id"],
        status=req.status,
        worker_id=current_user["user_id"],
        worker_name=req.worker_name or current_user["user_id"],
        actual_care_time=req.actual_care_time,
    )
    if not ok:
        raise HTTPException(status_code=404, detail=f"计划条目不存在: {plan_item_id}")
    logger.info("work_flow.plan_item_confirmed", plan_item_id=plan_item_id,
                status=req.status, operator=current_user["user_id"])
    return {"plan_item_id": plan_item_id, "status": req.status, "confirmed": True}


@router.get("/active/{site_id}")
async def get_active_work_flow(
    site_id: str,
    current_user: dict = Depends(get_current_user),
):
    """按老人查最近一场进行中的照护流程会话。

    用途：护理员从老人详情页进入时，前端先调这里；有进行中的会话就提示
    "继续未完成流程"，拿回 session_id 进入对话页即可命中检查点续跑，
    不依赖浏览器记住 URL。租户隔离：老人不归属当前租户视为不存在。
    """
    from backend.db.repositories import get_active_work_flow_session
    row = await get_active_work_flow_session(site_id, tenant_id=current_user["tenant_id"])
    if row is None:
        return {"active": False, "session": None}
    return {"active": True, "session": row}


@router.post("/sessions/{session_id}/discharge-order")
async def deliver_discharge_order_api(
    session_id: str,
    req: DischargeOrderRequest,
    current_user: dict = Depends(get_current_user),
):
    """SaaS 主动下发退住工单到指定会话（不推进对话，等护理员下次开口时生效）。

    与 MCP 工具 /mcp/work-flow 的 send_discharge_order 共用同一段落地逻辑
    （backend.agents.work_flow.discharge.deliver_discharge_order）：
    只写会话检查点里的 state.discharge_order，护理员下一次 /chat 时由状态机识别并收敛。
    """
    from backend.agents.work_flow.discharge import deliver_discharge_order
    result = await deliver_discharge_order(
        session_id=session_id,
        tenant_id=current_user["tenant_id"],
        order=req.order,
    )
    if not result.get("found"):
        raise HTTPException(status_code=404, detail=result.get("message", "会话不存在"))
    return result


# ── 出站通知（Agent → SaaS）：回调配置 / 事件审计 / 手动重投 ──

@router.put("/webhooks")
async def upsert_webhook(
    req: WebhookConfigRequest,
    current_user: dict = Depends(get_current_user),
):
    """登记 / 更新本租户的回调地址：流程终态报告会 POST 到这里。

    只改配置，不做连通性校验（不回连 SaaS）；真正的投递由后台投递器异步完成。
    """
    _require_admin(current_user)
    if not req.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="url 必须是 http(s) 地址")

    from backend.db.repositories import upsert_tenant_webhook
    ok = await upsert_tenant_webhook(
        tenant_id=current_user["tenant_id"],
        url=req.url,
        secret=req.secret,
        enabled=req.enabled,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="回调配置写入失败")
    return {"configured": True, "url": req.url, "enabled": req.enabled}


@router.get("/webhooks")
async def get_webhook(current_user: dict = Depends(get_current_user)):
    """查看本租户回调配置（密钥打码）。"""
    from backend.db.repositories import get_tenant_webhook
    row = await get_tenant_webhook(current_user["tenant_id"])
    if row is None:
        return {"configured": False, "webhook": None}
    return {
        "configured": True,
        "webhook": {
            "url": row["url"],
            "secret": _mask_secret(row["secret"]),
            "enabled": row["enabled"],
            "updated_at": row["updated_at"],
        },
    }


@router.get("/events")
async def list_outbound_events_api(
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
):
    """列出本租户的出站事件（最近优先）：投递状态、尝试次数、失败原因。"""
    from backend.db.repositories import list_outbound_events
    rows = await list_outbound_events(
        tenant_id=current_user["tenant_id"],
        limit=min(max(limit, 1), 200),
    )
    return {"total": len(rows), "events": rows}


@router.post("/events/{event_id}/redeliver")
async def redeliver_event(
    event_id: str,
    current_user: dict = Depends(get_current_user),
):
    """把一条失败/已发事件重置为 pending，由投递器重新投递（人工兜底）。"""
    _require_admin(current_user)
    from backend.db.repositories import reset_outbound_event_for_redelivery
    ok = await reset_outbound_event_for_redelivery(
        event_id, tenant_id=current_user["tenant_id"])
    if not ok:
        raise HTTPException(status_code=404, detail=f"事件不存在: {event_id}")
    return {"event_id": event_id, "status": "pending",
            "message": "已重置为待投递，投递器下一轮会重发"}


@router.get("/{session_id}/report")
async def get_work_report(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    """按会话 ID 查询照护评估报告（读 work_flow_reports 表，租户隔离）。"""
    from backend.db.repositories import get_work_flow_report
    row = await get_work_flow_report(session_id, tenant_id=current_user["tenant_id"])
    if row is None:
        raise HTTPException(status_code=404, detail=f"照护报告不存在: {session_id}")

    return {
        "session_id": row["session_id"],
        "site_id": row["site_id"],
        "service_plan_id": row.get("service_plan_id", ""),
        "stage_reached": row.get("stage_reached", ""),
        "total_turns": row.get("total_turns", 0),
        "status": "completed",
        "report": row.get("report", {}),
        "created_at": row.get("created_at"),
    }


@router.get("/{session_id}/report/export")
async def export_work_report_docx(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    """把作业流程评估报告导出为 Word（.docx）。"""
    from backend.core.export import build_work_flow_docx, docx_response_headers
    from backend.db.repositories import get_work_flow_report

    row = await get_work_flow_report(session_id, tenant_id=current_user["tenant_id"])
    if row is None:
        raise HTTPException(status_code=404, detail=f"照护报告不存在: {session_id}")

    content = build_work_flow_docx(row)
    filename = f"照护报告-{session_id}.docx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=docx_response_headers(filename, ascii_name=f"work-report-{session_id}.docx"),
    )
