# IoT 设备读数数据访问层：上报写入 + 前三轨查询（告警 Agent 使用；第四轨是能力评估，见 repositories.get_latest_assessment）
import uuid
from datetime import datetime
from sqlalchemy import text
from backend.dependencies import AsyncSessionLocal
from backend.core.logger import get_logger

logger = get_logger(__name__)

# 默认租户（与 repositories 保持一致）
DEFAULT_TENANT = "tenant_default"


async def save_device_readings(elder_id, device_id, device_type, readings,
                               tenant_id=DEFAULT_TENANT):
    """批量写入设备读数，返回成功条数。readings: [{metric_name, reading_value, reading_unit, is_abnormal, recorded_at}]"""
    rows = 0
    try:
        async with AsyncSessionLocal() as s:
            for r in readings:
                await s.execute(text(
                    "INSERT INTO device_readings (reading_id, device_id, elder_id, device_type,"
                    " metric_name, reading_value, reading_unit, is_abnormal, recorded_at, tenant_id)"
                    " VALUES (:rid,:did,:eid,:dt,:mn,:rv,:ru,:ab,:ts,:tid)"), {
                    "rid": f"dr-{uuid.uuid4().hex[:12]}",
                    "did": device_id, "eid": elder_id, "dt": device_type,
                    "mn": r.get("metric_name",""), "rv": str(r.get("reading_value","")),
                    "ru": r.get("reading_unit",""), "ab": bool(r.get("is_abnormal",False)),
                    "ts": r.get("recorded_at") or datetime.now(), "tid": tenant_id})
                rows += 1
            await s.commit()
    except Exception as e:
        logger.error("iot.readings_save_failed", error=str(e))
    logger.info("iot.readings_saved", elder_id=elder_id, rows=rows)
    return rows


async def get_recent_device_readings(elder_id, limit=10, tenant_id=DEFAULT_TENANT,
                                     device_id=None, metrics=None):
    """按老人查最近设备读数（第一轨数据源，强制租户过滤）。

    Args:
        device_id: 只看这台设备的读数（告警指定了设备时用；None=该老人全部设备）。
        metrics:   只看这些指标名（按设备类型过滤，如智能床垫只看心率/呼吸/离床）。

    返回的字典**包含 metric_name**：研判 prompt 需要"心率: 135bpm"这样的可读证据，
    只给设备名会让模型把"设备类型"当成"指标含义"。
    """
    try:
        sql = ("SELECT device_id, device_type, metric_name, reading_value,"
               " reading_unit, is_abnormal, recorded_at"
               " FROM device_readings WHERE elder_id = :eid AND tenant_id = :tid")
        params = {"eid": elder_id, "tid": tenant_id, "lim": limit}
        if device_id:
            sql += " AND device_id = :did"
            params["did"] = device_id
        if metrics:
            sql += " AND metric_name = ANY(:names)"
            params["names"] = list(metrics)
        sql += " ORDER BY recorded_at DESC LIMIT :lim"
        async with AsyncSessionLocal() as s:
            rows = (await s.execute(text(sql), params)).fetchall()
        return [{"device_id": r.device_id, "device_type": r.device_type,
                 "metric_name": r.metric_name,
                 "reading_value": r.reading_value, "reading_unit": r.reading_unit,
                 "is_abnormal": r.is_abnormal,
                 "timestamp": r.recorded_at.strftime("%Y-%m-%d %H:%M:%S")}
                for r in rows]
    except Exception as e:
        logger.error("iot.readings_get_failed", error=str(e))
        return []


async def get_recent_care_records(elder_id, limit=8, tenant_id=DEFAULT_TENANT):
    """按老人查最近照护记录（第二轨数据源，强制租户过滤）。"""
    try:
        async with AsyncSessionLocal() as s:
            rows = (await s.execute(text(
                "SELECT record_id, care_type, content, caregiver_id, created_at"
                " FROM care_records WHERE elder_id = :eid AND tenant_id = :tid"
                " ORDER BY created_at DESC LIMIT :lim"),
                {"eid": elder_id, "tid": tenant_id, "lim": limit})).fetchall()
        return [{"record_id": r.record_id, "elder_id": elder_id,
                 "care_type": r.care_type, "content": r.content or "",
                 "caregiver_id": r.caregiver_id,
                 "timestamp": r.created_at.strftime("%Y-%m-%d %H:%M:%S")}
                for r in rows]
    except Exception as e:
        logger.error("iot.care_get_failed", error=str(e))
        return []


_VITAL_RANGES = {
    "心率": ("bpm", "60-100"), "血压": ("mmHg", "90-140/60-90"),
    "体温": ("℃", "36-37.2"), "血氧": ("%", "95-100"),
}


async def get_recent_health_indicators(elder_id, limit=8, tenant_id=DEFAULT_TENANT):
    """从 device_readings 提取生命体征类读数（第三轨，强制租户过滤）。"""
    try:
        async with AsyncSessionLocal() as s:
            rows = (await s.execute(text(
                "SELECT metric_name, reading_value, reading_unit, is_abnormal, recorded_at"
                " FROM device_readings WHERE elder_id = :eid AND tenant_id = :tid"
                " AND metric_name = ANY(:names)"
                " ORDER BY recorded_at DESC LIMIT :lim"),
                {"eid": elder_id, "tid": tenant_id, "names": list(_VITAL_RANGES),
                 "lim": limit})).fetchall()
        out, seen = [], set()
        for r in rows:
            if r.metric_name in seen:
                continue
            seen.add(r.metric_name)
            unit, ref = _VITAL_RANGES.get(r.metric_name, (r.reading_unit, ""))
            out.append({"indicator_name": r.metric_name, "value": r.reading_value,
                        "unit": unit, "reference_range": ref,
                        "status": "abnormal" if r.is_abnormal else "normal",
                        "timestamp": r.recorded_at.strftime("%Y-%m-%d %H:%M:%S")})
        return out
    except Exception as e:
        logger.error("iot.vitals_get_failed", error=str(e))
        return []
