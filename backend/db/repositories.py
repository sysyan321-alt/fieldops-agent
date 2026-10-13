# backend/db/repositories.py
# 数据访问层：Agent 运行结果的持久化与查询（原生 SQL + text()，与项目风格一致）。
#
# 设计原则：
#   1. 「尽力而为」：所有写操作 try/except 包裹，落库失败只记日志、不中断 Agent 流程
#      （评估/照护/告警的主链路价值在推理结果，持久化是附加动作）
#   2. 幂等：写操作用 ON CONFLICT DO UPDATE，重放安全
#   3. 外键兜底：sites/devices 可能没有对应记录（演示/联调时 site_id 随意传），
#      写入前先补占位行，避免 FK 违约导致整条 INSERT 失败

import json
import uuid

from typing import Optional

from sqlalchemy import text

from backend.dependencies import AsyncSessionLocal
from backend.core.logger import get_logger

logger = get_logger(__name__)


# ── 工具函数 ────────────────────────────────────────────────

# 默认租户（与表结构 DEFAULT 一致，历史数据 / 单租户部署都归属它）
DEFAULT_TENANT = "tenant_default"


def _as_dict(value) -> dict | list | None:
    """JSONB 字段统一转 dict/list（兼容 asyncpg 返回 str 或已解析对象两种情况）。"""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


async def _ensure_site(site_id: str, name: str = "未登记老人",
                        tenant_id: str = DEFAULT_TENANT) -> str:
    """老人不存在时补占位行（ON CONFLICT 幂等），避免外键违约。

    返回老人实际归属的租户 ID（新建占位行即归属 tenant_id）。
    """
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            INSERT INTO sites (site_id, name, tenant_id)
            VALUES (:site_id, :name, :tenant_id)
            ON CONFLICT (site_id) DO NOTHING
        """), {"site_id": site_id, "name": name, "tenant_id": tenant_id})
        row = (await session.execute(text("""
            SELECT tenant_id FROM sites WHERE site_id = :site_id
        """), {"site_id": site_id})).first()
        await session.commit()
    return row.tenant_id if row else tenant_id


async def _guard_tenant(site_id: str, tenant_id: str,
                        name: str = "未登记老人") -> bool:
    """租户守卫：确保老人存在且归属当前租户，跨租户写入直接拒绝。"""
    actual = await _ensure_site(site_id, name, tenant_id)
    if actual != tenant_id:
        logger.warning("repo.tenant_violation", site_id=site_id,
                       tenant_id=tenant_id, owner_tenant=actual)
        return False
    return True


async def get_site_tenant(site_id: str) -> Optional[str]:
    """只读查询老人归属租户；不存在返回 None（不会像 _ensure_site 那样补占位行）。

    用于"删除会话状态"这类破坏性操作的租户守卫：先确认归属，再动手。
    """
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text("""
            SELECT tenant_id FROM sites WHERE site_id = :site_id
        """), {"site_id": site_id})).first()
    return row.tenant_id if row else None


async def _ensure_device(device_id: str, site_id: str,
                         tenant_id: str = DEFAULT_TENANT,
                         device_type: str = "",
                         type_code: str = "",
                         location: str = "") -> None:
    """设备不存在时补占位行（ON CONFLICT 幂等）。

    device_type / type_code 传入时一并落库：SaaS 下发的报警只带"设备编号 + 设备类型"，
    第一次收到就该把这台设备登记下来，否则后续四轨采集不知道该按哪类设备读指标。
    """
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            INSERT INTO devices (device_id, site_id, device_type, type_code, location, tenant_id)
            VALUES (:device_id, :site_id, :device_type, :type_code, :location, :tenant_id)
            ON CONFLICT (device_id) DO NOTHING
        """), {"device_id": device_id, "site_id": site_id,
               "device_type": device_type or "未知设备",
               "type_code": type_code or None, "location": location or "",
               "tenant_id": tenant_id})
        await session.commit()


async def ensure_device_types() -> None:
    """幂等播种设备类型字典（不覆盖已有记录，便于 SaaS 侧覆盖本地定义）。"""
    from backend.core.device_types import DEVICE_TYPES

    try:
        async with AsyncSessionLocal() as session:
            for dt in DEVICE_TYPES.values():
                await session.execute(text("""
                    INSERT INTO device_types
                        (type_code, type_name, metrics, alert_types, trust_level, trust_reason)
                    VALUES
                        (:code, :name, CAST(:metrics AS JSONB), CAST(:alerts AS JSONB),
                         :trust, :reason)
                    ON CONFLICT (type_code) DO NOTHING
                """), {
                    "code": dt.code, "name": dt.name,
                    "metrics": json.dumps(list(dt.metrics), ensure_ascii=False),
                    "alerts": json.dumps(list(dt.alert_types), ensure_ascii=False),
                    "trust": dt.trust_level, "reason": dt.trust_reason,
                })
            await session.commit()
        logger.info("repo.device_types_ensured", count=len(DEVICE_TYPES))
    except Exception as e:
        logger.error("repo.device_types_ensure_failed", error=str(e))


async def get_device(device_id: str, tenant_id: str = DEFAULT_TENANT) -> dict | None:
    """按设备 ID 查设备（强制租户过滤），用于取设备类型与安装位置。"""
    try:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(text("""
                SELECT device_id, site_id, device_type, type_code, location, status
                FROM devices WHERE device_id = :device_id AND tenant_id = :tenant_id
            """), {"device_id": device_id, "tenant_id": tenant_id})).first()
        if row is None:
            return None
        return {"device_id": row.device_id, "site_id": row.site_id,
                "device_type": row.device_type, "type_code": row.type_code or "",
                "location": row.location or "", "status": row.status}
    except Exception as e:
        logger.error("repo.device_get_failed", device_id=device_id, error=str(e))
        return None


# ── 评估结果：assessment_results 表 ─────────────────────────

async def save_assessment_result(
    assessment_id: str,
    site_id: str,
    dimension_scores: list,
    weighted_score: float,
    summary_payload: dict,
    tenant_id: str = DEFAULT_TENANT,
) -> bool:
    """保存评估 Agent 完整结果。summary_payload 含 structured/issues/summary 全量数据。"""
    try:
        if not await _guard_tenant(site_id, tenant_id):
            return False
        async with AsyncSessionLocal() as session:
            await session.execute(text("""
                INSERT INTO assessment_results
                    (assessment_id, site_id, tenant_id, dimension_scores, weighted_score, summary)
                VALUES
                    (:assessment_id, :site_id, :tenant_id, CAST(:dimension_scores AS JSONB),
                     :weighted_score, CAST(:summary AS JSONB))
                ON CONFLICT (assessment_id) DO UPDATE SET
                    dimension_scores = EXCLUDED.dimension_scores,
                    weighted_score   = EXCLUDED.weighted_score,
                    summary          = EXCLUDED.summary
            """), {
                "assessment_id": assessment_id,
                "site_id": site_id,
                "tenant_id": tenant_id,
                "dimension_scores": json.dumps(dimension_scores, ensure_ascii=False),
                "weighted_score": round(float(weighted_score), 2),
                "summary": json.dumps(summary_payload, ensure_ascii=False),
            })
            await session.commit()
        logger.info("repo.assessment_saved", assessment_id=assessment_id,
                    site_id=site_id, weighted_score=round(weighted_score, 2))
        return True
    except Exception as e:
        logger.error("repo.assessment_save_failed", assessment_id=assessment_id, error=str(e))
        return False


