# tests/test_device_taxonomy.py
# 设备谱系与证据纪律：
#   ① 设备类型登记表——编码/中文名/别名都能解析，认不出就明说认不出；
#   ② 四轨采集不编造数据——查不到就记「证据缺失」，而不是塞假读数；
#   ③ 证据缺失与设备可信度必须进入研判提示词（否则模型会对假数据下结论）；
#   ④ 业务系统下发的报警类型推断；
#   ⑤ 下行接入接口（POST /alert/ingest）的幂等与取值传递。
#
# 场景：园区/物业现场设备。登记表见 backend/core/device_types.py。

from backend.agents.alert import nodes as alert_nodes
from backend.core.device_types import (
    DEVICE_TYPES,
    TRUST_MAX,
    TRUST_MIN,
    device_types_for_alert,
    evidence_hint,
    metrics_of,
    resolve_device_type,
    trust_level_of,
)


# ── ① 设备类型登记表 ──────────────────────────────────────────

def test_registry_covers_nine_field_devices():
    """9 类现场设备，每类都要登记可信度与理由（研判提示词要用）。"""
    assert set(DEVICE_TYPES) == {
        "power_monitor", "video_analytics", "patrol_terminal", "env_station",
        "access_control", "locator_tag", "patrol_reader", "people_counter",
        "water_level",
    }
    for dt in DEVICE_TYPES.values():
        assert TRUST_MIN <= dt.trust_level <= TRUST_MAX, f"{dt.code} 可信度越界"
        assert dt.trust_reason, f"{dt.code} 缺可信度理由"
        assert dt.metrics, f"{dt.code} 没登记可上报指标"


def test_trust_level_reflects_evidence_quality():
    """可信度要体现"证据强度"而不是随手给分：人工留痕 5 分，易受环境影响的 2 分。"""
    assert trust_level_of("locator_tag") == 5, "人工动作留痕应是最强证据"
    assert trust_level_of("patrol_reader") == 5
    assert trust_level_of("people_counter") == 2, "人流量计数受逆光遮挡影响，证据最弱"
    assert trust_level_of("power_monitor") > trust_level_of("video_analytics"), \
        "直接测电气参数比视频判定可信"


def test_resolve_by_code_and_chinese_name():
    assert resolve_device_type("power_monitor").code == "power_monitor"
    assert resolve_device_type("配电监测").code == "power_monitor"
    assert resolve_device_type("门禁").code == "access_control"
    assert resolve_device_type("手持机").code == "patrol_terminal"          # 别名
    assert resolve_device_type("ACCESS_CONTROL").code == "access_control"   # 大小写不敏感


def test_unknown_device_is_not_guessed():
    """认不出的设备必须返回 None，而不是随便归到某类——猜错会污染证据可信度。"""
    assert resolve_device_type("外星设备") is None
    assert resolve_device_type("") is None
    assert resolve_device_type(None) is None
    assert metrics_of("外星设备") == ()
    # 未知设备按中间偏下可信度处理，并明确标注"未登记"
    assert trust_level_of("外星设备") == 3
    assert "未登记" in evidence_hint("外星设备")


def test_alert_type_reverse_lookup():
    """只下发报警类型时，要能反查通常由哪类设备产生。"""
    assert "video_analytics" in device_types_for_alert("fall_detected")
    assert "power_monitor" in device_types_for_alert("overcurrent")
    assert device_types_for_alert("不存在的告警") == ()


# ── ② 不再编造模拟数据 ────────────────────────────────────────

async def test_tracks_return_empty_instead_of_mock_data(monkeypatch):
    """库里没数据时三条轨必须返回空列表，而不是塞硬编码读数。"""
    from backend.db import iot_repo

    async def _no_readings(*_a, **_kw):
        return []

    monkeypatch.setattr(iot_repo, "get_recent_device_readings", _no_readings)
    monkeypatch.setattr(iot_repo, "get_recent_service_records", _no_readings)
    monkeypatch.setattr(iot_repo, "get_recent_health_indicators", _no_readings)

    state = {"site_id": "s-none", "device_id": "dev-none",
             "tenant_id": "tenant_default", "alert_type": "overcurrent"}
    readings = await alert_nodes._fetch_device_data(state)
    records = await alert_nodes._fetch_service_records(state)
    indicators = await alert_nodes._fetch_health_indicators(state)

    assert readings == [], "设备读数查不到时必须返回空，不得生成模拟读数"
    assert records == [], "服务记录查不到时必须返回空，不得生成模拟记录"
    assert indicators == [], "环境指标查不到时必须返回空，不得生成模拟指标"


