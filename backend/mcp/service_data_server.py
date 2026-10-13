# backend/mcp/service_data_server.py
# 服务数据 MCP Server（只读）：点位档案 / 服务记录 / 健康指标 / 设备读数 / 告警列表
#
# 端点：挂载在 /mcp/service-data，工具端点为 POST /mcp/service-data/mcp
# 独立运行：python -m backend.mcp.service_data_server（默认 8012 端口）
#
# 安全约定：所有工具都把 tenant_id 作为必填参数，SQL 全部强制 tenant_id 过滤，
# 与 REST 层一致——跨租户数据一律返回"不存在"，不泄露 ID 是否存在。

from sqlalchemy import text

from mcp.server.fastmcp import FastMCP

from backend.core.logger import get_logger
from backend.dependencies import AsyncSessionLocal

logger = get_logger(__name__)

mcp = FastMCP(
    name="FieldOps-ServiceData",
    instructions=(
        "FieldOps 作业能力工具 · 服务数据查询（v1.0，只读，免鉴权）。"
        "工具：get_site_profile（点位档案）、get_recent_service_records（服务记录）、"
        "get_recent_health_indicators（健康指标）、get_recent_device_readings（设备读数）、list_alerts（告警列表）。"
        "所有工具必须传 tenant_id，跨租户按不存在返回。"
    ),
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
async def get_site_profile(site_id: str, tenant_id: str) -> dict:
    """查询点位基本档案（含当前服务计划与绑定设备）。

    Args:
        site_id:  点位 ID（如 s001）
        tenant_id: 租户 ID，必填

    Returns:
        点位信息 + 服务计划 + 设备列表；点位不存在或跨租户时返回 {"found": false}
    """
    async with AsyncSessionLocal() as s:
        site = (await s.execute(text(
            "SELECT site_id, name, built_year, site_type, location, risk_tier, created_at"
            " FROM sites WHERE site_id = :eid AND tenant_id = :tid"),
            {"eid": site_id, "tid": tenant_id})).fetchone()

        if not site:
            logger.info("mcp.service_data.site_not_found", site_id=site_id)
            return {"found": False, "site_id": site_id}

        # 复用 repository：取该点位最近一次评估生成的服务计划
        from backend.db.repositories import get_latest_service_plan
        plan_rec = await get_latest_service_plan(site_id, tenant_id)

        devices = (await s.execute(text(
            "SELECT device_id, device_type, status FROM devices"
            " WHERE site_id = :eid AND tenant_id = :tid ORDER BY device_id"),
            {"eid": site_id, "tid": tenant_id})).fetchall()

    logger.info("mcp.service_data.site_profile", site_id=site_id, devices=len(devices))
    return {
        "found": True,
        "site_id": site.site_id,
        "name": site.name,
        "built_year": site.built_year,
        "site_type": site.site_type,
        "location": site.location,
        "risk_tier": site.risk_tier,
        "service_plan": (
            {"plan_id": plan_rec["plan_id"], "content": plan_rec["plan_content"],
             "created_at": plan_rec["created_at"]}
            if plan_rec else None
        ),
        "devices": [
            {"device_id": d.device_id, "device_type": d.device_type, "status": d.status}
            for d in devices
        ],
    }


@mcp.tool()
async def get_recent_service_records(site_id: str, tenant_id: str, limit: int = 8) -> list[dict]:
    """查询点位最近的服务记录（告警研判四轨中的第二轨）。

    Args:
        site_id:  点位 ID
        tenant_id: 租户 ID，必填
        limit:     返回条数，默认 8
    """
    from backend.db.iot_repo import get_recent_service_records as _query

    records = await _query(site_id, limit=limit, tenant_id=tenant_id)
    logger.info("mcp.service_data.service_records", site_id=site_id, rows=len(records))
    return records


@mcp.tool()
async def get_recent_health_indicators(site_id: str, tenant_id: str, limit: int = 8) -> list[dict]:
    """查询点位最近健康指标（告警研判四轨中的第三轨）。

    Args:
        site_id:  点位 ID
        tenant_id: 租户 ID，必填
        limit:     返回条数，默认 8
    """
    from backend.db.iot_repo import get_recent_health_indicators as _query

    indicators = await _query(site_id, limit=limit, tenant_id=tenant_id)
    logger.info("mcp.service_data.health_indicators", site_id=site_id, rows=len(indicators))
    return indicators


@mcp.tool()
async def get_recent_device_readings(site_id: str, tenant_id: str, limit: int = 10) -> list[dict]:
    """查询点位最近设备读数（告警研判四轨中的第一轨）。

    Args:
        site_id:  点位 ID
        tenant_id: 租户 ID，必填
        limit:     返回条数，默认 10
    """
    from backend.db.iot_repo import get_recent_device_readings as _query

    readings = await _query(site_id, limit=limit, tenant_id=tenant_id)
    logger.info("mcp.service_data.device_readings", site_id=site_id, rows=len(readings))
    return readings


@mcp.tool()
async def list_alerts(
    tenant_id: str,
    site_id: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """查询告警列表（按时间倒序），可按点位过滤。

    Args:
        tenant_id: 租户 ID，必填
        site_id:  可选，只看某位点位
        limit:     返回条数，默认 10
    """
    sql = (
        "SELECT alert_id, site_id, device_id, alert_type, risk_level, status,"
        " created_at FROM alerts WHERE tenant_id = :tid"
    )
    params: dict = {"tid": tenant_id, "lim": limit}
    if site_id:
        sql += " AND site_id = :eid"
        params["eid"] = site_id
    sql += " ORDER BY created_at DESC NULLS LAST LIMIT :lim"

    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(sql), params)).fetchall()

    logger.info("mcp.service_data.alerts", tenant_id=tenant_id, rows=len(rows))
    return [
        {
            "alert_id": r.alert_id,
            "site_id": r.site_id,
            "device_id": r.device_id,
            "alert_type": r.alert_type,
            "risk_level": r.risk_level,
            "status": r.status,
            "created_at": str(r.created_at),
        }
        for r in rows
    ]