async def get_latest_assessment(site_id: str, tenant_id: str = DEFAULT_TENANT) -> dict | None:
    """取老人最近一次能力评估（告警研判的第四轨）。

    用途：设备告警只是"当下异常"，评估结果才是"这个人本来有多脆弱"——
    跌倒高风险、ADL 低分、认知受损的老人，同样的心率异常要按更高风险处理。
    没有评估记录时返回 None（新入住、尚未评估），调用方按"无第四轨"继续。
    """
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text("""
            SELECT assessment_id, site_id, dimension_scores, weighted_score,
                   summary, created_at
            FROM assessment_results
            WHERE site_id = :site_id AND tenant_id = :tenant_id
            ORDER BY created_at DESC
            LIMIT 1
        """), {"site_id": site_id, "tenant_id": tenant_id})).first()

    if not row:
        return None
    return {
        "assessment_id": row.assessment_id,
        "site_id": row.site_id,
        "dimension_scores": _as_dict(row.dimension_scores) or [],
        "weighted_score": float(row.weighted_score) if row.weighted_score is not None else None,
        "summary": _as_dict(row.summary) or {},
        "created_at": str(row.created_at) if row.created_at else "",
    }


async def get_assessment_result(assessment_id: str,
                                tenant_id: str = DEFAULT_TENANT) -> dict | None:
    """按评估 ID 查询评估结果（强制租户过滤，跨租户视为不存在）。"""
    try:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(text("""
                SELECT assessment_id, site_id, dimension_scores, weighted_score, summary, created_at
                FROM assessment_results
                WHERE assessment_id = :assessment_id AND tenant_id = :tenant_id
            """), {"assessment_id": assessment_id, "tenant_id": tenant_id})).first()
        if row is None:
            return None
        return {
            "assessment_id": row.assessment_id,
            "site_id": row.site_id,
            "dimension_scores": _as_dict(row.dimension_scores) or [],
            "weighted_score": float(row.weighted_score) if row.weighted_score is not None else 0,
            "summary": _as_dict(row.summary) or {},
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
    except Exception as e:
        logger.error("repo.assessment_get_failed", assessment_id=assessment_id, error=str(e))
        return None


# ── 照护计划：service_plans 表 ────────────────────────────────

async def save_service_plan(
    plan_id: str,
    site_id: str,
    plan_content: dict,
    tenant_id: str = DEFAULT_TENANT,
) -> bool:
    """保存评估派生的照护计划。

    plan_id 约定为 plan-{assessment_id}：每次评估生成一个新版本计划，
    天然幂等（同一评估重跑走 ON CONFLICT 更新内容，不新增行）。
    """
    try:
        if not plan_id:
            logger.error("repo.service_plan_save_skipped", reason="empty_plan_id")
            return False
        if not await _guard_tenant(site_id, tenant_id):
            return False
        async with AsyncSessionLocal() as session:
            await session.execute(text("""
                INSERT INTO service_plans
                    (plan_id, site_id, plan_content, tenant_id)
                VALUES
                    (:plan_id, :site_id, CAST(:plan_content AS JSONB), :tenant_id)
                ON CONFLICT (plan_id) DO UPDATE SET
                    plan_content = EXCLUDED.plan_content
            """), {
                "plan_id": plan_id,
                "site_id": site_id,
                "plan_content": json.dumps(plan_content, ensure_ascii=False),
                "tenant_id": tenant_id,
            })
            await session.commit()
        logger.info("repo.service_plan_saved", plan_id=plan_id, site_id=site_id,
                    risk_tier=plan_content.get("risk_tier"),
                    daily_tasks_count=len(plan_content.get("daily_tasks", [])))
        return True
    except Exception as e:
        logger.error("repo.service_plan_save_failed", plan_id=plan_id, error=str(e))
        return False


async def get_latest_service_plan(
    site_id: str, tenant_id: str = DEFAULT_TENANT,
) -> dict | None:
    """取老人最近一次评估生成的照护计划（强制租户过滤）。

    service_plans 表只有 created_at，按 created_at DESC 取最新版本；
    无记录或查询失败时返回 None——调用方（service_plan_provider）会继续查业务配置的
    照护服务项，两者都没有才报"计划缺失"，不会编一份假计划。
    """
    try:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(text("""
                SELECT plan_id, site_id, plan_content, created_at
                FROM service_plans
                WHERE site_id = :site_id AND tenant_id = :tenant_id
                ORDER BY created_at DESC
                LIMIT 1
            """), {"site_id": site_id, "tenant_id": tenant_id})).first()
        if row is None:
            return None
        return {
            "plan_id": row.plan_id,
            "site_id": row.site_id,
            "plan_content": _as_dict(row.plan_content) or {},
            "created_at": str(row.created_at) if row.created_at else "",
        }
    except Exception as e:
        logger.error("repo.service_plan_get_failed", site_id=site_id, error=str(e))
        return None


# ── 照护服务项与每日照护计划（对齐机构 SaaS 的业务模型）──────
# site_service_items = 业务配置的"该老人要做什么服务"（care 只读，由业务侧维护）
# daily_work_plans    = 每日执行计划（care 只读，生成规则在业务侧）

async def save_service_item(
    site_id: str,
    project_name: str,
    tenant_id: str = DEFAULT_TENANT,
    project_id: str = "",
    frequency_id: str = "",
    frequency_name: str = "",
    times_per_day: int = 0,
    nurse_time_slots: list | None = None,
    price: float = 0,
    is_required: bool = True,
    risk_tier: str = "",
    source: str = "manual",
    item_id: str = "",
) -> str | None:
    """写入/更新一条老人照护服务项，返回 item_id。

    project_name 在同一老人下唯一（业务上"同一个服务项目"只登记一条），
    重复导入走更新而不是插新行。
    """
    try:
        if not await _guard_tenant(site_id, tenant_id):
            return None
        row_id = item_id or f"esi-{uuid.uuid4().hex[:12]}"
        async with AsyncSessionLocal() as session:
            await session.execute(text("""
                INSERT INTO site_service_items
                    (item_id, site_id, project_id, project_name, frequency_id, frequency_name,
                     times_per_day, nurse_time_slots, price, is_required, risk_tier, source, tenant_id)
                VALUES
                    (:item_id, :site_id, :project_id, :project_name, :frequency_id, :frequency_name,
                     :times_per_day, CAST(:slots AS JSONB), :price, :is_required, :risk_tier,
                     :source, :tenant_id)
                ON CONFLICT (item_id) DO UPDATE SET
                    project_name   = EXCLUDED.project_name,
                    frequency_name = EXCLUDED.frequency_name,
                    times_per_day  = EXCLUDED.times_per_day,
                    nurse_time_slots = EXCLUDED.nurse_time_slots,
                    price          = EXCLUDED.price,
                    is_required    = EXCLUDED.is_required,
                    risk_tier     = EXCLUDED.risk_tier,
                    active         = TRUE
            """), {
                "item_id": row_id, "site_id": site_id, "project_id": project_id,
                "project_name": project_name, "frequency_id": frequency_id,
                "frequency_name": frequency_name, "times_per_day": int(times_per_day or 0),
                "slots": json.dumps(nurse_time_slots or [], ensure_ascii=False),
                "price": price or 0, "is_required": bool(is_required),
                "risk_tier": risk_tier, "source": source, "tenant_id": tenant_id,
            })
            await session.commit()
        return row_id
    except Exception as e:
        logger.error("repo.service_item_save_failed", site_id=site_id, error=str(e))
        return None


