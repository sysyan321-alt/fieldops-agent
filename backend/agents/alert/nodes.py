# backend/agents/alert/nodes.py
# 告警研判 Agent 的所有节点函数

import asyncio
import json
import uuid

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import interrupt

from backend.agents.alert.state import (
    AlertState, RiskAssessment, SupervisorDecision,
)
from backend.agents.alert.prompts import (
    SYSTEM_PROMPT, RISK_ASSESS_PROMPT, DISPATCH_ORDER_PROMPT,
)
from backend.core.llm_factory import get_llm, get_structured_llm
from backend.core.logger import get_logger

logger = get_logger(__name__)

# 处置措施兜底：LLM 的结构化输出会漏掉 recommended_actions，原因是字段在 Pydantic 里
# 有默认值（default_factory=list），生成 tool schema 时它就进不了 required 列表，
# 走 function_calling 的模型可以"合法地"不输出这个字段——校验也不会报错。
# 实测：三次真实告警里有两次该字段为空，工单因此变成"措施：[]"。
#
# 为什么不用"把字段改成必填"来解决：改成 min_length=1 之后，模型一旦不输出就会抛
# ValidationError，整次评估被丢进 except 分支，risk_level / reasoning 全部降级成默认中风险——
# 那等于用一个可选字段的格式问题换掉整份评估结论。更划算的做法是保留宽松 schema、
# 在节点里按风险等级补保底动作：模型的具体措施优先，缺失才补通用动作，并打警告留下可观测信号。
DEFAULT_ACTIONS_BY_RISK: dict[str, list[str]] = {
    "high":   ["立即派人现场查看点位状况", "通知值班医生评估是否需要转诊", "持续监测生命体征并记录"],
    "medium": ["尽快派人查看点位状况", "监测生命体征并记录变化"],
    "low":    ["按常规巡视安排观察", "记录点位状态变化"],
}
DEFAULT_ACTIONS_FALLBACK: list[str] = ["立即派人现场查看点位状况", "持续监测生命体征并记录"]


def ensure_actions(risk_level: str, actions: list[str] | None) -> list[str]:
    """保证处置措施非空：为空时按风险等级回落保底动作，非空时原样保留。

    只做"补空"，不做"改写"——LLM 给出的具体措施优先级永远高于通用兜底。
    """
    cleaned = [a.strip() for a in (actions or []) if isinstance(a, str) and a.strip()]
    if cleaned:
        return cleaned
    return list(DEFAULT_ACTIONS_BY_RISK.get(risk_level, DEFAULT_ACTIONS_FALLBACK))


def _get_message_content(msg) -> str:
    """统一获取消息文本内容"""
    if hasattr(msg, "text") and not callable(getattr(msg, "text", None)):
        return msg.text
    if isinstance(msg.content, str):
        return msg.content
    return str(msg.content)


# ──────────────────────────────────────────────────────────────
# 四轨数据采集的独立函数（被 run_four_tracks_node 并行调用）
# ──────────────────────────────────────────────────────────────