async def test_missing_evidence_is_recorded_as_gap(monkeypatch):
    """四轨全空 → evidence_quality=insufficient，且逐轨列出缺失项。"""
    async def _empty(_state):
        return []

    async def _no_assessment(_state):
        return None

    monkeypatch.setattr(alert_nodes, "_fetch_device_data", _empty)
    monkeypatch.setattr(alert_nodes, "_fetch_service_records", _empty)
    monkeypatch.setattr(alert_nodes, "_fetch_health_indicators", _empty)
    monkeypatch.setattr(alert_nodes, "_fetch_assessment_snapshot", _no_assessment)

    result = await alert_nodes.run_four_tracks_node(
        {"alert_type": "overcurrent", "device_id": "", "tenant_id": "tenant_default"})

    assert result["evidence_quality"] == "insufficient"
    assert set(result["evidence_gaps"]) == {
        "设备读数", "照护记录", "健康指标", "最近一次能力评估"}


async def test_partial_evidence_quality(monkeypatch):
    """只有部分轨道有数据 → partial。"""
    async def _devices(_state):
        return [{"metric_name": "电流", "reading_value": "320", "reading_unit": "A"}]

    async def _empty(_state):
        return []

    async def _snapshot(_state):
        return {"weighted_score": 62.0, "summary": {}}

    monkeypatch.setattr(alert_nodes, "_fetch_device_data", _devices)
    monkeypatch.setattr(alert_nodes, "_fetch_service_records", _empty)
    monkeypatch.setattr(alert_nodes, "_fetch_health_indicators", _empty)
    monkeypatch.setattr(alert_nodes, "_fetch_assessment_snapshot", _snapshot)

    result = await alert_nodes.run_four_tracks_node(
        {"alert_type": "overcurrent", "device_id": "", "tenant_id": "tenant_default"})

    assert result["evidence_quality"] == "partial"
    assert set(result["evidence_gaps"]) == {"照护记录", "健康指标"}


async def test_device_profile_is_resolved_and_carried(monkeypatch):
    """设备谱系要在采集前解析出来，并带进 State。"""
    async def _empty(_state):
        return []

    async def _snapshot(_state):
        return None

    monkeypatch.setattr(alert_nodes, "_fetch_device_data", _empty)
    monkeypatch.setattr(alert_nodes, "_fetch_service_records", _empty)
    monkeypatch.setattr(alert_nodes, "_fetch_health_indicators", _empty)
    monkeypatch.setattr(alert_nodes, "_fetch_assessment_snapshot", _snapshot)

    result = await alert_nodes.run_four_tracks_node(
        {"alert_type": "fall_detected", "device_id": "", "tenant_id": "tenant_default"})

    profile = result["device_profile"]
    assert profile["type_code"] == "video_analytics", "跌倒告警应推断为视频行为分析"
    assert profile["source"] == "alert_type_inferred"
    assert profile["trust_level"] == DEVICE_TYPES["video_analytics"].trust_level


# ── ③ 证据纪律进提示词 ────────────────────────────────────────

def test_risk_prompt_contains_evidence_discipline():
    """缺证据不许判 low、低可信设备单源不能定性的规则必须写在提示词里。"""
    from backend.agents.alert.prompts import RISK_ASSESS_PROMPT

    for must_have in ("证据完整性", "不得推测", "insufficient", "不得判 low", "单源证据"):
        assert must_have in RISK_ASSESS_PROMPT, f"提示词缺少证据纪律：{must_have}"


def test_device_metric_filter_uses_registry():
    """按设备类型过滤指标：配电监测读电气参数，视频分析关心事件。"""
    assert "电流" in metrics_of("power_monitor")
    assert "跌倒事件" in metrics_of("video_analytics")
    assert "电流" not in metrics_of("video_analytics"), "视频分析不该被要求提供电流证据"


# ── ④ 设备类型优先级 ──────────────────────────────────────────

async def test_declared_device_type_wins_over_alert_type_inference(monkeypatch):
    """报警自带设备类型时以它为准（比按告警类型反查更贴近事实）。"""
    async def _no_device(*_a, **_kw):
        return None

    monkeypatch.setattr("backend.db.repositories.get_device", _no_device)

    profile = await alert_nodes.resolve_device_profile(
        {"device_id": "dev-x", "tenant_id": "tenant_default",
         "alert_type": "overcurrent", "device_type_declared": "视频行为分析"})

    assert profile["type_code"] == "video_analytics"
    assert profile["source"] == "alert_declared"
    assert profile["trust_level"] == DEVICE_TYPES["video_analytics"].trust_level


async def test_declared_type_wins_and_mismatch_is_logged(monkeypatch, caplog):
    """报警声明的类型与本地登记不一致时：采信声明，但必须留痕（登记可能过期）。"""
    async def _device(*_a, **_kw):
        return {"device_id": "dev-001", "site_id": "s001",
                "device_type": "人流量统计", "type_code": "people_counter",
                "location": "3号楼B1", "status": "active"}

    monkeypatch.setattr("backend.db.repositories.get_device", _device)

    profile = await alert_nodes.resolve_device_profile(
        {"device_id": "dev-001", "tenant_id": "tenant_default",
         "alert_type": "crowd_density", "device_type_declared": "配电监测"})

    assert profile["type_code"] == "power_monitor", "应采信报警声明的设备类型"
    assert profile["source"] == "alert_declared"
    assert profile["location"] == "3号楼B1", "位置仍从设备登记补全"
    assert "type_mismatch" in caplog.text, "声明与登记不一致必须留下可观测信号"