async def list_service_items(site_id: str, tenant_id: str = DEFAULT_TENANT) -> list[dict]:
    """取老人当前生效的照护服务项（强制租户过滤，必选在前）。"""
    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(text("""
                SELECT item_id, site_id, project_id, project_name, frequency_id, frequency_name,
                       times_per_day, nurse_time_slots, price, is_required, risk_tier, source
                FROM site_service_items
                WHERE site_id = :site_id AND tenant_id = :tenant_id AND active = TRUE
                ORDER BY is_required DESC, created_at ASC
            """), {"site_id": site_id, "tenant_id": tenant_id})).fetchall()
        return [{
            "item_id": r.item_id, "site_id": r.site_id,
            "project_id": r.project_id or "", "project_name": r.project_name,
            "frequency_id": r.frequency_id or "", "frequency_name": r.frequency_name or "",
            "times_per_day": r.times_per_day or 0,
            "nurse_time_slots": _as_dict(r.nurse_time_slots) or [],
            "price": float(r.price or 0), "is_required": bool(r.is_required),
            "risk_tier": r.risk_tier or "", "source": r.source or "template",
        } for r in rows]
    except Exception as e:
        logger.error("repo.service_items_list_failed", site_id=site_id, error=str(e))
        return []


# 护理等级 → 照护项目模板（本地 Provider 的演示配置）
# 生产形态下这份配置在机构业务系统（按护理等级组织），
# 本地配置只为让 demo 能独立跑通"评估等级 → 该等级服务项"这条链路。
DEFAULT_LEVEL_CONFIGS: list[dict] = [
    # 自理：巡视 + 健康监测为主
    {"risk_tier": "自理", "project_name": "每日巡视", "frequency_name": "2次/日",
     "times_per_day": 2, "nurse_time_slots": [{"startTime": "09:00", "endTime": "11:00"},
                                              {"startTime": "15:00", "endTime": "17:00"}],
     "is_required": True, "sort_order": 1},
    {"risk_tier": "自理", "project_name": "健康监测", "frequency_name": "1次/日",
     "times_per_day": 1, "nurse_time_slots": [{"startTime": "08:00", "endTime": "09:00"}],
     "is_required": True, "sort_order": 2},
    {"risk_tier": "自理", "project_name": "康复训练", "frequency_name": "1次/日",
     "times_per_day": 1, "nurse_time_slots": [{"startTime": "14:00", "endTime": "15:00"}],
     "is_required": False, "sort_order": 3},
    # 介助：生活照护为主
    {"risk_tier": "介助", "project_name": "协助进食", "frequency_name": "3次/日",
     "times_per_day": 3, "nurse_time_slots": [{"startTime": "07:00", "endTime": "08:00"},
                                              {"startTime": "11:30", "endTime": "12:30"},
                                              {"startTime": "17:30", "endTime": "18:30"}],
     "is_required": True, "sort_order": 1},
    {"risk_tier": "介助", "project_name": "协助翻身", "frequency_name": "每2小时",
     "times_per_day": 6, "nurse_time_slots": [{"startTime": "08:00", "endTime": "20:00"}],
     "is_required": True, "sort_order": 2},
    {"risk_tier": "介助", "project_name": "用药协助", "frequency_name": "3次/日",
     "times_per_day": 3, "nurse_time_slots": [{"startTime": "07:00", "endTime": "19:00"}],
     "is_required": True, "sort_order": 3},
    {"risk_tier": "介助", "project_name": "口腔护理", "frequency_name": "2次/日",
     "times_per_day": 2, "nurse_time_slots": [{"startTime": "07:00", "endTime": "09:00"}],
     "is_required": True, "sort_order": 4},
    {"risk_tier": "介助", "project_name": "每日巡视", "frequency_name": "2次/日",
     "times_per_day": 2, "nurse_time_slots": [{"startTime": "09:00", "endTime": "17:00"}],
     "is_required": True, "sort_order": 5},
    {"risk_tier": "介助", "project_name": "康复训练", "frequency_name": "隔日1次",
     "times_per_day": 1, "nurse_time_slots": [{"startTime": "14:00", "endTime": "15:00"}],
     "is_required": False, "sort_order": 6},
    # 介护：全天候照护为主
    {"risk_tier": "介护", "project_name": "鼻饲护理", "frequency_name": "4次/日",
     "times_per_day": 4, "nurse_time_slots": [{"startTime": "07:00", "endTime": "21:00"}],
     "is_required": True, "sort_order": 1},
    {"risk_tier": "介护", "project_name": "协助翻身", "frequency_name": "每1小时",
     "times_per_day": 12, "nurse_time_slots": [{"startTime": "06:00", "endTime": "22:00"}],
     "is_required": True, "sort_order": 2},
    {"risk_tier": "介护", "project_name": "皮肤护理", "frequency_name": "3次/日",
     "times_per_day": 3, "nurse_time_slots": [{"startTime": "08:00", "endTime": "20:00"}],
     "is_required": True, "sort_order": 3},
    {"risk_tier": "介护", "project_name": "口腔护理", "frequency_name": "2次/日",
     "times_per_day": 2, "nurse_time_slots": [{"startTime": "07:00", "endTime": "09:00"}],
     "is_required": True, "sort_order": 4},
    {"risk_tier": "介护", "project_name": "全天巡视", "frequency_name": "6次/日",
     "times_per_day": 6, "nurse_time_slots": [{"startTime": "00:00", "endTime": "24:00"}],
     "is_required": True, "sort_order": 5},
]


async def ensure_service_tier_configs() -> None:
    """幂等播种护理等级照护模板（不覆盖已有记录，便于机构侧覆盖本地定义）。"""
    try:
        async with AsyncSessionLocal() as session:
            for i, cfg in enumerate(DEFAULT_LEVEL_CONFIGS):
                await session.execute(text("""
                    INSERT INTO service_tier_configs
                        (config_id, risk_tier, project_name, frequency_name, times_per_day,
                         nurse_time_slots, is_required, sort_order)
                    VALUES
                        (:config_id, :risk_tier, :project_name, :frequency_name, :times_per_day,
                         CAST(:slots AS JSONB), :is_required, :sort_order)
                    ON CONFLICT (config_id) DO NOTHING
                """), {
                    "config_id": f"clc-{cfg['risk_tier']}-{i + 1:02d}",
                    "risk_tier": cfg["risk_tier"], "project_name": cfg["project_name"],
                    "frequency_name": cfg.get("frequency_name", ""),
                    "times_per_day": cfg.get("times_per_day", 0),
                    "slots": json.dumps(cfg.get("nurse_time_slots") or [], ensure_ascii=False),
                    "is_required": cfg.get("is_required", True),
                    "sort_order": cfg.get("sort_order", 0),
                })
            await session.commit()
        logger.info("repo.service_tier_configs_ensured", count=len(DEFAULT_LEVEL_CONFIGS))
    except Exception as e:
        logger.error("repo.service_tier_configs_ensure_failed", error=str(e))


async def list_level_configs(risk_tier: str, tenant_id: str = DEFAULT_TENANT,
                             required_only: bool = False) -> list[dict]:
    """取某护理等级可用的照护项目（由业务系统提供「该等级允许做什么」）。"""
    if not risk_tier:
        return []
    try:
        sql = ("SELECT config_id, risk_tier, project_id, project_name, frequency_id,"
               " frequency_name, times_per_day, nurse_time_slots, price, is_required"
               " FROM service_tier_configs WHERE risk_tier = :level AND active = TRUE")
        params = {"level": risk_tier}
        if required_only:
            sql += " AND is_required = TRUE"
        sql += " ORDER BY sort_order ASC"
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(text(sql), params)).fetchall()
        return [{
            "project_id": r.project_id or "", "project_name": r.project_name,
            "frequency_id": r.frequency_id or "", "frequency_name": r.frequency_name or "",
            "times_per_day": r.times_per_day or 0,
            "nurse_time_slots": _as_dict(r.nurse_time_slots) or [],
            "price": float(r.price or 0), "is_required": bool(r.is_required),
            "risk_tier": r.risk_tier, "source": "template",
        } for r in rows]
    except Exception as e:
        logger.error("repo.level_configs_list_failed", risk_tier=risk_tier, error=str(e))
        return []