async def resolve_device_profile(state: AlertState) -> dict:
    """解析触发告警的设备：类型编码、证据可信度、安装位置。

    优先级（顺序即证据相关度）：
      ① 告警自带的设备类型声明（SaaS 下发 报警记录 时会带"设备类型"）——
         它描述的是"这台报警设备是干什么的"，与本次告警最相关；
      ② 设备表登记的 type_code / device_type 自由字符串（历史数据）；
      ③ 按告警类型反查通常由哪类设备产生（兜底推断）。
    认不出就是认不出——返回 code 为空，研判侧会按"来源不明，证据可信度 3"处理。
    """
    from backend.core.device_types import device_types_for_alert, resolve_device_type
    from backend.db.repositories import get_device

    device_id = state.get("device_id", "")
    tenant_id = state.get("tenant_id") or "tenant_default"
    profile: dict = {"device_id": device_id, "type_code": "", "type_name": "",
                     "trust_level": 3, "location": "", "source": "unknown"}

    declared = resolve_device_type(state.get("device_type_declared"))
    row = await get_device(device_id, tenant_id=tenant_id) if device_id else None
    if row:
        profile["location"] = row.get("location", "")

    registered = None
    if row:
        registered = resolve_device_type(row.get("type_code")) or resolve_device_type(row.get("device_type"))

    # 告警自带的声明优先；与设备登记不一致时记录下来——这通常意味着设备换了或登记过期，
    # 静默采信任一方都会让"证据可信度"失真，所以不一致必须留痕。
    if declared:
        if registered and registered.code != declared.code:
            logger.warning("resolve_device_profile.type_mismatch",
                           device_id=device_id, declared=declared.code,
                           registered=registered.code,
                           note="采信告警声明，设备登记可能过期")
        profile.update(type_code=declared.code, type_name=declared.name,
                       trust_level=declared.trust_level, source="alert_declared")
        if not profile["location"] and row:
            profile["location"] = row.get("location", "")
        logger.info("resolve_device_profile.done", device_id=device_id,
                    type_code=profile["type_code"], source=profile["source"])
        return profile

    if registered:
        profile.update(type_code=registered.code, type_name=registered.name,
                       trust_level=registered.trust_level, source="device_table")
        logger.info("resolve_device_profile.done", device_id=device_id,
                    type_code=profile["type_code"], source=profile["source"])
        return profile

    candidates = device_types_for_alert(state.get("alert_type"))
    if candidates:
        dt = resolve_device_type(candidates[0])
        if dt:
            profile.update(type_code=dt.code, type_name=dt.name,
                           trust_level=dt.trust_level, source="alert_type_inferred")
    logger.info("resolve_device_profile.done", device_id=device_id,
                type_code=profile["type_code"], source=profile["source"])
    return profile


async def _fetch_device_data(state: AlertState) -> list[dict]:
    """第一轨：采集设备读数。

    改造前这里在库里查不到数据时**现场编造模拟读数**（电流永远 320A、水位永远正常），
    导致研判结论由硬编码常量决定。现在：查不到就返回空，由 run_four_tracks 记成
    「证据缺失」，让风险等级体现"证据不足"，而不是让模型对着假数据下结论。
    """
    from backend.core.device_types import metrics_of
    from backend.db.iot_repo import get_recent_device_readings

    site_id = state.get("site_id", "")
    device_id = state.get("device_id", "")
    tenant_id = state.get("tenant_id") or "tenant_default"
    profile = state.get("device_profile") or {}

    # 先按告警设备 + 该设备类型的指标取；取不到再放宽到该点位全部设备
    metrics = metrics_of(profile.get("type_code")) or None
    readings = await get_recent_device_readings(
        site_id, limit=10, tenant_id=tenant_id, device_id=device_id, metrics=metrics)
    if not readings and metrics:
        readings = await get_recent_device_readings(
            site_id, limit=10, tenant_id=tenant_id, metrics=metrics)
    if not readings:
        readings = await get_recent_device_readings(
            site_id, limit=10, tenant_id=tenant_id, device_id=device_id)
    if not readings:
        readings = await get_recent_device_readings(site_id, limit=10, tenant_id=tenant_id)

    logger.info("fetch_device_data.done", site_id=site_id, device_id=device_id,
                count=len(readings), filtered_by_metrics=bool(metrics and readings))
    return readings


async def _fetch_service_records(state: AlertState) -> list[dict]:
    """第二轨：采集近期服务记录（只读真实记录，查不到返回空让研判知道"没有记录"）。"""
    site_id = state.get("site_id", "")
    from backend.db.iot_repo import get_recent_service_records
    records = await get_recent_service_records(
        site_id, limit=8, tenant_id=state.get("tenant_id") or "tenant_default")
    logger.info("fetch_service_records.done", site_id=site_id, count=len(records))
    return records


