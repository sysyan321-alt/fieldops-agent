# backend/core/outbound.py
# 出站通知投递器：把发件箱（outbound_events）里的事件 POST 到租户回调地址（SaaS 侧）。
#
# 为什么是「发件箱 + 后台投放」而不是在业务流程里直接 httpx.post：
#   ① 外部系统抖动不该拖慢 / 搞挂作业流程（最后一步不该被网络拖死）；
#   ② 进程崩溃会丢事件——先落库再投递，重启后自动续投；
#   ③ 需要重试、幂等、审计（谁在什么时候投了几次、失败原因）。
# 投递语义是「至少一次」：SaaS 侧用 X-FieldOps-Event-Id 做消费端去重。
#
# 与上行（SaaS → Agent）的对称关系：
#   上行 = 请求体 / 专属回调把业务信号写进 state（discharge_order）；
#   下行 = 本模块把 Agent 的终态结果推回 SaaS（work_flow.finished）。

import asyncio
import contextlib
import hashlib
import hmac
import json
from datetime import datetime, timezone

import httpx

from backend.config import get_settings
from backend.core.logger import get_logger
from backend.db import repositories as repo

logger = get_logger(__name__)

# ── 事件类型 ─────────────────────────────────────────────────
EVENT_WORK_FLOW_FINISHED = "work_flow.finished"   # 作业流程走到终态、报告已落库
EVENT_ALERT_DISPATCHED = "alert.dispatched"       # 告警研判通过值班主管确认、工单已派发
EVENT_ALERT_DISMISSED = "alert.dismissed"         # 告警被值班主管取消处置（不派单）

# ── 重试策略 ─────────────────────────────────────────────────
# 指数退避（秒）：第 n 次投递失败后等待 _BACKOFF_SECONDS[n-1]；
# 投递次数达到 MAX_ATTEMPTS 仍失败 → 置 failed（保留记录，可人工重投）。
_BACKOFF_SECONDS = (30, 120, 600, 1800)
MAX_ATTEMPTS = len(_BACKOFF_SECONDS) + 1


def sign_payload(secret: str, body: bytes) -> str:
    """HMAC-SHA256 十六进制签名。secret 为空返回空串（SaaS 侧跳过校验）。"""
    if not secret:
        return ""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def build_headers(event: dict, body: bytes, secret: str) -> dict:
    """构造投递头：事件标识 + 时间戳 + 可选 HMAC 签名。"""
    headers = {
        "Content-Type": "application/json",
        "X-FieldOps-Event": str(event.get("event_type", "")),
        "X-FieldOps-Event-Id": str(event.get("event_id", "")),
        "X-FieldOps-Delivery-Attempt": str(int(event.get("attempts", 0)) + 1),
        "X-FieldOps-Timestamp": str(int(datetime.now(timezone.utc).timestamp())),
    }
    signature = sign_payload(secret, body)
    if signature:
        headers["X-FieldOps-Signature"] = f"sha256={signature}"
    return headers


async def deliver_event(event: dict, webhook: dict) -> tuple[bool, str]:
    """投递单个事件一次。返回 (是否成功, 失败原因)。"""
    settings = get_settings()
    body = json.dumps(event.get("payload", {}), ensure_ascii=False).encode("utf-8")
    headers = build_headers(event, body, webhook.get("secret", ""))
    try:
        async with httpx.AsyncClient(timeout=settings.outbound_timeout) as client:
            resp = await client.post(webhook["url"], content=body, headers=headers)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    if 200 <= resp.status_code < 300:
        return True, ""
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


async def dispatch_pending(limit: int | None = None) -> dict:
    """扫描一轮发件箱并投递，返回统计（后台循环与测试共用）。"""
    settings = get_settings()
    events = await repo.list_pending_outbound_events(
        limit=limit or settings.outbound_batch_size)
    stats = {"scanned": len(events), "sent": 0, "retried": 0,
             "failed": 0, "skipped": 0}
    webhook_cache: dict[str, dict | None] = {}

    for event in events:
        tenant_id = event["tenant_id"]
        if tenant_id not in webhook_cache:
            webhook_cache[tenant_id] = await repo.get_tenant_webhook(tenant_id)
        webhook = webhook_cache[tenant_id]

        # 未配置 / 已停用：保留 pending（不计入重试次数），等配好地址后自动补投
        if not webhook or not webhook.get("enabled") or not webhook.get("url"):
            stats["skipped"] += 1
            logger.warning("outbound.no_webhook_configured",
                           event_id=event["event_id"], tenant_id=tenant_id)
            continue

        ok, error = await deliver_event(event, webhook)
        if ok:
            await repo.mark_outbound_event_sent(event["event_id"])
            stats["sent"] += 1
            logger.info("outbound.sent", event_id=event["event_id"],
                        event_type=event["event_type"], url=webhook["url"])
            continue

        attempts = int(event.get("attempts", 0)) + 1
        if attempts >= MAX_ATTEMPTS:
            await repo.mark_outbound_event_failed(event["event_id"], error)
            stats["failed"] += 1
            logger.warning("outbound.failed", event_id=event["event_id"],
                           attempts=attempts, error=error)
        else:
            delay = _BACKOFF_SECONDS[min(attempts - 1, len(_BACKOFF_SECONDS) - 1)]
            await repo.mark_outbound_event_retry(event["event_id"], error, delay)
            stats["retried"] += 1
            logger.warning("outbound.retry_scheduled", event_id=event["event_id"],
                           attempts=attempts, delay_seconds=delay, error=error)

    return stats