async def bind_service_items_for_level(site_id: str, risk_tier: str,
                                       tenant_id: str = DEFAULT_TENANT) -> list[dict]:
    """把该护理等级的**必选**照护项目落到老人名下，返回老人当前生效的服务项。

    这是"评估结论驱动业务配置"的那一步：评估给出护理等级 → 该等级的必选项目
    成为老人的照护服务项 → 照护流程的任务清单由它生成。已经手工登记过的服务项
    不会被覆盖（同名视为同一项）。
    """
    if not risk_tier:
        return await list_service_items(site_id, tenant_id=tenant_id)

    configs = await list_level_configs(risk_tier, tenant_id=tenant_id, required_only=True)
    if not configs:
        logger.warning("repo.level_configs_empty", risk_tier=risk_tier)
        return await list_service_items(site_id, tenant_id=tenant_id)

    existing = {it.get("project_name") for it in
                await list_service_items(site_id, tenant_id=tenant_id)}
    for cfg in configs:
        if cfg["project_name"] in existing:
            continue
        await save_service_item(
            site_id=site_id, project_name=cfg["project_name"], tenant_id=tenant_id,
            project_id=cfg.get("project_id", ""), frequency_id=cfg.get("frequency_id", ""),
            frequency_name=cfg.get("frequency_name", ""),
            times_per_day=cfg.get("times_per_day", 0),
            nurse_time_slots=cfg.get("nurse_time_slots") or [],
            price=cfg.get("price", 0), is_required=cfg.get("is_required", True),
            risk_tier=risk_tier, source="template",
        )
    items = await list_service_items(site_id, tenant_id=tenant_id)
    logger.info("repo.service_items_bound", site_id=site_id, risk_tier=risk_tier,
                bound=len(configs), total=len(items))
    return items


# ── 每日照护计划：生成（本地替身）与执行回写 ──────────────────
# 边界提醒：真实的生成规则属于业务侧（每日计划由业务系统生成）。
# 下面这个展开函数只为让本地 Provider 在演示/离线时能给出当天的可执行清单，
# 生产形态走 SaaSServicePlanProvider 直接读主系统的当天计划。

def _split_window(slot: dict, times: int) -> list[str] | None:
    """把一个护理时间窗均分成 times 段，用于"每2小时翻身×6次"这类配置。

    不这么做的话，6 次翻身会得到 6 行一模一样的"08:00-20:00"，
    护理员看不出这是今天第几次、几点该做。均分后是 08:00-10:00、10:00-12:00…

    解析失败（如跨天写法 "24:00" 或格式不规范）就返回 None，
    由调用方回退成"同一个窗口重复标注"，而不是抛异常中断。
    """
    if times < 2:
        return None
    start, end = str(slot.get("startTime", "")), str(slot.get("endTime", ""))
    # "24:00" 不是 %H:%M 的合法输入，先归一成当天最后一分钟
    end = "23:59" if end.strip() == "24:00" else end
    try:
        from datetime import datetime as _dt, timedelta as _td

        s = _dt.strptime(start, "%H:%M")
        e = _dt.strptime(end, "%H:%M")
        total_minutes = (e - s).total_seconds() / 60
        if total_minutes <= 0:
            return None
        step = total_minutes / times
        return [f"{(s + _td(minutes=step * i)):%H:%M}-{(s + _td(minutes=step * (i + 1))):%H:%M}"
                for i in range(times)]
    except Exception:  # noqa: BLE001
        return None


def _expand_items_to_day(items: list[dict], plan_date) -> list[dict]:
    """把服务项按频次展开成当天的计划条目（确定性 ID，重复展开幂等）。

    频次/护理时间段来自业务配置：一天 3 次的"协助进食"就展开成 3 条，
    时间窗标注规则：配了多个窗口就逐个用；只配了一个窗口而频次大于 1，
    就把该窗口均分成频次段（见 _split_window）。
    """
    entries: list[dict] = []
    seq = 0
    for it in items:
        times = int(it.get("times_per_day") or 1)
        slots = [s for s in (it.get("nurse_time_slots") or []) if isinstance(s, dict)]
        # 单窗口多频次 → 均分；分段失败则退化为重复同一窗口
        split = _split_window(slots[0], max(times, 1)) if len(slots) == 1 else None
        for i in range(max(times, 1)):
            seq += 1
            window = ""
            if split:
                window = split[i]
            elif slots:
                s = slots[i % len(slots)]
                start, end = s.get("startTime", ""), s.get("endTime", "")
                if start or end:
                    window = f"{start}-{end}"
            entries.append({
                "plan_item_id": f"dcp-{plan_date}-{seq:03d}",
                "project_name": it.get("project_name", ""),
                "nurse_time": window,
                "seq": seq,
            })
    return entries


async def ensure_daily_plan(site_id: str, plan_date=None,
                            tenant_id: str = DEFAULT_TENANT) -> list[dict]:
    """按老人的照护服务项展开当天计划并落库（幂等），返回当天计划。

    重复调用不会产生重复条目（plan_item_id 是 (日期, 序号) 的确定性拼装）；
    已经确认完成（done）的条目不会被重展开覆盖。
    """
    from datetime import date as _date

    plan_date = plan_date or _date.today()
    items = await list_service_items(site_id, tenant_id=tenant_id)
    if not items:
        return []
    entries = _expand_items_to_day(items, plan_date)
    try:
        async with AsyncSessionLocal() as session:
            for e in entries:
                await session.execute(text("""
                    INSERT INTO daily_work_plans
                        (plan_item_id, site_id, plan_date, project_name, nurse_time, status, tenant_id)
                    VALUES
                        (:pid, :eid, :pdate, :pname, :ntime, 'planned', :tid)
                    ON CONFLICT (plan_item_id) DO NOTHING
                """), {"pid": e["plan_item_id"], "eid": site_id, "pdate": plan_date,
                       "pname": e["project_name"], "ntime": e["nurse_time"], "tid": tenant_id})
            await session.commit()
    except Exception as ex:
        logger.error("repo.daily_plan_ensure_failed", site_id=site_id, error=str(ex))
    return await list_daily_plan(site_id, plan_date=plan_date, tenant_id=tenant_id)


async def list_daily_plan(site_id: str, plan_date=None,
                          tenant_id: str = DEFAULT_TENANT) -> list[dict]:
    """读某天的照护计划（默认今天），带执行状态。"""
    from datetime import date as _date

    plan_date = plan_date or _date.today()
    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(text("""
                SELECT plan_item_id, project_name, nurse_time, worker_id, worker_name,
                       actual_care_time, status
                FROM daily_work_plans
                WHERE site_id = :eid AND tenant_id = :tid AND plan_date = :pdate
                ORDER BY plan_item_id ASC
            """), {"eid": site_id, "tid": tenant_id, "pdate": plan_date})).fetchall()
        return [{
            "plan_item_id": r.plan_item_id, "project_name": r.project_name,
            "nurse_time": r.nurse_time or "", "worker_id": r.worker_id or "",
            "worker_name": r.worker_name or "", "actual_care_time": r.actual_care_time or "",
            "status": r.status, "plan_date": plan_date.isoformat(),
        } for r in rows]
    except Exception as e:
        logger.error("repo.daily_plan_list_failed", site_id=site_id, error=str(e))
        return []