async def _fetch_health_indicators(state: AlertState) -> list[dict]:
    """第三轨：采集健康指标（只读真实读数，查不到返回空）。"""
    from backend.db.iot_repo import get_recent_health_indicators
    indicators = await get_recent_health_indicators(
        state.get("site_id", ""), limit=8,
        tenant_id=state.get("tenant_id") or "tenant_default")
    logger.info("fetch_health_indicators.done", count=len(indicators))
    return indicators


# ──────────────────────────────────────────────────────────────
# 第四轨：点位最近一次风险评估（DB 读取）
# ──────────────────────────────────────────────────────────────

def _format_assessment_snapshot(snapshot: dict | None) -> str:
    """把评估快照压成几行文本喂给研判 LLM（只保留对风险判断有用的部分）。"""
    if not snapshot:
        return "（暂无评估记录）"

    summary = snapshot.get("summary") or {}
    dimensions = snapshot.get("dimension_scores") or []
    dim_text = " / ".join(
        f"{d.get('dimension', d.get('name', ''))} {d.get('score', '')}"
        for d in dimensions if isinstance(d, dict)
    ) or "（无维度明细）"
    issues = summary.get("issues") or summary.get("problem_list") or []
    issue_text = "；".join(
        str(i.get("suggestion") or i.get("description") or i) if isinstance(i, dict) else str(i)
        for i in issues[:5]
    ) or "（无问题清单）"

    return (
        f"- 评估时间：{snapshot.get('created_at', '未知')}\n"
        f"- 加权总分：{snapshot.get('weighted_score', '未知')}\n"
        f"- 维度得分：{dim_text}\n"
        f"- 风险等级建议：{summary.get('risk_tier_recommendation', '未给出')}\n"
        f"- 主要问题：{issue_text}"
    )


async def _fetch_assessment_snapshot(state: AlertState) -> dict | None:
    """第四轨：读取点位最近一次风险评估，作为风险研判的基线背景。

    没有评估记录不算失败：返回 None，让研判只按当前三轨判断（第一至第三轨）。
    """
    from backend.db.repositories import get_latest_assessment

    site_id = state.get("site_id", "")
    tenant_id = state.get("tenant_id") or "tenant_default"
    if not site_id:
        return None

    snapshot = await get_latest_assessment(site_id, tenant_id=tenant_id)
    if snapshot:
        logger.info("fetch_assessment_snapshot.done",
                    assessment_id=snapshot.get("assessment_id", ""),
                    weighted_score=snapshot.get("weighted_score"))
    else:
        logger.info("fetch_assessment_snapshot.empty", site_id=site_id)
    return snapshot


# ──────────────────────────────────────────────────────────────
# 节点：fetch_device_data_node / fetch_service_records_node / fetch_health_indicators_node
# （独立暴露，供 run_four_tracks_node 并行调用）
# ──────────────────────────────────────────────────────────────

async def fetch_device_data_node(state: AlertState) -> dict:
    """第一轨节点：采集设备读数。"""
    readings = await _fetch_device_data(state)
    return {"device_readings": readings}


async def fetch_service_records_node(state: AlertState) -> dict:
    """第二轨节点：采集服务记录。"""
    records = await _fetch_service_records(state)
    return {"service_records": records}


async def fetch_health_indicators_node(state: AlertState) -> dict:
    """第三轨节点：采集健康指标。"""
    indicators = await _fetch_health_indicators(state)
    return {"health_indicators": indicators}


async def fetch_assessment_snapshot_node(state: AlertState) -> dict:
    """第四轨节点：读取点位最近一次风险评估。"""
    snapshot = await _fetch_assessment_snapshot(state)
    return {"assessment_snapshot": snapshot}


# ──────────────────────────────────────────────────────────────
# 节点：run_four_tracks — 四轨并行采集
# ──────────────────────────────────────────────────────────────

