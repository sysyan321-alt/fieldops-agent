# tests/test_risk_tier_binding.py
# 「评估结论 → 风险等级 → 该等级服务项 → 服务任务清单」这条链路的边界测试。
#
# 要守住的边界：**任务清单只能来自业务配置的服务项，不能由评估问题派生**。
# 改造前 _build_service_plan_from_assessment 把 issues 的 suggestion 拼成
# "[HIGH] 建议xxx" 当作 daily_tasks，等于让 AI 决定"该做什么服务"。

from backend.core.service_plan_provider import LocalServicePlanProvider, SaasServicePlanProvider
from backend.core.orchestrator import Orchestrator
from backend.db.repositories import DEFAULT_LEVEL_CONFIGS


def _assessment(issues=None, level="二级"):
    return {
        "assessment_id": "asmt-1",
        "weighted_score": 62.5,
        "dimension_scores": [{"dimension": "ADL", "score": 55}],
        "issues": issues if issues is not None else [
            {"priority": "high", "suggestion": "夜间加装床栏，预防跌倒"},
            {"priority": "medium", "suggestion": "增加蛋白质摄入"},
        ],
        "summary": {"risk_tier_recommendation": level, "overall_comment": "总体偏弱"},
        "structured": {},
    }


def _items():
    return [
        {"project_name": "设备状态检查", "frequency_name": "2次/日", "times_per_day": 2,
         "nurse_time_slots": [{"startTime": "08:00", "endTime": "20:00"}], "is_required": True},
        {"project_name": "日常巡检", "frequency_name": "3次/日", "times_per_day": 3,
         "nurse_time_slots": [], "is_required": True},
    ]


# ── ① 任务清单来自服务项，不来自评估问题 ────────────────────────

def test_daily_tasks_come_from_service_items_not_issues():
    plan = Orchestrator._build_service_plan_from_assessment(_assessment(), "s001", service_items=_items())

    assert plan["daily_tasks"] == [
        "设备状态检查（2次/日） 时间窗：08:00-20:00",
        "日常巡检（3次/日）",
    ]
    # 评估的处置建议不能出现在任务清单里（那是建议，不是服务组织排的服务）
    assert not any("床栏" in t or "蛋白质" in t for t in plan["daily_tasks"])


def test_assessment_conclusions_are_still_carried():
    """砍掉的是"用问题当任务"，不是评估结论本身——等级/问题/注意事项都要保留。"""
    plan = Orchestrator._build_service_plan_from_assessment(_assessment(), "s001", service_items=_items())

    assert plan["risk_tier"] == "二级"
    assert plan["special_notes"] == "总体偏弱"
    assert plan["weighted_score"] == 62.5
    assert plan["high_priority_issues"] == [
        {"priority": "high", "suggestion": "夜间加装床栏，预防跌倒"}]


def test_no_service_items_means_empty_tasks():
    """没绑到服务项时任务清单为空（work_flow 会提示计划缺失），不拿问题凑数。"""
    plan = Orchestrator._build_service_plan_from_assessment(_assessment(), "s001", service_items=[]);

    assert plan["daily_tasks"] == []
    assert plan["service_items"] == []


def test_assessment_fallback_returns_none():
    """评估走兜底（structured_output 为 None）时不构造 service_plan。"""
    assert Orchestrator._build_service_plan_from_assessment(None, "s001") is None


# ── ② 等级模板配置（本地演示配置的形状）────────────────────────

def test_default_level_configs_cover_three_levels():
    levels = {c["risk_tier"] for c in DEFAULT_LEVEL_CONFIGS}
    assert levels == {"三级", "二级", "一级"}
    for c in DEFAULT_LEVEL_CONFIGS:
        assert c["project_name"], "模板项必须有项目名"
        assert isinstance(c["is_required"], bool)
    # 风险等级越高，同一点位的巡检频次必须更密
    level1 = [c for c in DEFAULT_LEVEL_CONFIGS
              if c["risk_tier"] == "一级" and c["project_name"] == "日常巡检"][0]
    level2 = [c for c in DEFAULT_LEVEL_CONFIGS
              if c["risk_tier"] == "二级" and c["project_name"] == "日常巡检"][0]
    level3 = [c for c in DEFAULT_LEVEL_CONFIGS
              if c["risk_tier"] == "三级" and c["project_name"] == "日常巡检"][0]
    assert level1["times_per_day"] > level2["times_per_day"] > level3["times_per_day"]


# ── ③ Provider 的落地责任差异 ─────────────────────────────────

async def test_local_provider_writes_items(monkeypatch):
    """本地实现：本服务把该等级的必选项写进 site_service_items。"""
    called = {}

    async def _bind(site_id, risk_tier, tenant_id="tenant_default"):
        called.update(site_id=site_id, risk_tier=risk_tier, tenant_id=tenant_id)
        return _items()

    monkeypatch.setattr("backend.db.repositories.bind_service_items_for_level", _bind)

    out = await LocalServicePlanProvider().ensure_items_for_level("s001", "二级", "tenant_default")

    assert out == _items()
    assert called == {"site_id": "s001", "risk_tier": "二级", "tenant_id": "tenant_default"}


async def test_saas_provider_only_reads_allowed_items(monkeypatch):
    """SaaS 实现：只取"该等级允许做什么"，**不写主系统的业务数据**。"""
    provider = SaasServicePlanProvider(base_url="http://saas.local/api")
    seen = {}

    async def _post(path, payload):
        seen.update(path=path, payload=payload)
        return {"list": [{"projectName": "设备状态检查", "serverTimes": 2,
                          "nurseTimeDto": [{"startTime": "08:00", "endTime": "20:00"}],
                          "isRequired": 1, "nurseLevelName": "二级"}]}

    monkeypatch.setattr(provider, "_post", _post)

    items = await provider.ensure_items_for_level("s001", "二级", "tenant_default")

    # 断言用常量而不是字面量：路径是占位命名，换系统时只改常量、不动用例
    assert seen["path"] == SaasServicePlanProvider.PATH_AVAILABLE_ITEMS
    assert seen["payload"] == {"siteId": "s001", "nurseLevelName": "二级"}
    assert items[0]["project_name"] == "设备状态检查"
    assert items[0]["times_per_day"] == 2
    assert items[0]["source"] == "template"


def test_saas_provider_requires_base_url():
    """没配地址就得明确报错，而不是悄悄返回空数据。"""
    import pytest
    from backend.core.service_plan_provider import ServicePlanProviderError

    with pytest.raises(ServicePlanProviderError):
        SaasServicePlanProvider(base_url="")