async def confirm_daily_plan_item(plan_item_id: str, tenant_id: str = DEFAULT_TENANT,
                                  status: str = "done", worker_id: str = "",
                                  worker_name: str = "",
                                  actual_care_time: str = "") -> bool:
    """护工执行后回写：标记该项完成/未完成 + 执行人与实际时间。

    对应业务系统侧的执行结果回写接口。状态语义：
      done    —— 已按计划完成
      missed  —— 该做没做（漏做，会进入照护历史的异常记录）
    """
    if status not in ("done", "missed", "planned"):
        logger.warning("repo.daily_plan_confirm_bad_status", status=status)
        return False
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("""
                UPDATE daily_work_plans
                SET status = :status, worker_id = :cid, worker_name = :cname,
                    actual_care_time = :atime, updated_at = CURRENT_TIMESTAMP
                WHERE plan_item_id = :pid AND tenant_id = :tid
            """), {"status": status, "cid": worker_id, "cname": worker_name,
                   "atime": actual_care_time, "pid": plan_item_id, "tid": tenant_id})
            if result.rowcount == 0:
                await session.rollback()
                return False
            await session.commit()
        logger.info("repo.daily_plan_confirmed", plan_item_id=plan_item_id, status=status)
        return True
    except Exception as e:
        logger.error("repo.daily_plan_confirm_failed", plan_item_id=plan_item_id, error=str(e))
        return False


def daily_plan_progress(plan: list[dict]) -> dict:
    """当天计划执行进度（给 prompt 和前端用）。"""
    total = len(plan)
    done = sum(1 for p in plan if p.get("status") == "done")
    missed = sum(1 for p in plan if p.get("status") == "missed")
    return {"total": total, "done": done, "missed": missed, "pending": total - done - missed}


async def list_service_history(site_id: str, tenant_id: str = DEFAULT_TENANT,
                            limit: int = 8) -> list[dict]:
    """取真实照护历史（替代原先写死的两条 mock 记录）。

    两个来源合并后按日期倒序：
      ① daily_work_plans 每日计划：给出"计划做了什么 / 完成没完成"；
      ② service_records 照护记录：护工实际登记的执行内容。
    两边都没有就返回空列表——Agent 应当知道"没有历史记录"，而不是拿到两条假记录。
    """
    history: dict[str, dict] = {}
    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(text("""
                SELECT plan_date, project_name, status, worker_name, actual_care_time
                FROM daily_work_plans
                WHERE site_id = :site_id AND tenant_id = :tenant_id
                ORDER BY plan_date DESC LIMIT :lim
            """), {"site_id": site_id, "tenant_id": tenant_id, "lim": limit})).fetchall()
        for r in rows:
            day = r.plan_date.isoformat() if r.plan_date else ""
            entry = history.setdefault(day, {"date": day, "tasks": [], "abnormal": ""})
            label = r.project_name + ("（已完成）" if r.status == "done" else "（未完成）")
            if r.worker_name:
                label += f" 执行：{r.worker_name}"
            if r.actual_care_time:
                label += f" 实际：{r.actual_care_time}"
            entry["tasks"].append(label)
            if r.status in ("missed", "未完成"):
                entry["abnormal"] = (entry["abnormal"] + "；" if entry["abnormal"] else "") + \
                                    f"{r.project_name}未按时完成"
    except Exception as e:
        logger.error("repo.service_history_plan_failed", site_id=site_id, error=str(e))

    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(text("""
                SELECT record_id, service_type, content, worker_id, created_at
                FROM service_records
                WHERE site_id = :site_id AND tenant_id = :tenant_id
                ORDER BY created_at DESC LIMIT :lim
            """), {"site_id": site_id, "tenant_id": tenant_id, "lim": limit})).fetchall()
        for r in rows:
            day = r.created_at.strftime("%Y-%m-%d") if r.created_at else ""
            entry = history.setdefault(day, {"date": day, "tasks": [], "abnormal": ""})
            entry["tasks"].append(f"{r.service_type}：{r.content or ''}".strip("："))
    except Exception as e:
        logger.error("repo.service_history_records_failed", site_id=site_id, error=str(e))

    ordered = sorted(history.values(), key=lambda x: x["date"], reverse=True)
    return ordered[:limit]


# ── 照护流程报告：work_flow_reports 表 ──────────────────────

async def save_work_flow_report(
    session_id: str,
    site_id: str,
    service_plan_id: str,
    stage_reached: str,
    total_turns: int,
    report: dict,
    tenant_id: str = DEFAULT_TENANT,
) -> bool:
    """保存照护流程评估报告（session_id 作为唯一键，重放覆盖）。"""
    try:
        if not await _guard_tenant(site_id, tenant_id):
            return False
        async with AsyncSessionLocal() as session:
            await session.execute(text("""
                INSERT INTO work_flow_reports
                    (report_id, session_id, site_id, service_plan_id, stage_reached,
                     total_turns, report, tenant_id)
                VALUES
                    (:report_id, :session_id, :site_id, :service_plan_id, :stage_reached,
                     :total_turns, CAST(:report AS JSONB), :tenant_id)
                ON CONFLICT (session_id) DO UPDATE SET
                    stage_reached = EXCLUDED.stage_reached,
                    total_turns   = EXCLUDED.total_turns,
                    report        = EXCLUDED.report
            """), {
                "report_id": f"rpt-{uuid.uuid4().hex[:12]}",
                "session_id": session_id,
                "site_id": site_id,
                "service_plan_id": service_plan_id or "",
                "stage_reached": stage_reached or "",
                "total_turns": int(total_turns or 0),
                "report": json.dumps(report, ensure_ascii=False),
                "tenant_id": tenant_id,
            })
            await session.commit()
        logger.info("repo.work_report_saved", session_id=session_id, site_id=site_id)
        return True
    except Exception as e:
        logger.error("repo.work_report_save_failed", session_id=session_id, error=str(e))
        return False