async def run_four_tracks_node(state: AlertState) -> dict:
    """
    四轨并行数据采集：
        第一轨：设备读数
        第二轨：服务记录
        第三轨：健康指标
        第四轨：点位最近一次风险评估（跌倒风险 / ADL / 认知等基线）

    为什么要第四轨：设备告警只说明"当下异常"，评估结果说明"这个人本来有多脆弱"——
    风险等级高或历史缺陷多的点位，同样的过载告警要按更高等级处理。

    为什么要记「证据缺失」：三条数据轨都可能查不到数据（设备没上报、护工没记录、
    点位没做过评估）。缺证据是真实状态，必须显式传给研判——改造前是现场编造模拟数据，
    等于用硬编码常量冒充现场证据，风险等级会因此系统性失真。

    asyncio.gather(return_exceptions=True)：
        某一轨抛异常不会中断其他轨（第四轨没有评估记录时返回 None，不算失败）。
    """
    logger.info("four_tracks.start", alert_type=state.get("alert_type", ""))

    # 先解析设备谱系（类型编码 / 证据可信度 / 安装位置），第一轨按它决定采什么指标
    profile = await resolve_device_profile(state)
    enriched = {**state, "device_profile": profile}

    raw = await asyncio.gather(
        _fetch_device_data(enriched),
        _fetch_service_records(enriched),
        _fetch_health_indicators(enriched),
        _fetch_assessment_snapshot(enriched),
        return_exceptions=True,
    )

    device_readings = raw[0] if not isinstance(raw[0], Exception) else []
    service_records = raw[1] if not isinstance(raw[1], Exception) else []
    health_indicators = raw[2] if not isinstance(raw[2], Exception) else []
    assessment_snapshot = raw[3] if not isinstance(raw[3], Exception) else None

    for name, exc in zip(["device", "service", "health", "assessment"], raw):
        if isinstance(exc, Exception):
            logger.error(f"four_tracks.{name}_failed", error=str(exc))

    # 证据完整性：缺得越多，越不能把风险判低（见 assess_risk 的提示词约束）
    gaps = []
    if not device_readings:
        gaps.append("设备读数")
    if not service_records:
        gaps.append("服务记录")
    if not health_indicators:
        gaps.append("健康指标")
    if not assessment_snapshot:
        gaps.append("最近一次风险评估")
    if len(gaps) >= 3:
        evidence_quality = "insufficient"
    elif gaps:
        evidence_quality = "partial"
    else:
        evidence_quality = "complete"

    logger.info(
        "four_tracks.done",
        device=len(device_readings),
        service=len(service_records),
        health=len(health_indicators),
        assessment=bool(assessment_snapshot),
        evidence_quality=evidence_quality,
        gaps=gaps,
    )

    return {
        "device_readings": device_readings,
        "service_records": service_records,
        "health_indicators": health_indicators,
        "assessment_snapshot": assessment_snapshot,
        "device_profile": profile,
        "evidence_gaps": gaps,
        "evidence_quality": evidence_quality,
    }


# ──────────────────────────────────────────────────────────────
# 节点：aggregate_tracks — 汇总四轨数据
# ──────────────────────────────────────────────────────────────

async def aggregate_tracks_node(state: AlertState) -> dict:
    """汇总四轨采集结果，为风险评估准备数据。"""
    device_readings = state.get("device_readings", [])
    service_records = state.get("service_records", [])
    health_indicators = state.get("health_indicators", [])

    logger.info(
        "aggregate_tracks.done",
        device=len(device_readings),
        service=len(service_records),
        health=len(health_indicators),
    )
    return {}  # 四轨数据已在 State 中，无需额外处理


# ──────────────────────────────────────────────────────────────
# 节点：assess_risk — 风险评估（LLM 结构化输出）
# ──────────────────────────────────────────────────────────────