# ── 业务侧便捷入口 ───────────────────────────────────────────

async def enqueue_work_flow_finished(
    session_id: str,
    site_id: str,
    tenant_id: str,
    service_plan_id: str,
    stage_reached: str,
    total_turns: int,
    report: dict,
    discharge_driven: bool = False,
) -> str | None:
    """作业流程终态 → 落一条 work_flow.finished 事件（报告已随载荷内联）。

    幂等：同一 session 重复落库不会产生第二条（发件箱唯一索引兜底）。
    """
    payload = {
        "event": EVENT_WORK_FLOW_FINISHED,
        "session_id": session_id,
        "site_id": site_id,
        "service_plan_id": service_plan_id or "",
        "stage_reached": stage_reached or "",
        "total_turns": int(total_turns or 0),
        "discharge_driven": bool(discharge_driven),   # 是否由 SaaS 退住工单驱动
        "report": report or {},
        "report_url": f"/api/v1/work-flow/{session_id}/report",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    return await repo.enqueue_outbound_event(
        event_type=EVENT_WORK_FLOW_FINISHED,
        aggregate_id=session_id,
        payload=payload,
        tenant_id=tenant_id,
    )


async def enqueue_alert_decision(
    alert_id: str,
    site_id: str,
    tenant_id: str,
    dispatch_order: dict,
    device_id: str = "",
    alert_type: str = "",
    external_alert_id: str = "",
    source: str = "local",
    handled_by: str = "",
    device_type: str = "",
    location: str = "",
) -> str | None:
    """告警处置终态 → 落一条出站事件（派单或取消）。

    与 work_flow.finished 共用同一发件箱与投递器：告警是"AI 研判 + 人审决断"的链路，
    值班主管一按确认，工单就该同步到业务系统，而不是让 SaaS 轮询告警接口。
    幂等键用 order_id（每张派发工单唯一）；dismiss 不派单但仍通知业务方"已取消处置"。

    external_alert_id 是业务系统 报警记录 ID（报警记录）：SaaS 下发的报警经 care 研判处置后，
    它要按这个 ID 回写处置结果，
    所以必须原样带回，不能让 SaaS 做二次匹配。
    """
    status = (dispatch_order or {}).get("status", "dispatched")
    if status == "dismissed":
        event_type = EVENT_ALERT_DISMISSED
    else:
        event_type = EVENT_ALERT_DISPATCHED

    payload = {
        "event": event_type,
        "alert_id": alert_id,
        "site_id": site_id,
        "order_id": (dispatch_order or {}).get("order_id", ""),
        "device_id": device_id,
        "device_type": device_type,
        "alert_type": alert_type,
        # 对接业务系统 报警记录：来源、SaaS 报警 ID、报警位置、处理人
        "source": source or "local",
        "external_alert_id": external_alert_id or "",
        "location": location or "",
        "handled_by": handled_by or (dispatch_order or {}).get("supervisor_id", ""),
        "risk_level": (dispatch_order or {}).get("risk_level", ""),
        "actions": (dispatch_order or {}).get("actions", []),
        "order_content": (dispatch_order or {}).get("order_content", ""),
        "supervisor_id": (dispatch_order or {}).get("supervisor_id", ""),
        "status": status,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    aggregate_id = (dispatch_order or {}).get("order_id") or alert_id
    return await repo.enqueue_outbound_event(
        event_type=event_type,
        aggregate_id=aggregate_id,
        payload=payload,
        tenant_id=tenant_id,
    )


# ── 后台投递循环 ─────────────────────────────────────────────

_worker_task: asyncio.Task | None = None


async def _worker_loop(interval: int) -> None:
    logger.info("outbound.worker_started", interval=interval)
    while True:
        try:
            await dispatch_pending()
        except asyncio.CancelledError:
            raise
        except Exception as e:   # 单轮异常不能把循环打死
            logger.warning("outbound.worker_error", error=str(e))
        await asyncio.sleep(interval)


def start_outbound_worker() -> None:
    """随应用启动后台投递器（幂等：已在跑则不重复启动）。"""
    global _worker_task
    settings = get_settings()
    if not settings.outbound_enabled:
        logger.info("outbound.worker_disabled")
        return
    if _worker_task and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(
        _worker_loop(settings.outbound_worker_interval))


async def stop_outbound_worker() -> None:
    """关闭后台投递器（应用 shutdown 时调用）。"""
    global _worker_task
    if _worker_task is None:
        return
    _worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await _worker_task
    _worker_task = None
    logger.info("outbound.worker_stopped")