async def get_work_flow_report(session_id: str,
                               tenant_id: str = DEFAULT_TENANT) -> dict | None:
    """按会话 ID 查询照护报告（强制租户过滤，跨租户视为不存在）。"""
    try:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(text("""
                SELECT session_id, site_id, service_plan_id, stage_reached, total_turns, report, created_at
                FROM work_flow_reports
                WHERE session_id = :session_id AND tenant_id = :tenant_id
            """), {"session_id": session_id, "tenant_id": tenant_id})).first()
        if row is None:
            return None
        return {
            "session_id": row.session_id,
            "site_id": row.site_id,
            "service_plan_id": row.service_plan_id,
            "stage_reached": row.stage_reached,
            "total_turns": row.total_turns,
            "report": _as_dict(row.report) or {},
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
    except Exception as e:
        logger.error("repo.work_report_get_failed", session_id=session_id, error=str(e))
        return None


# ── 照护流程会话登记：work_flow_sessions 表 ─────────────────
# 业务侧权威记录：/start 登记、每轮同步进度、finished 置完成。
# 前端按 site_id 查进行中会话拿回 session_id，不再依赖浏览器保存 URL。

async def create_work_flow_session(
    session_id: str,
    site_id: str,
    staff_id: str,
    current_stage: str,
    tenant_id: str = DEFAULT_TENANT,
) -> bool:
    """照护流程 /start 时登记会话（同 session_id 重复发起幂等跳过）。"""
    try:
        if not await _guard_tenant(site_id, tenant_id):
            return False
        async with AsyncSessionLocal() as session:
            await session.execute(text("""
                INSERT INTO work_flow_sessions
                    (session_id, site_id, staff_id, current_stage, total_turns, status, tenant_id)
                VALUES
                    (:session_id, :site_id, :staff_id, :current_stage, 0, 'in_progress', :tenant_id)
                ON CONFLICT (session_id) DO NOTHING
            """), {
                "session_id": session_id,
                "site_id": site_id,
                "staff_id": staff_id,
                "current_stage": current_stage or "admission_assessment",
                "tenant_id": tenant_id,
            })
            await session.commit()
        logger.info("repo.work_flow_session_created",
                    session_id=session_id, site_id=site_id, stage=current_stage)
        return True
    except Exception as e:
        logger.error("repo.work_flow_session_create_failed",
                     session_id=session_id, error=str(e))
        return False


async def update_work_flow_session_progress(
    session_id: str,
    current_stage: str,
    total_turns: int,
    tenant_id: str = DEFAULT_TENANT,
) -> bool:
    """每轮对话后同步阶段进度；走到 finished 自动把会话置为完成。失败不阻断对话。"""
    try:
        status = "finished" if (current_stage or "") == "finished" else "in_progress"
        async with AsyncSessionLocal() as session:
            await session.execute(text("""
                UPDATE work_flow_sessions
                SET current_stage = :current_stage,
                    total_turns   = :total_turns,
                    status        = :status,
                    updated_at    = CURRENT_TIMESTAMP
                WHERE session_id = :session_id AND tenant_id = :tenant_id
            """), {
                "session_id": session_id,
                "current_stage": current_stage or "",
                "total_turns": int(total_turns or 0),
                "status": status,
                "tenant_id": tenant_id,
            })
            await session.commit()
        return True
    except Exception as e:
        logger.error("repo.work_flow_session_update_failed",
                     session_id=session_id, error=str(e))
        return False


async def get_work_flow_session(session_id: str,
                                tenant_id: str = DEFAULT_TENANT) -> dict | None:
    """按 session_id 查会话登记行。

    用途：/chat 请求不带 site_id，从这里找回，保证 thread_id 与 /start
    拼接一致（否则检查点键对不上，流程续不上）。跨租户视为不存在。
    """
    try:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(text("""
                SELECT session_id, site_id, staff_id, current_stage,
                       total_turns, status
                FROM work_flow_sessions
                WHERE session_id = :session_id AND tenant_id = :tenant_id
            """), {"session_id": session_id, "tenant_id": tenant_id})).first()
        if row is None:
            return None
        return {
            "session_id": row.session_id,
            "site_id": row.site_id,
            "staff_id": row.staff_id,
            "current_stage": row.current_stage,
            "total_turns": row.total_turns,
            "status": row.status,
        }
    except Exception as e:
        logger.error("repo.work_flow_session_get_failed",
                     session_id=session_id, error=str(e))
        return None


async def get_active_work_flow_session(site_id: str,
                                       tenant_id: str = DEFAULT_TENANT) -> dict | None:
    """按老人查最近一场进行中的照护流程会话；没有返回 None。"""
    try:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(text("""
                SELECT session_id, site_id, staff_id, current_stage,
                       total_turns, started_at, updated_at
                FROM work_flow_sessions
                WHERE site_id = :site_id
                  AND tenant_id = :tenant_id
                  AND status = 'in_progress'
                ORDER BY started_at DESC
                LIMIT 1
            """), {"site_id": site_id, "tenant_id": tenant_id})).first()
        if row is None:
            return None
        return {
            "session_id": row.session_id,
            "site_id": row.site_id,
            "staff_id": row.staff_id,
            "current_stage": row.current_stage,
            "total_turns": row.total_turns,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
    except Exception as e:
        logger.error("repo.work_flow_session_get_active_failed",
                     site_id=site_id, error=str(e))
        return None


# ── 出站通知：tenant_webhooks 表 + outbound_events 表 ───────
# 上行（SaaS → Agent）是"请求体/回调注入业务信号"，下行（Agent → SaaS）走这里：
#   业务事件先写发件箱（outbound_events），再由后台投递器异步 POST 到租户回调地址。
# 为何不直接在流程里 await httpx.post：
#   ① 外部系统抖动不该拖慢或搞挂照护流程；② 进程崩溃会丢事件；
#   ③ 需要重试/幂等/审计。发件箱模式把"产生事件"与"投递事件"解耦。

async def upsert_tenant_webhook(tenant_id: str, url: str, secret: str | None = None,
                                enabled: bool = True) -> bool:
    """登记/更新租户回调地址（一租户一条，覆盖写）。

    secret 语义：None = 不改动现有密钥（前端不回显密钥，避免保存时误清空）；
    空串 = 显式清空（改用无签名模式）；非空 = 覆盖。
    """
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("""
                INSERT INTO tenant_webhooks (tenant_id, url, secret, enabled, updated_at)
                VALUES
                    (:tenant_id, :url, COALESCE(CAST(:secret AS VARCHAR), ''), :enabled,
                     CURRENT_TIMESTAMP)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    url        = EXCLUDED.url,
                    secret     = COALESCE(CAST(:secret AS VARCHAR), tenant_webhooks.secret),
                    enabled    = EXCLUDED.enabled,
                    updated_at = CURRENT_TIMESTAMP
            """), {
                "tenant_id": tenant_id,
                "url": url,
                "secret": secret,
                "enabled": bool(enabled),
            })
            await session.commit()
        logger.info("repo.webhook_upserted", tenant_id=tenant_id, enabled=bool(enabled))
        return True
    except Exception as e:
        logger.error("repo.webhook_upsert_failed", tenant_id=tenant_id, error=str(e))
        return False


async def get_tenant_webhook(tenant_id: str) -> dict | None:
    """取租户回调配置；未配置返回 None（出站通知对该租户关闭）。"""
    try:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(text("""
                SELECT tenant_id, url, secret, enabled, updated_at
                FROM tenant_webhooks
                WHERE tenant_id = :tenant_id
            """), {"tenant_id": tenant_id})).first()
        if row is None:
            return None
        return {
            "tenant_id": row.tenant_id,
            "url": row.url,
            "secret": row.secret or "",
            "enabled": bool(row.enabled),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
    except Exception as e:
        logger.error("repo.webhook_get_failed", tenant_id=tenant_id, error=str(e))
        return None


async def enqueue_outbound_event(
    event_type: str,
    aggregate_id: str,
    payload: dict,
    tenant_id: str = DEFAULT_TENANT,
) -> str | None:
    """把一条业务事件写入发件箱。

    幂等：`(event_type, aggregate_id)` 唯一索引 + ON CONFLICT DO NOTHING，
    重复落同一事件（如流程重跑 save_report）不会产生第二条，返回 None。
    """
    event_id = f"evt-{uuid.uuid4().hex[:12]}"
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("""
                INSERT INTO outbound_events
                    (event_id, tenant_id, event_type, aggregate_id, payload,
                     status, attempts, next_retry_at)
                VALUES
                    (:event_id, :tenant_id, :event_type, :aggregate_id,
                     CAST(:payload AS JSONB), 'pending', 0, CURRENT_TIMESTAMP)
                ON CONFLICT (event_type, aggregate_id) DO NOTHING
            """), {
                "event_id": event_id,
                "tenant_id": tenant_id,
                "event_type": event_type,
                "aggregate_id": aggregate_id,
                "payload": json.dumps(payload, ensure_ascii=False),
            })
            await session.commit()
        if not result.rowcount:
            logger.info("repo.outbound_event_duplicated",
                        event_type=event_type, aggregate_id=aggregate_id)
            return None
        logger.info("repo.outbound_event_enqueued",
                    event_id=event_id, event_type=event_type,
                    aggregate_id=aggregate_id, tenant_id=tenant_id)
        return event_id
    except Exception as e:
        logger.error("repo.outbound_event_enqueue_failed",
                     event_type=event_type, aggregate_id=aggregate_id, error=str(e))
        return None