async def assess_risk_node(state: AlertState) -> dict:
    """用 LLM 结构化输出进行风险评估。"""
    alert_type = state.get("alert_type", "unknown")
    alert_timestamp = state.get("alert_timestamp", "")
    device_readings = state.get("device_readings", [])
    service_records = state.get("service_records", [])
    health_indicators = state.get("health_indicators", [])

    # 格式化四轨数据为文本
    device_text = "\n".join([
        f"- {r.get('metric_name') or r.get('device_type', '')}: "
        f"{r.get('reading_value', '')}{r.get('reading_unit', '')} "
        f"({r.get('timestamp', '')}){' [异常]' if r.get('is_abnormal') else ''}"
        for r in device_readings
    ]) or "（无设备数据）"

    service_text = "\n".join([
        f"- [{r.get('service_type', '')}] {r.get('content', '')} ({r.get('timestamp', '')})"
        for r in service_records
    ]) or "（无服务记录）"

    health_text = "\n".join([
        f"- {h.get('indicator_name', '')}: {h.get('value', '')}{h.get('unit', '')} "
        f"(参考: {h.get('reference_range', '')}) [{h.get('status', '')}]"
        for h in health_indicators
    ]) or "（无健康指标）"

    assessment_text = _format_assessment_snapshot(state.get("assessment_snapshot"))

    # 设备谱系与证据完整性：这两段决定"证据能不能支撑结论"
    from backend.core.device_types import evidence_hint
    profile = state.get("device_profile") or {}
    device_profile_text = evidence_hint(profile.get("type_code") or profile.get("device_id"))
    if profile.get("location"):
        device_profile_text += f"\n报警位置：{profile['location']}"

    gaps = state.get("evidence_gaps") or []
    evidence_quality = state.get("evidence_quality") or "unknown"
    evidence_gap_text = "无缺失" if not gaps else "、".join(gaps)

    prompt = RISK_ASSESS_PROMPT.format(
        alert_type=alert_type,
        alert_timestamp=alert_timestamp,
        device_profile=device_profile_text,
        evidence_quality=evidence_quality,
        evidence_gaps=evidence_gap_text,
        device_readings=device_text,
        service_records=service_text,
        health_indicators=health_text,
        assessment_snapshot=assessment_text,
    )

    try:
        structured_llm = get_structured_llm("alert", RiskAssessment)
        result: RiskAssessment = await structured_llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])
        risk_dict = result.model_dump()
        # 措施为空时补兜底：字段有默认值，LLM 漏字段不会抛错，会静默传下去
        raw_actions = risk_dict.get("recommended_actions")
        risk_dict["recommended_actions"] = ensure_actions(
            risk_dict.get("risk_level", "medium"), raw_actions,
        )
        if not raw_actions:
            logger.warning("assess_risk.actions_backfilled",
                           risk_level=risk_dict.get("risk_level"),
                           actions=len(risk_dict["recommended_actions"]))
        logger.info("assess_risk.done", risk_level=risk_dict.get("risk_level"),
                    risk_score=risk_dict.get("risk_score"),
                    actions=len(risk_dict["recommended_actions"]))
        return {"risk_assessment": risk_dict}
    except Exception as e:
        logger.error("assess_risk.failed", error=str(e))
        # 降级：默认中风险
        return {"risk_assessment": {
            "risk_level": "medium", "risk_score": 50,
            "reasoning": "风险评估服务异常，默认中风险，需人工核实",
            "recommended_actions": ["立即派人现场查看", "持续监测生命体征"],
            "confidence": 0.0,
        }}


# ──────────────────────────────────────────────────────────────
# 节点：notify_supervisor — 通知值班主管
# ──────────────────────────────────────────────────────────────

