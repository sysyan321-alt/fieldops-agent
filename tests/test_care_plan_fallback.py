# tests/test_care_plan_fallback.py
# care_flow load_context_node 的照护计划来源契约（不依赖 DB：monkeypatch repository 层）
#
# 契约（改造后）：
#   ① Pipeline 上游注入的 care_plan 覆盖 Provider 的计划；
#   ② 无上游时走 CarePlanProvider：业务服务项 + 评估派生计划；
#   ③ 两者都没有 → care_plan_missing=True，**不再编造 mock 计划**；
#   ④ 取数抛异常 → 同样标记缺失，不编数据；
#   ⑤ 照护历史来自真实记录，没有就是空列表（原来无论来源都用两条写死的记录）。

import pytest

from backend.agents.care_flow.graph import _route_after_load_context
from backend.agents.care_flow.nodes import load_context_node
from backend.db import repositories as repo


def _base_state(**overrides):
    state = {
        "elder_id": "e001",
        "tenant_id": "tenant_default",
        "care_plan_id": "",
    }
    state.update(overrides)
    return state


def _patch_repo(monkeypatch, *, items=None, plan=None, history=None, raises=False):
    """把 repositories 的三个取数函数换成固定返回。"""
    async def _items(elder_id, tenant_id="tenant_default"):
        if raises:
            raise RuntimeError("db down")
        return items or []

    async def _plan(elder_id, tenant_id="tenant_default"):
        if raises:
            raise RuntimeError("db down")
        return plan

    async def _history(elder_id, tenant_id="tenant_default", limit=8):
        if raises:
            raise RuntimeError("db down")
        return history or []

    monkeypatch.setattr(repo, "list_service_items", _items)
    monkeypatch.setattr(repo, "get_latest_care_plan", _plan)
    monkeypatch.setattr(repo, "list_care_history", _history)


@pytest.mark.asyncio
async def test_plan_from_pipeline_upstream(monkeypatch):
    """① 上游注入 plan 时覆盖 Provider 的计划。"""
    _patch_repo(monkeypatch, plan={
        "plan_id": "plan-db-1",
        "plan_content": {"care_level": "自理", "daily_tasks": ["DB 任务"]},
    })

    upstream = {
        "plan_id": "plan-asmt-1",
        "care_level": "介护",
        "daily_tasks": ["[HIGH] 鼻饲管护理"],
        "special_notes": "卧床",
    }
    out = await load_context_node(_base_state(care_plan=upstream))

    assert out["plan_source"] == "pipeline"
    assert out["care_plan"]["care_level"] == "介护"
    assert out["care_plan"]["plan_id"] == "plan-asmt-1"
    assert out["care_plan_missing"] is False


@pytest.mark.asyncio
async def test_plan_from_ai_assessment_when_only_ai_record(monkeypatch):
    """② 只有评估派生的计划时，来源标 ai_assessment。"""
    _patch_repo(monkeypatch, plan={
        "plan_id": "plan-seed-001", "elder_id": "e001",
        "plan_content": {"care_level": "介助", "daily_tasks": ["晨间护理", "每2小时翻身"],
                         "special_notes": "高血压"},
        "created_at": "2026-10-01 00:00:00",
    })

    out = await load_context_node(_base_state())

    assert out["plan_source"] == "ai_assessment"
    assert out["care_plan"]["care_level"] == "介助"
    assert out["care_plan"]["plan_id"] == "plan-seed-001"
    assert len(out["care_plan"]["daily_tasks"]) == 2
    assert out["care_plan_missing"] is False


@pytest.mark.asyncio
async def test_plan_from_service_items_when_no_ai_record(monkeypatch):
    """②' 只有机构登记的服务项时，任务清单由服务项生成（不是模型编的）。"""
    _patch_repo(monkeypatch, items=[{
        "project_name": "协助翻身", "frequency_name": "每2小时", "times_per_day": 6,
        "nurse_time_slots": [{"startTime": "08:00", "endTime": "10:00"}],
        "is_required": True, "care_level": "介护",
    }])

    out = await load_context_node(_base_state())

    assert out["plan_source"] == "service_items"
    assert out["care_plan"]["daily_tasks"] == ["协助翻身（每2小时） 时间窗：08:00-10:00"]
    assert out["care_plan"]["care_level"] == "介护"