def _event_row_to_dict(row) -> dict:
    """发件箱行 → dict（payload 统一解析成对象）。"""
    return {
        "event_id": row.event_id,
        "tenant_id": row.tenant_id,
        "event_type": row.event_type,
        "aggregate_id": row.aggregate_id,
        "payload": _as_dict(row.payload) or {},
        "status": row.status,
        "attempts": row.attempts,
        "last_error": row.last_error or "",
        "next_retry_at": row.next_retry_at.isoformat() if row.next_retry_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
    }


_EVENT_COLS = ("event_id, tenant_id, event_type, aggregate_id, payload, status, "
               "attempts, last_error, next_retry_at, created_at, sent_at")


async def list_pending_outbound_events(limit: int = 20) -> list[dict]:
    """取到期待投递的 pending 事件（按创建时间先进先出）。"""
    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(text(f"""
                SELECT {_EVENT_COLS}
                FROM outbound_events
                WHERE status = 'pending' AND next_retry_at <= CURRENT_TIMESTAMP
                ORDER BY created_at ASC
                LIMIT :limit
            """), {"limit": int(limit)})).all()
        return [_event_row_to_dict(r) for r in rows]
    except Exception as e:
        logger.error("repo.outbound_events_list_pending_failed", error=str(e))
        return []


async def mark_outbound_event_sent(event_id: str) -> bool:
    """标记投递成功。"""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("""
                UPDATE outbound_events
                SET status = 'sent', sent_at = CURRENT_TIMESTAMP,
                    attempts = attempts + 1, last_error = ''
                WHERE event_id = :event_id
            """), {"event_id": event_id})
            await session.commit()
        return True
    except Exception as e:
        logger.error("repo.outbound_event_mark_sent_failed",
                     event_id=event_id, error=str(e))
        return False


async def mark_outbound_event_retry(event_id: str, error: str,
                                    delay_seconds: int) -> bool:
    """投递失败但仍有重试额度：attempts+1，退避到 now + delay 再投。"""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("""
                UPDATE outbound_events
                SET attempts      = attempts + 1,
                    last_error    = :error,
                    next_retry_at = CURRENT_TIMESTAMP
                                    + (CAST(:delay AS INTEGER) * INTERVAL '1 second')
                WHERE event_id = :event_id
            """), {"event_id": event_id, "error": (error or "")[:500],
                   "delay": int(delay_seconds)})
            await session.commit()
        return True
    except Exception as e:
        logger.error("repo.outbound_event_mark_retry_failed",
                     event_id=event_id, error=str(e))
        return False


async def mark_outbound_event_failed(event_id: str, error: str) -> bool:
    """重试额度耗尽：置 failed（保留记录供审计与人工重投）。"""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("""
                UPDATE outbound_events
                SET status = 'failed', attempts = attempts + 1, last_error = :error
                WHERE event_id = :event_id
            """), {"event_id": event_id, "error": (error or "")[:500]})
            await session.commit()
        logger.warning("repo.outbound_event_failed", event_id=event_id, error=error)
        return True
    except Exception as e:
        logger.error("repo.outbound_event_mark_failed_failed",
                     event_id=event_id, error=str(e))
        return False


async def get_outbound_event(event_id: str, tenant_id: str = DEFAULT_TENANT) -> dict | None:
    """按事件 ID 查询（租户隔离）。"""
    try:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(text(f"""
                SELECT {_EVENT_COLS}
                FROM outbound_events
                WHERE event_id = :event_id AND tenant_id = :tenant_id
            """), {"event_id": event_id, "tenant_id": tenant_id})).first()
        return _event_row_to_dict(row) if row else None
    except Exception as e:
        logger.error("repo.outbound_event_get_failed", event_id=event_id, error=str(e))
        return None


async def list_outbound_events(tenant_id: str = DEFAULT_TENANT,
                               limit: int = 50) -> list[dict]:
    """列出本租户出站事件（最近优先），供审计 / 排障。"""
    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(text(f"""
                SELECT {_EVENT_COLS}
                FROM outbound_events
                WHERE tenant_id = :tenant_id
                ORDER BY created_at DESC
                LIMIT :limit
            """), {"tenant_id": tenant_id, "limit": int(limit)})).all()
        return [_event_row_to_dict(r) for r in rows]
    except Exception as e:
        logger.error("repo.outbound_events_list_failed", tenant_id=tenant_id, error=str(e))
        return []


async def reset_outbound_event_for_redelivery(event_id: str,
                                              tenant_id: str = DEFAULT_TENANT) -> bool:
    """把 failed/sent 事件重置为 pending 以便手动重投（租户隔离）。"""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("""
                UPDATE outbound_events
                SET status = 'pending', attempts = 0, last_error = '',
                    next_retry_at = CURRENT_TIMESTAMP, sent_at = NULL
                WHERE event_id = :event_id AND tenant_id = :tenant_id
            """), {"event_id": event_id, "tenant_id": tenant_id})
            await session.commit()
        return bool(result.rowcount)
    except Exception as e:
        logger.error("repo.outbound_event_reset_failed", event_id=event_id, error=str(e))
        return False


# ── 告警：alerts 表 + dispatch_orders 表 ────────────────────

async def save_alert_triggered(
    alert_id: str,
    site_id: str,
    device_id: str,
    alert_type: str,
    risk_assessment: dict,
    three_tracks: dict,
    tenant_id: str = DEFAULT_TENANT,
    source: str = "local",
    external_alert_id: str | None = None,
    location: str = "",
    device_type: str = "",
    type_code: str = "",
) -> bool:
    """告警触发后（站长审核前）落库：状态 reviewing，保存风险评估与四轨数据。

    three_tracks 形如 {"device_readings": [...], "service_records": [...], "health_indicators": [...]}
    供 GET /alert/{id}/review 接口读取，替代 MemorySaver 反查。

    source / external_alert_id / location 用于对接机构 SaaS 的报警记录（报警记录）：
      care = care 自触发；saas = SaaS 下发，external_alert_id 是 SaaS 侧报警记录 ID，
      处置完成后业务系统可按它回写处置结果。
    """
    try:
        if not await _guard_tenant(site_id, tenant_id):
            return False
        await _ensure_device(device_id, site_id, tenant_id,
                             device_type=device_type, type_code=type_code, location=location)
        async with AsyncSessionLocal() as session:
            await session.execute(text("""
                INSERT INTO alerts
                    (alert_id, site_id, device_id, alert_type, status, risk_level,
                     risk_assessment, three_tracks, tenant_id, source, external_alert_id, location)
                VALUES
                    (:alert_id, :site_id, :device_id, :alert_type, 'reviewing', :risk_level,
                     CAST(:risk_assessment AS JSONB), CAST(:three_tracks AS JSONB), :tenant_id,
                     :source, :external_alert_id, :location)
                ON CONFLICT (alert_id) DO UPDATE SET
                    status          = 'reviewing',
                    risk_level      = EXCLUDED.risk_level,
                    risk_assessment = EXCLUDED.risk_assessment,
                    three_tracks    = EXCLUDED.three_tracks,
                    location        = EXCLUDED.location
            """), {
                "alert_id": alert_id,
                "site_id": site_id,
                "device_id": device_id,
                "alert_type": alert_type,
                "risk_level": (risk_assessment or {}).get("risk_level", "medium"),
                "risk_assessment": json.dumps(risk_assessment or {}, ensure_ascii=False),
                "three_tracks": json.dumps(three_tracks or {}, ensure_ascii=False),
                "tenant_id": tenant_id,
                "source": source or "local",
                "external_alert_id": external_alert_id or None,
                "location": location or "",
            })
            await session.commit()
        logger.info("repo.alert_saved", alert_id=alert_id, site_id=site_id,
                    risk_level=(risk_assessment or {}).get("risk_level"),
                    source=source or "local", external_alert_id=external_alert_id or "")
        return True
    except Exception as e:
        logger.error("repo.alert_save_failed", alert_id=alert_id, error=str(e))
        return False