async def notify_supervisor_node(state: AlertState) -> dict:
    """落库告警记录（含风险评估与四轨数据），并通知值班主管审核（模拟通知）。"""
    alert_id = state.get("alert_id", "")
    risk_assessment = state.get("risk_assessment", {})

    # 落库：状态 reviewing + 风险评估 + 四轨数据（供 GET /alert/{id}/review 读取）
    from backend.db.repositories import save_alert_triggered
    profile = state.get("device_profile") or {}
    saved = await save_alert_triggered(
        alert_id=alert_id,
        site_id=state.get("site_id", ""),
        device_id=state.get("device_id", ""),
        alert_type=state.get("alert_type", ""),
        risk_assessment=risk_assessment,
        tenant_id=state.get("tenant_id") or "tenant_default",
        # 来源与 SaaS 报警 ID：SaaS 下发的报警处置完成后要按 external_alert_id 回调
        source=state.get("source") or "local",
        external_alert_id=state.get("external_alert_id") or None,
        location=state.get("alert_location") or profile.get("location", ""),
        device_type=profile.get("type_name", ""),
        type_code=profile.get("type_code", ""),
        # 列名沿用早期的 three_tracks（历史命名，没做 DDL 重命名），里面存的是四轨
        three_tracks={
            "device_readings": state.get("device_readings", []),
            "service_records": state.get("service_records", []),
            "health_indicators": state.get("health_indicators", []),
            "assessment_snapshot": state.get("assessment_snapshot"),
            "device_profile": profile,
            "evidence_gaps": state.get("evidence_gaps", []),
            "evidence_quality": state.get("evidence_quality", ""),
        },
    )
    logger.info("notify_supervisor.done", alert_id=alert_id, persisted=saved)
    return {"supervisor_notified": True}


# ──────────────────────────────────────────────────────────────
# 节点：supervisor_review — Human-in-the-Loop 暂停点
# ──────────────────────────────────────────────────────────────

async def supervisor_review_node(state: AlertState) -> dict:
    """
    Human-in-the-Loop 核心节点。

    interrupt(display_data) 做两件事：
        1. 把 display_data 暴露给外部（值班主管可通过 GET 接口读取）
        2. 冻结图执行，State 自动保存到 checkpointer（默认 AsyncPostgresSaver，落 PostgreSQL）

    图在此暂停，直到值班主管通过 POST 接口传入 Command(resume=decision) 恢复。
    恢复后，interrupt() 的返回值就是 decision（supervisor_decision 字段）。
    """
    risk_assessment = state.get("risk_assessment", {})

    display_data = {
        "alert_id":          state.get("alert_id", ""),
        "site_id":          state.get("site_id", ""),
        "alert_type":        state.get("alert_type", ""),
        "alert_timestamp":   state.get("alert_timestamp", ""),
        "risk_assessment":   risk_assessment,
        "device_readings":   state.get("device_readings", []),
        "service_records":      state.get("service_records", []),
        "health_indicators": state.get("health_indicators", []),
        "message":           "请检查告警风险评估结果，确认后点击派发工单。",
    }

    # interrupt() 在此暂停图执行
    supervisor_decision = interrupt(display_data)

    logger.info(
        "supervisor_review.resumed",
        alert_id=state.get("alert_id", ""),
        action=supervisor_decision.get("action", "unknown"),
    )

    return {"supervisor_decision": supervisor_decision}


# ──────────────────────────────────────────────────────────────
# 节点：apply_supervisor_decision — 应用值班主管决策
# ──────────────────────────────────────────────────────────────

async def apply_supervisor_decision_node(state: AlertState) -> dict:
    """
    将值班主管决策合并到处置措施中。
    approve → 使用 AI 推荐措施
    modify  → 应用值班主管修改的措施
    dismiss → 取消处置（仅记录）
    """
    decision = state.get("supervisor_decision", {})
    action = decision.get("action", "approve")
    risk_assessment = state.get("risk_assessment", {})

    if action == "approve":
        # 直接使用 AI 推荐措施
        final_actions = risk_assessment.get("recommended_actions", [])
        logger.info("apply_supervisor_decision.approve", actions=len(final_actions))
    elif action == "modify":
        # 使用值班主管修改的措施
        final_actions = decision.get("modified_actions", [])
        if not final_actions:
            final_actions = risk_assessment.get("recommended_actions", [])
        logger.info("apply_supervisor_decision.modify", actions=len(final_actions))
    else:  # dismiss
        final_actions = []
        logger.info("apply_supervisor_decision.dismiss")

    return {}  # 最终措施在 dispatch_order_node 中生成工单时使用


# ──────────────────────────────────────────────────────────────
# 节点：dispatch_order — 生成并派发工单
# ──────────────────────────────────────────────────────────────