# ── 服务计划相关（业务配置 + 每日执行）──────────────────────
# 分工提醒：这些工具都只读。服务项目/频次属于业务配置（写在 SaaS 侧），
# care 只提供"查询 + 按等级推荐"的能力，唯一会回写数据的是 work-flow 的
# confirm_service_plan_item（写执行结果，不写业务规则）。

@mcp.tool()
async def list_site_service_items(site_id: str, tenant_id: str) -> dict:
    """查询该点位当前生效的服务项（要做什么、多久做一次、作业时间窗、是否必选）。

    Args:
        site_id:  点位 ID
        tenant_id: 租户 ID，必填

    Returns:
        {"found": bool, "items": [...]}；没有登记过服务项时 items 为空数组
        （表示"该点位还没有服务计划"，调用方应引导先做评估或登记服务项）
    """
    from backend.db.repositories import list_service_items

    items = await list_service_items(site_id, tenant_id=tenant_id)
    logger.info("mcp.service_data.service_items", site_id=site_id,
                tenant_id=tenant_id, count=len(items))
    return {"found": bool(items), "site_id": site_id, "items": items}


@mcp.tool()
async def list_service_tier_configs(risk_tier: str, tenant_id: str,
                                  required_only: bool = False) -> list[dict]:
    """按风险等级查询可用的服务项目（业务系统提供「该等级允许做什么」）。

    用途：评估给出风险等级后，先看这个等级"允许做哪些服务"，再决定给点位挂哪些；
    AI 可以在可选集合内推荐，但不能新增集合外的服务。

    Args:
        risk_tier:    风险等级，如 三级 / 二级 / 一级
        tenant_id:     租户 ID，必填
        required_only: True 只返回必选项
    """
    from backend.db.repositories import list_level_configs

    configs = await list_level_configs(risk_tier, tenant_id=tenant_id,
                                       required_only=required_only)
    logger.info("mcp.service_data.level_configs", risk_tier=risk_tier,
                tenant_id=tenant_id, count=len(configs))
    return configs


@mcp.tool()
async def get_today_service_plan(site_id: str, tenant_id: str) -> dict:
    """查询点位**当天**的服务计划与执行进度（今天还剩哪几项没做）。

    注意：生成规则属于业务侧。本地 Provider 下
    该工具会按服务项展开当天条目（演示/离线用），SaaS Provider 下读主系统的当天计划。

    Args:
        site_id:  点位 ID
        tenant_id: 租户 ID，必填

    Returns:
        {"site_id", "plan": [...含 status/done|missed|planned...], "progress": {...}}
    """
    from backend.core.service_plan_provider import get_service_plan_provider
    from backend.db.repositories import daily_plan_progress

    plan = await get_service_plan_provider().today_plan(site_id, tenant_id)
    logger.info("mcp.service_data.today_plan", site_id=site_id,
                tenant_id=tenant_id, count=len(plan))
    return {"site_id": site_id, "plan": plan, "progress": daily_plan_progress(plan)}


if __name__ == "__main__":
    import uvicorn

    port = 8012
    print(f"服务数据 MCP Server → http://localhost:{port}/mcp")
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=port)
