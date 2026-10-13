# tests/test_device_taxonomy.py
# 设备谱系与证据纪律：
#   ① 设备类型登记表——编码/中文名/历史自由字符串都能解析，认不出就明说认不出；
#   ② 四轨采集不再编造模拟数据——查不到数据记成「证据缺失」，而不是塞假读数；
#   ③ 证据缺失与设备可信度必须进入研判提示词（否则模型会对假数据下结论）；
#   ④ SaaS 下行报警的类型推断。
#
# 背景：改造前 _fetch_device_data/_fetch_service_records/_fetch_health_indicators 在库里
# 查不到数据时会现场生成硬编码读数（心率永远 135、离床永远 15 分钟），
# 导致风险等级由常量决定。这组用例就是防止那个行为回归。

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

def test_registry_matches_saas_device_taxonomy():
    """9 类设备对齐机构 SaaS 的设备列表（equipments/mattress-list）。"""
    assert set(DEVICE_TYPES) == {
        "mattress", "radar", "bracelet", "dx_health", "gate",
        "face_clock", "dx_patrol", "flow_people", "bed_hammock",
    }
    for dt in DEVICE_TYPES.values():
        assert TRUST_MIN <= dt.trust_level <= TRUST_MAX, f"{dt.code} 可信度越界"
        assert dt.trust_reason, f"{dt.code} 缺可信度理由（研判 prompt 要用）"


def test_resolve_by_code_name_and_legacy_string():
    """编码、中文名、历史自由字符串都要能解析（老数据是自由字符串）。"""
    assert resolve_device_type("mattress").code == "mattress"
    assert resolve_device_type("智能床垫").code == "mattress"
    assert resolve_device_type("离床传感器").code == "mattress"      # 种子数据的写法
    assert resolve_device_type("心率手环").code == "bracelet"        # 种子数据的写法
    assert resolve_device_type("跌倒检测").code == "radar"
    assert resolve_device_type("RADAR").code == "radar"              # 大小写不敏感


def test_unknown_device_is_not_guessed():
    """认不出的设备必须返回 None，而不是随便归到某类——猜错会污染证据可信度。"""
    assert resolve_device_type("外星设备") is None
    assert resolve_device_type("") is None
    assert resolve_device_type(None) is None
    assert metrics_of("外星设备") == ()
    # 未知设备按中间偏下可信度处理，并在提示里明确标注"未登记"
    assert trust_level_of("外星设备") == 3
    assert "未登记" in evidence_hint("外星设备")


def test_alert_type_reverse_lookup():
    """SaaS 只下发报警类型时，要能反查通常由哪类设备产生。"""
    assert "radar" in device_types_for_alert("fall_detected")
    assert "mattress" in device_types_for_alert("off_bed_timeout")
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

    state = {"site_id": "e-none", "device_id": "dev-none",
             "tenant_id": "tenant_default", "alert_type": "heart_rate_abnormal"}
    readings = await alert_nodes._fetch_device_data(state)
    records = await alert_nodes._fetch_service_records(state)
    indicators = await alert_nodes._fetch_health_indicators(state)

    assert readings == [], "设备读数查不到时必须返回空，不得生成模拟读数"
    assert records == [], "照护记录查不到时必须返回空，不得生成模拟记录"
    assert indicators == [], "健康指标查不到时必须返回空，不得生成模拟指标"


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
        {"alert_type": "heart_rate_abnormal", "device_id": "", "tenant_id": "tenant_default"})

    assert result["evidence_quality"] == "insufficient"
    assert set(result["evidence_gaps"]) == {"设备读数", "照护记录", "健康指标", "最近一次能力评估"}


async def test_partial_evidence_quality(monkeypatch):
    """只有一轨有数据 → partial（不是 complete，也不是 insufficient）。"""
    async def _devices(_state):
        return [{"metric_name": "心率", "reading_value": "135", "reading_unit": "bpm"}]

    async def _empty(_state):
        return []

    async def _snapshot(_state):
        return {"weighted_score": 62.0, "summary": {}}

    monkeypatch.setattr(alert_nodes, "_fetch_device_data", _devices)
    monkeypatch.setattr(alert_nodes, "_fetch_service_records", _empty)
    monkeypatch.setattr(alert_nodes, "_fetch_health_indicators", _empty)
    monkeypatch.setattr(alert_nodes, "_fetch_assessment_snapshot", _snapshot)

    result = await alert_nodes.run_four_tracks_node(
        {"alert_type": "heart_rate_abnormal", "device_id": "", "tenant_id": "tenant_default"})

    assert result["evidence_quality"] == "partial"
    assert set(result["evidence_gaps"]) == {"照护记录", "健康指标"}


async def test_device_profile_is_resolved_and_carried(monkeypatch):
    """设备谱系要在采集前解析出来，并带进 State 与落库数据。"""
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
    assert profile["type_code"] == "radar", "跌倒告警应推断为跌倒雷达"
    assert profile["source"] == "alert_type_inferred"
    assert profile["trust_level"] == DEVICE_TYPES["radar"].trust_level


# ── ③ 证据纪律进提示词 ────────────────────────────────────────

def test_risk_prompt_contains_evidence_discipline():
    """缺证据不许判 low、低可信设备单源不能定性的规则必须写在提示词里。"""
    from backend.agents.alert.prompts import RISK_ASSESS_PROMPT

    for must_have in ("证据完整性", "不得推测", "insufficient", "不得判 low", "单源证据"):
        assert must_have in RISK_ASSESS_PROMPT, f"提示词缺少证据纪律：{must_have}"