async def find_alert_by_external_id(external_alert_id: str,
                                  tenant_id: str = DEFAULT_TENANT) -> dict | None:
    """按 SaaS 报警记录 ID 反查 care 告警（下行重放幂等：同一条报警只研判一次）。"""
    if not external_alert_id:
        return None
    try:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(text("""
                SELECT alert_id, site_id, device_id, alert_type, status, risk_level
                FROM alerts WHERE tenant_id = :tenant_id AND external_alert_id = :external_alert_id
            """), {"tenant_id": tenant_id, "external_alert_id": external_alert_id})).first()
        if row is None:
            return None
        return {"alert_id": row.alert_id, "site_id": row.site_id,
                "device_id": row.device_id, "alert_type": row.alert_type,
                "status": row.status, "risk_level": row.risk_level}
    except Exception as e:
        logger.error("repo.alert_by_external_id_failed",
                     external_alert_id=external_alert_id, error=str(e))
        return None


async def mark_alert_handled(alert_id: str, handled_by: str,
                             tenant_id: str = DEFAULT_TENANT) -> bool:
    """记录处置人与处置时间（对齐 SaaS 报警记录的"处理人/处理时间"）。"""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("""
                UPDATE alerts SET handled_by = :handled_by, handled_at = CURRENT_TIMESTAMP
                WHERE alert_id = :alert_id AND tenant_id = :tenant_id
            """), {"handled_by": handled_by, "alert_id": alert_id, "tenant_id": tenant_id})
            if result.rowcount == 0:
                await session.rollback()
                return False
            await session.commit()
        return True
    except Exception as e:
        logger.error("repo.alert_handled_failed", alert_id=alert_id, error=str(e))
        return False


async def save_alert_dispatched(alert_id: str, site_id: str, dispatch_order: dict,
                                tenant_id: str = DEFAULT_TENANT) -> bool:
    """站长确认后：更新告警状态 + 写入派发工单（跨租户告警拒绝更新）。"""
    try:
        order_id = dispatch_order.get("order_id") or f"order-{uuid.uuid4().hex[:12]}"
        status = dispatch_order.get("status", "dispatched")
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("""
                UPDATE alerts
                SET status = :status
                WHERE alert_id = :alert_id AND tenant_id = :tenant_id
            """), {"status": status, "alert_id": alert_id, "tenant_id": tenant_id})
            if result.rowcount == 0:
                await session.rollback()
                logger.warning("repo.tenant_violation", alert_id=alert_id, tenant_id=tenant_id)
                return False

            await session.execute(text("""
                INSERT INTO dispatch_orders
                    (order_id, alert_id, site_id, order_content, status, tenant_id)
                VALUES
                    (:order_id, :alert_id, :site_id, :order_content, :status, :tenant_id)
                ON CONFLICT (order_id) DO UPDATE SET
                    order_content = EXCLUDED.order_content,
                    status        = EXCLUDED.status
            """), {
                "order_id": order_id,
                "alert_id": alert_id,
                "site_id": site_id,
                "order_content": dispatch_order.get("order_content", ""),
                "status": status,
                "tenant_id": tenant_id,
            })
            await session.commit()
        logger.info("repo.alert_dispatched", alert_id=alert_id, order_id=order_id, status=status)
        return True
    except Exception as e:
        logger.error("repo.alert_dispatch_failed", alert_id=alert_id, error=str(e))
        return False


async def get_alert_review(alert_id: str, tenant_id: str = DEFAULT_TENANT) -> dict | None:
    """按告警 ID 查询审核数据（强制租户过滤，跨租户视为不存在）。"""
    try:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(text("""
                SELECT alert_id, site_id, device_id, alert_type, status, risk_level,
                       risk_assessment, three_tracks, created_at,
                       source, external_alert_id, location, handled_by, handled_at
                FROM alerts
                WHERE alert_id = :alert_id AND tenant_id = :tenant_id
            """), {"alert_id": alert_id, "tenant_id": tenant_id})).first()
        if row is None:
            return None
        return {
            "alert_id": row.alert_id,
            "site_id": row.site_id,
            "device_id": row.device_id,
            "alert_type": row.alert_type,
            "status": row.status,
            "risk_level": row.risk_level,
            "risk_assessment": _as_dict(row.risk_assessment) or {},
            "three_tracks": _as_dict(row.three_tracks) or {},
            "created_at": row.created_at.isoformat() if row.created_at else None,
            # 对接机构 SaaS 报警记录所需的字段（SaaS 侧按下行报警 ID 关联处置）
            "source": row.source or "local",
            "external_alert_id": row.external_alert_id or "",
            "location": row.location or "",
            "handled_by": row.handled_by or "",
            "handled_at": row.handled_at.isoformat() if row.handled_at else None,
        }
    except Exception as e:
        logger.error("repo.alert_get_failed", alert_id=alert_id, error=str(e))
        return None


# ── 用户认证：users 表 ───────────────────────────────────────

# 默认账号（与原 MOCK_USERS 一致，便于平滑迁移；密码为 bcrypt 哈希）
DEFAULT_USERS: list[dict] = [
    {"user_id": "u-worker",  "username": "worker",
     "password_hash": "$2b$12$ucHhpWFw3/ZQzp34HXxZxuVXYEvHlYyCI1puiS7bKw5hBg/67gk02",
     "role": "worker"},   # Worker@123
    {"user_id": "u-supervisor", "username": "supervisor",
     "password_hash": "$2b$12$MKzMKo8McwqizaA5HdMKWO2KfcJ1s0QqJs97x0Jipf7rXvbD6/vpu",
     "role": "supervisor"},  # Supervisor@123
    {"user_id": "u-admin",      "username": "admin",
     "password_hash": "$2b$12$FjgrgbimEaGKtfusbf2Fau5LrW/eFq.zWfyE44OcJ.vVwRQRIVy0u",
     "role": "admin"},       # Admin@123
]


async def ensure_default_users() -> None:
    """幂等播种默认账号（不覆盖已有记录）。"""
    try:
        async with AsyncSessionLocal() as session:
            for u in DEFAULT_USERS:
                await session.execute(text("""
                    INSERT INTO users (user_id, username, password_hash, role, tenant_id)
                    VALUES (:user_id, :username, :password_hash, :role, 'tenant_default')
                    ON CONFLICT (username) DO NOTHING
                """), u)
            await session.commit()
        logger.info("repo.default_users_ensured", count=len(DEFAULT_USERS))
    except Exception as e:
        logger.error("repo.default_users_ensure_failed", error=str(e))


async def get_user_by_username(username: str) -> dict | None:
    """按用户名查询用户（登录验证用）。"""
    try:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(text("""
                SELECT user_id, username, password_hash, role, tenant_id
                FROM users WHERE username = :username
            """), {"username": username})).first()
            if not row:
                return None
            return {
                "user_id": row.user_id,
                "username": row.username,
                "password_hash": row.password_hash,
                "role": row.role,
                "tenant_id": row.tenant_id,
            }
    except Exception as e:
        logger.error("repo.user_get_failed", username=username, error=str(e))
        return None