async def test_falls_back_to_device_registry(monkeypatch):
    """报警没带设备类型时，回到设备登记表。"""
    async def _device(*_a, **_kw):
        return {"device_id": "dev-002", "site_id": "s001", "device_type": "水位监测",
                "type_code": "", "location": "", "status": "active"}

    monkeypatch.setattr("backend.db.repositories.get_device", _device)

    profile = await alert_nodes.resolve_device_profile(
        {"device_id": "dev-002", "tenant_id": "tenant_default",
         "alert_type": "water_level_abnormal"})

    assert profile["type_code"] == "water_level"
    assert profile["source"] == "device_table"


# ── ⑤ 业务系统下行报警的类型推断 ──────────────────────────────

def test_infer_alert_type_from_external_payload():
    """业务系统只给设备类型 + 报警内容时，要能推出本服务的告警类型编码。"""
    from backend.api.v1.alert import _infer_alert_type

    assert _infer_alert_type("配电监测", "A相电流过载 320A") == "overcurrent"
    assert _infer_alert_type("视频行为分析", "检测到人员跌倒") == "fall_detected"
    assert _infer_alert_type("视频行为分析", "检测到人员闯入限制区域") == "intrusion_detected"
    assert _infer_alert_type("环境监测站", "可燃气浓度超标") == "air_quality_abnormal"
    assert _infer_alert_type("门禁", "门被非正常开启") == "gate_forced"
    assert _infer_alert_type("水位监测", "地面积水，疑似渗漏") == "water_level_abnormal"
    assert _infer_alert_type("巡更器", "B2 巡点漏巡") == "missed_patrol"
    # 内容无关键词 → 用该设备类型的第一种可能告警
    assert _infer_alert_type("视频行为分析", "设备触发") == "fall_detected"
    # 设备也认不出 → 兜底不抛异常
    assert _infer_alert_type("外星设备", "未知") == "device_offline"


# ── ⑥ 下行接入接口 ────────────────────────────────────────────

_USER = {"user_id": "u-supervisor", "role": "supervisor", "tenant_id": "tenant_default"}


async def test_ingest_is_idempotent_on_external_id(monkeypatch):
    """同一 (租户, 业务系统报警 ID) 重复下发时：不重复研判，直接返回既有结果。"""
    from backend.api.v1.alert import AlertIngestRequest, ingest_alert

    async def _existing(external_alert_id, tenant_id="tenant_default"):
        return {"alert_id": "alert-known", "site_id": "s1", "device_id": "d1",
                "alert_type": "fall_detected", "status": "reviewing", "risk_level": "high"}

    ran = {"assessment": False}

    async def _no_run(**_kw):
        ran["assessment"] = True
        return {}

    monkeypatch.setattr("backend.db.repositories.find_alert_by_external_id", _existing)
    monkeypatch.setattr("backend.api.v1.alert._run_alert_assessment", _no_run)

    out = await ingest_alert(
        AlertIngestRequest(external_alert_id="EXT-1", device_id="d1", site_id="s1"),
        current_user=_USER)

    assert out["duplicated"] is True
    assert out["alert_id"] == "alert-known"
    assert ran["assessment"] is False, "重复下发不该再跑一遍研判（白花一次 LLM）"


async def test_ingest_runs_assessment_and_carries_external_id(monkeypatch):
    """首次下发：带上 source=saas 与业务系统报警 ID（回写时要原样带回）。"""
    from backend.api.v1.alert import AlertIngestRequest, ingest_alert

    async def _none(external_alert_id, tenant_id="tenant_default"):
        return None

    seen = {}

    async def _run(**kw):
        seen.update(kw)
        return {"risk_assessment": {}}

    monkeypatch.setattr("backend.db.repositories.find_alert_by_external_id", _none)
    monkeypatch.setattr("backend.api.v1.alert._run_alert_assessment", _run)

    out = await ingest_alert(
        AlertIngestRequest(external_alert_id="EXT-2", device_id="dev-9", site_id="s2",
                           device_type="视频行为分析", alert_content="检测到人员跌倒",
                           location="3号楼B1", alert_time="2026-10-13 16:00:00"),
        current_user=_USER)

    assert out["source"] == "saas"
    assert out["external_alert_id"] == "EXT-2"
    assert out["alert_type"] == "fall_detected"          # 由设备类型 + 内容推断
    assert seen["source"] == "saas"
    assert seen["external_alert_id"] == "EXT-2"
    assert seen["alert_location"] == "3号楼B1"
    assert seen["device_type"] == "视频行为分析"