def test_device_metric_filter_uses_registry():
    """按设备类型过滤指标：床垫读心率/呼吸/离床，雷达只关心跌倒类事件。"""
    assert "心率" in metrics_of("mattress")
    assert "跌倒事件" in metrics_of("radar")
    assert "心率" not in metrics_of("radar"), "雷达不该被要求提供心率证据"


# ── ⑤ 设备类型优先级 ──────────────────────────────────────────

async def test_declared_device_type_wins_over_alert_type_inference(monkeypatch):
    """SaaS 报警自带设备类型时，以它为准（比按告警类型反查更贴近事实）。"""
    async def _no_device(*_a, **_kw):
        return None

    monkeypatch.setattr("backend.db.repositories.get_device", _no_device)

    # 床垫报了心率异常：按告警类型反查会得到 mattress/bracelet 两个候选，
    # 但报警自己说是"智能手环"，就该用手环（可信度 3，而不是床垫的 4）
    profile = await alert_nodes.resolve_device_profile(
        {"device_id": "dev-x", "tenant_id": "tenant_default",
         "alert_type": "heart_rate_abnormal", "device_type_declared": "智能手环"})

    assert profile["type_code"] == "bracelet"
    assert profile["source"] == "alert_declared"
    assert profile["trust_level"] == DEVICE_TYPES["bracelet"].trust_level


async def test_declared_type_wins_and_mismatch_is_logged(monkeypatch, caplog):
    """报警声明的类型与本地登记不一致时：采信声明，但必须留痕（登记可能过期）。"""
    async def _device(*_a, **_kw):
        return {"device_id": "dev-001", "site_id": "e001",
                "device_type": "心率手环", "type_code": "bracelet",
                "location": "3号楼201", "status": "active"}

    monkeypatch.setattr("backend.db.repositories.get_device", _device)

    profile = await alert_nodes.resolve_device_profile(
        {"device_id": "dev-001", "tenant_id": "tenant_default",
         "alert_type": "heart_rate_abnormal", "device_type_declared": "智能床垫"})

    assert profile["type_code"] == "mattress", "应采信报警声明的设备类型"
    assert profile["source"] == "alert_declared"
    assert profile["location"] == "3号楼201", "位置仍从设备登记补全"
    assert "type_mismatch" in caplog.text, "声明与登记不一致必须留下可观测信号"


async def test_falls_back_to_device_registry(monkeypatch):
    """报警没带设备类型时，回到设备登记表。"""
    async def _device(*_a, **_kw):
        return {"device_id": "dev-002", "site_id": "e001", "device_type": "离床传感器",
                "type_code": "", "location": "", "status": "active"}

    monkeypatch.setattr("backend.db.repositories.get_device", _device)

    profile = await alert_nodes.resolve_device_profile(
        {"device_id": "dev-002", "tenant_id": "tenant_default",
         "alert_type": "off_bed_timeout"})

    assert profile["type_code"] == "mattress"
    assert profile["source"] == "device_table"


# ── ④ SaaS 下行报警的类型推断 ─────────────────────────────────

def test_infer_alert_type_from_saas_payload():
    """SaaS 只给设备类型 + 报警内容时，要能推出 care 的告警类型编码。"""
    from backend.api.v1.alert import _infer_alert_type

    assert _infer_alert_type("智能床垫", "检测到心率偏高 135bpm") == "heart_rate_abnormal"
    assert _infer_alert_type("跌倒雷达", "检测到跌倒事件") == "fall_detected"
    assert _infer_alert_type("智能床垫", "老人离床超过 15 分钟") == "off_bed_timeout"
    assert _infer_alert_type("门岗闸机", "老人外出未登记") == "wander_detected"
    # 内容无关键词 → 用该设备类型的第一种可能告警
    assert _infer_alert_type("跌倒雷达", "设备触发") == "fall_detected"
    # 设备也认不出 → 兜底不抛异常
    assert _infer_alert_type("外星设备", "未知") == "heart_rate_abnormal"


# ── ⑤ 下行接入接口（POST /alert/ingest）───────────────────────

_USER = {"user_id": "u-supervisor", "role": "supervisor", "tenant_id": "tenant_default"}


async def test_ingest_is_idempotent_on_external_id(monkeypatch):
    """同一 (租户, 业务系统报警 ID) 重复下发时：不重复研判，直接返回既有结果。"""
    from backend.api.v1.alert import AlertIngestRequest, ingest_alert

    async def _existing(external_alert_id, tenant_id="tenant_default"):
        return {"alert_id": "alert-known", "site_id": "e1", "device_id": "d1",
                "alert_type": "fall_detected", "status": "reviewing", "risk_level": "high"}

    ran = {"assessment": False}

    async def _no_run(**_kw):
        ran["assessment"] = True
        return {}

    monkeypatch.setattr("backend.db.repositories.find_alert_by_external_id", _existing)
    monkeypatch.setattr("backend.api.v1.alert._run_alert_assessment", _no_run)

    out = await ingest_alert(
        AlertIngestRequest(external_alert_id="EXT-1", device_id="d1", site_id="e1"),
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
        AlertIngestRequest(external_alert_id="EXT-2", device_id="dev-9", site_id="e2",
                           device_type="跌倒雷达", alert_content="检测到跌倒事件",
                           location="3号楼201-2床", alert_time="2026-10-13 16:00:00"),
        current_user=_USER)

    assert out["source"] == "saas"
    assert out["external_alert_id"] == "EXT-2"
    assert out["alert_type"] == "fall_detected"          # 由设备类型 + 内容推断
    assert seen["source"] == "saas"
    assert seen["external_alert_id"] == "EXT-2"
    assert seen["alert_location"] == "3号楼201-2床"
    assert seen["device_type"] == "跌倒雷达"