@pytest.mark.asyncio
async def test_service_items_override_ai_tasks_when_merged(monkeypatch):
    """两边都有 → merged，且任务清单以**业务服务项**为准（AI 不决定做什么服务）。"""
    _patch_repo(
        monkeypatch,
        items=[{"project_name": "口腔护理", "frequency_name": "2次/日", "times_per_day": 2,
                "is_required": True, "care_level": "介助"}],
        plan={"plan_id": "plan-1", "plan_content": {
            "care_level": "介助", "daily_tasks": ["[HIGH] AI 建议的任务"],
            "special_notes": "认知障碍需提醒"}},
    )

    out = await load_context_node(_base_state())

    assert out["plan_source"] == "merged"
    assert out["care_plan"]["daily_tasks"] == ["口腔护理（2次/日）"], "业务服务项应覆盖 AI 任务清单"
    assert out["care_plan"]["special_notes"] == "认知障碍需提醒", "AI 的注意事项应保留"
    assert out["care_plan"]["plan_id"] == "plan-1"


@pytest.mark.asyncio
async def test_plan_missing_instead_of_mock(monkeypatch):
    """③ 两边都没有 → 标记缺失，不再产出 mock 计划（这是本次改造的核心）。"""
    _patch_repo(monkeypatch)

    out = await load_context_node(_base_state())

    assert out["care_plan_missing"] is True
    assert out["plan_source"] == "missing"
    assert out["care_plan"]["daily_tasks"] == [], "缺失时不得凭空生成任务清单"
    assert not out["care_plan"].get("care_level")
    assert out["care_history"] == [], "没有记录就是空列表，不得编造历史"


@pytest.mark.asyncio
async def test_plan_missing_when_provider_raises(monkeypatch):
    """④ Provider 抛异常（DB 挂了 / 远端不可用）→ 同样标记缺失，不编数据、不中断图。"""
    _patch_repo(monkeypatch, raises=True)

    out = await load_context_node(_base_state())

    assert out["care_plan_missing"] is True
    assert out["plan_source"] == "missing"
    assert out["care_history"] == []


@pytest.mark.asyncio
async def test_care_history_comes_from_records(monkeypatch):
    """⑤ 照护历史来自真实记录（原来是写死的两条 mock）。"""
    _patch_repo(monkeypatch,
                items=[{"project_name": "巡视", "is_required": True, "care_level": "介助"}],
                history=[{"date": "2026-10-12", "tasks": ["巡视：老人安静休息"], "abnormal": ""}])

    out = await load_context_node(_base_state())

    assert out["care_history"] == [
        {"date": "2026-10-12", "tasks": ["巡视：老人安静休息"], "abnormal": ""}]


def test_router_sends_missing_plan_to_notice():
    """计划缺失时路由到告知节点，而不是继续推进阶段。"""
    assert _route_after_load_context({"care_plan_missing": True}) == "notify_missing_plan"
    assert _route_after_load_context({"care_plan_missing": False}) == "check_stage"
    assert _route_after_load_context({}) == "check_stage", "缺字段时按有计划处理（兼容旧状态）"


@pytest.mark.asyncio
async def test_notify_missing_plan_node_is_actionable():
    """告知内容要说明原因和下一步，而不是一句"服务不可用"。"""
    from backend.agents.care_flow.nodes import notify_missing_plan_node

    out = await notify_missing_plan_node({"elder_id": "e999"})

    assert out["structured_output"]["plan_missing"] is True
    assert "e999" in out["answer"]
    assert "能力评估" in out["answer"] and "照护服务项" in out["answer"]
