# tests/test_care_level_binding.py
# 「评估结论 → 护理等级 → 该等级服务项 → 照护任务清单」这条链路的边界测试。
#
# 要守住的边界：**任务清单只能来自业务配置的服务项，不能由评估问题派生**。
# 改造前 _build_care_plan_from_assessment 把 issues 的 suggestion 拼成
# "[HIGH] 建议xxx" 当作 daily_tasks，等于让 AI 决定"该做什么服务"。

from backend.core.care_plan_provider import LocalCarePlanProvider, SaasCarePlanProvider
from backend.core.orchestrator import Orchestrator
from backend.db.repositories import DEFAULT_LEVEL_CONFIGS


def _assessment(issues=None, level="介助"):
    return {
        "assessment_id": "asmt-1",
        "weighted_score": 62.5,
        "dimension_scores": [{"dimension": "ADL", "score": 55}],
        "issues": issues if issues is not None else [
            {"priority": "high", "suggestion": "夜间加装床栏，预防跌倒"},
            {"priority": "medium", "suggestion": "增加蛋白质摄入"},
        ],
        "summary": {"care_level_recommendation": level, "overall_comment": "总体偏弱"},
        "structured": {},
    }


def _items():
    return [
        {"project_name": "协助翻身", "frequency_name": "每2小时", "times_per_day": 6,
         "nurse_time_slots": [{"startTime": "08:00", "endTime": "20:00"}], "is_required": True},
        {"project_name": "用药协助", "frequency_name": "3次/日", "times_per_day": 3,
         "nurse_time_slots": [], "is_required": True},
    ]


# ── ① 任务清单来自服务项，不来自评估问题 ────────────────────────

def test_daily_tasks_come_from_service_items_not_issues():
    plan = Orchestrator._build_care_plan_from_assessment(_assessment(), "e001", service_items=_items())

    assert plan["daily_tasks"] == [
        "协助翻身（每2小时） 时间窗：08:00-20:00",
        "用药协助（3次/日）",
    ]
    # 评估的处置建议不能出现在任务清单里（那是建议，不是机构排的服务）
    assert not any("床栏" in t or "蛋白质" in t for t in plan["daily_tasks"])


def test_assessment_conclusions_are_still_carried():
    """砍掉的是"用问题当任务"，不是评估结论本身——等级/问题/注意事项都要保留。"""
    plan = Orchestrator._build_care_plan_from_assessment(_assessment(), "e001", service_items=_items())

    assert plan["care_level"] == "介助"
    assert plan["special_notes"] == "总体偏弱"
    assert plan["weighted_score"] == 62.5
    assert plan["high_priority_issues"] == [
        {"priority": "high", "suggestion": "夜间加装床栏，预防跌倒"}]


def test_no_service_items_means_empty_tasks():
    """没绑到服务项时任务清单为空（care_flow 会提示计划缺失），不拿问题凑数。"""
    plan = Orchestrator._build_care_plan_from_assessment(_assessment(), "e001", service_items=[]);

    assert plan["daily_tasks"] == []
    assert plan["service_items"] == []


def test_assessment_fallback_returns_none():
    """评估走兜底（structured_output 为 None）时不构造 care_plan。"""
    assert Orchestrator._build_care_plan_from_assessment(None, "e001") is None


# ── ② 等级模板配置（本地演示配置的形状）────────────────────────

def test_default_level_configs_cover_three_levels():
    levels = {c["care_level"] for c in DEFAULT_LEVEL_CONFIGS}
    assert levels == {"自理", "介助", "介护"}
    for c in DEFAULT_LEVEL_CONFIGS:
        assert c["project_name"], "模板项必须有项目名"
        assert isinstance(c["is_required"], bool)
    # 介护等级必须有更高的照护强度（翻身频次高于介助）
    ji_hu = [c for c in DEFAULT_LEVEL_CONFIGS
             if c["care_level"] == "介护" and c["project_name"] == "协助翻身"][0]
    jie_zhu = [c for c in DEFAULT_LEVEL_CONFIGS
               if c["care_level"] == "介助" and c["project_name"] == "协助翻身"][0]
    assert ji_hu["times_per_day"] > jie_zhu["times_per_day"]


# ── ③ Provider 的落地责任差异 ─────────────────────────────────

async def test_local_provider_writes_items(monkeypatch):
    """本地实现：care 自己把该等级的必选项写进 elder_service_items。"""
    called = {}

    async def _bind(elder_id, care_level, tenant_id="tenant_default"):
        called.update(elder_id=elder_id, care_level=care_level, tenant_id=tenant_id)
        return _items()

    monkeypatch.setattr("backend.db.repositories.bind_service_items_for_level", _bind)

    out = await LocalCarePlanProvider().ensure_items_for_level("e001", "介助", "tenant_default")

    assert out == _items()
    assert called == {"elder_id": "e001", "care_level": "介助", "tenant_id": "tenant_default"}


async def test_saas_provider_only_reads_allowed_items(monkeypatch):
    """SaaS 实现：只取"该等级允许做什么"，**不写主系统的业务数据**。"""
    provider = SaasCarePlanProvider(base_url="http://saas.local/api")
    seen = {}

    async def _post(path, payload):
        seen.update(path=path, payload=payload)
        return {"list": [{"projectName": "协助翻身", "serverTimes": 6,
                          "nurseTimeDto": [{"startTime": "08:00", "endTime": "20:00"}],
                          "isRequired": 1, "nurseLevelName": "介助"}]}

    monkeypatch.setattr(provider, "_post", _post)

    items = await provider.ensure_items_for_level("e001", "介助", "tenant_default")

    # 断言用常量而不是字面量：路径是占位命名，换系统时只改常量、不动用例
    assert seen["path"] == SaasCarePlanProvider.PATH_AVAILABLE_ITEMS
    assert seen["payload"] == {"elderId": "e001", "nurseLevelName": "介助"}
    assert items[0]["project_name"] == "协助翻身"
    assert items[0]["times_per_day"] == 6
    assert items[0]["source"] == "template"


def test_saas_provider_requires_base_url():
    """没配地址就得明确报错，而不是悄悄返回空数据。"""
    import pytest
    from backend.core.care_plan_provider import CarePlanProviderError

    with pytest.raises(CarePlanProviderError):
        SaasCarePlanProvider(base_url="")
