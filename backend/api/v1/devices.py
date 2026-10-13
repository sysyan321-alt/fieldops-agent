# 设备数据上报 API（IoT 接入）：设备网关推送读数 + 查询
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from backend.dependencies import get_current_user
from backend.db import iot_repo

router = APIRouter(prefix="/devices")


class ReadingItem(BaseModel):
    metric_name: str
    reading_value: str
    reading_unit: str = ""
    is_abnormal: bool = False
    recorded_at: str | None = None


class ReadingsPayload(BaseModel):
    site_id: str
    device_type: str = "未知设备"
    readings: list[ReadingItem] = Field(min_length=1)


@router.post("/{device_id}/readings")
async def upload_readings(
    device_id: str,
    payload: ReadingsPayload,
    current_user: dict = Depends(get_current_user),
):
    """设备网关上报读数（支持批量）。存库供告警 Agent 前三轨读取（第四轨取风险评估）。"""
    from datetime import datetime
    items = []
    for r in payload.readings:
        ts = None
        if r.recorded_at:
            try:
                ts = datetime.fromisoformat(r.recorded_at)
            except ValueError:
                pass
        items.append({"metric_name": r.metric_name, "reading_value": r.reading_value,
                      "reading_unit": r.reading_unit, "is_abnormal": r.is_abnormal,
                      "recorded_at": ts})
    # 租户守卫：点位归属其他租户时拒绝上报（403，不创建跨租户数据）
    tenant_id = current_user["tenant_id"]
    from backend.db.repositories import _ensure_site, _ensure_device
    owner_tenant = await _ensure_site(payload.site_id, tenant_id=tenant_id)
    if owner_tenant != tenant_id:
        raise HTTPException(403, "点位不属于当前租户")
    await _ensure_device(device_id, payload.site_id, tenant_id)
    n = await iot_repo.save_device_readings(
        payload.site_id, device_id, payload.device_type, items, tenant_id=tenant_id)
    if n == 0:
        raise HTTPException(500, "写入失败")
    return {"device_id": device_id, "saved": n}


@router.get("/site/{site_id}/readings")
async def get_readings(
    site_id: str,
    limit: int = 10,
    current_user: dict = Depends(get_current_user),
):
    """查询点位最近设备读数（租户隔离，跨租户返回空列表）。"""
    return {"readings": await iot_repo.get_recent_device_readings(
        site_id, limit, tenant_id=current_user["tenant_id"])}