async def dispatch_order_node(state: AlertState) -> dict:
    """生成并派发工单（模拟发送）。"""
    decision = state.get("supervisor_decision", {})
    action = decision.get("action", "approve")
    risk_assessment = state.get("risk_assessment", {})
    risk_level = risk_assessment.get("risk_level", "medium")
    recommended_actions = risk_assessment.get("recommended_actions", [])

    if action == "modify":
        final_actions = decision.get("modified_actions", recommended_actions)
    elif action == "dismiss":
        final_actions = []
    else:
        final_actions = recommended_actions

    # 第二道防线：即使上游漏了措施（或值班主管 modify 传了空列表），
    # 只要不是"驳回"，派发出去的工单必须有可执行动作
    if action != "dismiss":
        final_actions = ensure_actions(risk_level, final_actions)

    # 用 LLM 生成工单文本
    try:
        prompt = DISPATCH_ORDER_PROMPT.format(
            risk_level=risk_level,
            recommended_actions=json.dumps(final_actions, ensure_ascii=False),
            supervisor_decision=json.dumps(decision, ensure_ascii=False),
        )
        llm = get_llm("alert", temperature=0)
        resp = await llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])
        order_content = _get_message_content(resp)
    except Exception as e:
        logger.error("dispatch_order.llm_failed", error=str(e))
        order_content = f"风险等级：{risk_level}\n处置措施：\n" + "\n".join(f"- {a}" for a in final_actions)

    dispatch_order = {
        "order_id": f"order-{uuid.uuid4().hex[:12]}",
        "alert_id": state.get("alert_id", ""),
        "site_id": state.get("site_id", ""),
        "risk_level": risk_level,
        "actions": final_actions,
        "order_content": order_content,
        "supervisor_id": decision.get("supervisor_id", ""),
        "status": "dispatched" if action != "dismiss" else "dismissed",
    }

    # 落库：更新告警状态 + 写入派发工单（尽力而为，失败不中断）
    from backend.db.repositories import mark_alert_handled, save_alert_dispatched
    saved = await save_alert_dispatched(
        alert_id=state.get("alert_id", ""),
        site_id=state.get("site_id", ""),
        dispatch_order=dispatch_order,
        tenant_id=state.get("tenant_id") or "tenant_default",
    )
    # 记录处置人与处置时间（对齐 SaaS 报警记录的"处理人/处理时间"）
    supervisor_id = decision.get("supervisor_id", "")
    if saved and supervisor_id:
        await mark_alert_handled(
            state.get("alert_id", ""), supervisor_id,
            tenant_id=state.get("tenant_id") or "tenant_default")

    logger.info("dispatch_order.done", alert_id=state.get("alert_id", ""),
                order_id=dispatch_order["order_id"],
                status=dispatch_order["status"], persisted=saved)

    # 下行通知：处置终态落一条出站事件，由后台投递器回调 SaaS
    # （派单 → alert.dispatched；取消处置 → alert.dismissed；同样"尽力而为"）
    from backend.core.outbound import enqueue_alert_decision
    profile = state.get("device_profile") or {}
    await enqueue_alert_decision(
        alert_id=state.get("alert_id", ""),
        site_id=state.get("site_id", ""),
        tenant_id=state.get("tenant_id") or "tenant_default",
        dispatch_order=dispatch_order,
        device_id=state.get("device_id", ""),
        alert_type=state.get("alert_type", ""),
        external_alert_id=state.get("external_alert_id") or "",
        source=state.get("source") or "local",
        handled_by=supervisor_id,
        device_type=profile.get("type_name", ""),
        location=state.get("alert_location") or profile.get("location", ""),
    )

    structured_output = {
        "alert_id": state.get("alert_id", ""),
        "risk_assessment": risk_assessment,
        "dispatch_order": dispatch_order,
        "published": True,
    }

    return {
        "dispatch_order": dispatch_order,
        "published": True,
        "structured_output": structured_output,
    }
