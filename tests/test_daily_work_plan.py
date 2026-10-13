# tests/test_daily_service_plan.py
# 每日照护计划：展开生成、执行回写、进度统计。
#
# 边界提醒（和照护计划那组用例一致）：
#   计划的**生成规则**属于业务侧，本地实现只是演示/离线替身；
#   care 可以回写的是"执行结果"这类事实数据（谁在什么时候做了），不是业务规则。

from datetime import date

import pytest

from backend.db.repositories import (
    _expand_items_to_day,
    daily_plan_progress,
)


# ── ① 按频次展开当天计划 ──────────────────────────────────────

def test_expand_multi_times_project():
    """一天 3 次的"协助进食"要展开成 3 条；频次来自业务配置，不是模型编的。"""
    items = [{
        "project_name": "协助进食", "times_per_day": 3,
        "nurse_time_slots": [{"startTime": "07:00", "endTime": "08:00"},
                             {"startTime": "11:30", "endTime": "12:30"},
                             {"startTime": "17:30", "endTime": "18:30"}],
    }]
    entries = _expand_items_to_day(items, date(2026, 10, 13))

    assert len(entries) == 3
    assert [e["project_name"] for e in entries] == ["协助进食"] * 3
    assert [e["nurse_time"] for e in entries] == [
        "07:00-08:00", "11:30-12:30", "17:30-18:30"]


def test_expand_ids_are_deterministic():
    """ID 必须是确定性的——重复展开才不会产生重复条目（幂等的基础）。"""
    items = [{"project_name": "巡视", "times_per_day": 2, "nurse_time_slots": []}]
    first = _expand_items_to_day(items, date(2026, 10, 13))
    second = _expand_items_to_day(items, date(2026, 10, 13))

    assert [e["plan_item_id"] for e in first] == [e["plan_item_id"] for e in second]
    assert first[0]["plan_item_id"].startswith("dcp-2026-10-13-")


def test_expand_without_time_slots_still_produces_entries():
    """没配护理时间窗也要给出条目（nurse_time 留空），不能因为没窗口就少做一次。"""
    items = [{"project_name": "用药协助", "times_per_day": 2, "nurse_time_slots": []}]
    entries = _expand_items_to_day(items, date(2026, 10, 13))

    assert len(entries) == 2
    assert all(e["nurse_time"] == "" for e in entries)


def test_single_window_multi_times_is_split():
    """一个窗口配多次频次时均分（否则 6 次翻身会是 6 行一模一样的 08:00-20:00）。"""
    items = [{"project_name": "协助翻身", "times_per_day": 6,
              "nurse_time_slots": [{"startTime": "08:00", "endTime": "20:00"}]}]
    entries = _expand_items_to_day(items, date(2026, 10, 13))

    windows = [e["nurse_time"] for e in entries]
    assert len(windows) == 6
    assert len(set(windows)) == 6, "每一段都该不同，护理员要能分清第几次"
    assert windows[0] == "08:00-10:00"
    assert windows[-1] == "18:00-20:00"


def test_split_handles_day_end_notation():
    """"24:00" 这类跨天写法不能抛异常，要能正常均分。"""
    items = [{"project_name": "全天巡视", "times_per_day": 4,
              "nurse_time_slots": [{"startTime": "00:00", "endTime": "24:00"}]}]
    entries = _expand_items_to_day(items, date(2026, 10, 13))

    windows = [e["nurse_time"] for e in entries]
    assert len(set(windows)) == 4
    assert windows[0].startswith("00:00-")


def test_split_falls_back_on_bad_format():
    """时间格式不规范时回退成重复窗口，而不是让整天的计划生成失败。"""
    items = [{"project_name": "巡视", "times_per_day": 3,
              "nurse_time_slots": [{"startTime": "上午", "endTime": "下午"}]}]
    entries = _expand_items_to_day(items, date(2026, 10, 13))

    assert len(entries) == 3
    assert all(e["nurse_time"] == "上午-下午" for e in entries)


def test_expand_times_zero_defaults_to_one():
    """频次缺失或为 0 时按 1 次处理——不能变成"今天不用做"。"""
    assert len(_expand_items_to_day([{"project_name": "巡视", "times_per_day": 0}],
                                    date(2026, 10, 13))) == 1
    assert len(_expand_items_to_day([{"project_name": "巡视"}], date(2026, 10, 13))) == 1


def test_expand_covers_multiple_projects_once():
    """多个项目各展开各的：二级 5 项（含 6 次的翻身）应得到正确总数。"""
    items = [
        {"project_name": "协助翻身", "times_per_day": 6, "nurse_time_slots": []},
        {"project_name": "协助进食", "times_per_day": 3, "nurse_time_slots": []},
        {"project_name": "每日巡视", "times_per_day": 2, "nurse_time_slots": []},
    ]
    entries = _expand_items_to_day(items, date(2026, 10, 13))

    assert len(entries) == 11
    assert len({e["plan_item_id"] for e in entries}) == 11, "ID 不能撞"


# ── ② 进度统计 ────────────────────────────────────────────────

def test_progress_counts_each_state():
    plan = [{"status": "done"}, {"status": "done"}, {"status": "missed"},
            {"status": "planned"}]
    assert daily_plan_progress(plan) == {"total": 4, "done": 2, "missed": 1, "pending": 1}


def test_progress_on_empty_plan():
    assert daily_plan_progress([]) == {"total": 0, "done": 0, "missed": 0, "pending": 0}


# ── ③ Provider 的回写责任差异 ─────────────────────────────────

async def test_local_provider_confirms_locally(monkeypatch):
    """本地实现：回写 care 自己的 daily_work_plans。"""
    from backend.core.service_plan_provider import LocalServicePlanProvider

    seen = {}

    async def _confirm(plan_item_id, tenant_id="tenant_default", **kw):
        seen.update(plan_item_id=plan_item_id, tenant_id=tenant_id, **kw)
        return True

    monkeypatch.setattr("backend.db.repositories.confirm_daily_plan_item", _confirm)

    ok = await LocalServicePlanProvider().confirm_plan_item(
        "dcp-2026-10-13-001", "tenant_default",
        status="done", worker_id="u-1", worker_name="张三", actual_care_time="08:05")

    assert ok is True
    assert seen["plan_item_id"] == "dcp-2026-10-13-001"
    assert seen["status"] == "done"
    assert seen["actual_care_time"] == "08:05"


async def test_saas_provider_posts_confirm(monkeypatch):
    """SaaS 实现：调主系统的执行结果回写接口（写的是执行结果，不是业务规则）。"""
    from backend.core.service_plan_provider import SaasServicePlanProvider

    provider = SaasServicePlanProvider(base_url="http://saas.local/api")
    seen = {}

    async def _post(path, payload):
        seen.update(path=path, payload=payload)
        return {}

    monkeypatch.setattr(provider, "_post", _post)

    ok = await provider.confirm_plan_item(
        "P-100", "tenant_default", status="done", worker_id="u-1",
        worker_name="张三", actual_care_time="08:05")

    assert ok is True
    assert seen["path"] == SaasServicePlanProvider.PATH_CONFIRM_ITEM
    assert seen["payload"]["id"] == "P-100"
    assert seen["payload"]["actualCareTime"] == "08:05"


async def test_saas_provider_normalizes_status(monkeypatch):
    """主系统的状态值要归一成 care 的 done/missed/planned。"""
    from backend.core.service_plan_provider import SaasServicePlanProvider

    provider = SaasServicePlanProvider(base_url="http://saas.local/api")

    async def _post(path, payload):
        return {"list": [{"id": "1", "projectName": "巡视", "statusF": "已完成"},
                         {"id": "2", "projectName": "翻身", "statusF": "未完成"},
                         {"id": "3", "projectName": "进食", "statusF": "待执行"}]}

    monkeypatch.setattr(provider, "_post", _post)

    plan = await provider.today_plan("s001", "tenant_default")

    assert [p["status"] for p in plan] == ["done", "missed", "planned"]


# ── ④ 提示词里要带上当日进度 ──────────────────────────────────

def test_prompts_include_today_plan_placeholder():
    """日常照护/异常处理阶段必须能看到"今天还剩哪几项"（缺占位符会格式化失败）。"""
    from backend.agents.work_flow.prompts import ABNORMAL_PROMPT, DAILY_CARE_PROMPT

    for tpl in (DAILY_CARE_PROMPT, ABNORMAL_PROMPT):
        text = tpl.format(service_plan="{}", worker_input="x", today_plan="今日共 3 项")
        assert "今日共 3 项" in text


def test_format_today_plan_renders_status():
    from backend.agents.work_flow.nodes import _format_today_plan

    text = _format_today_plan(
        [{"project_name": "协助翻身", "nurse_time": "08:00-10:00",
          "status": "done", "worker_name": "张三"},
         {"project_name": "用药协助", "status": "planned"}],
        {"total": 2, "done": 1, "missed": 0, "pending": 1})

    assert "今日共 2 项" in text
    assert "已完成 1" in text and "待执行 1" in text
    assert "[已完成] 协助翻身" in text
    assert "[待执行] 用药协助" in text


def test_format_today_plan_empty():
    from backend.agents.work_flow.nodes import _format_today_plan

    assert "没有可执行的照护计划条目" in _format_today_plan([], {})


# ── ⑤ 计划总览接口（前端视图的数据源）────────────────────────

USER = {"user_id": "u-supervisor", "role": "supervisor", "tenant_id": "tenant_default"}


class _FakeBundle:
    def __init__(self, plan, source):
        self.plan = plan
        self.plan_source = source
        self.history = []


class _FakeProvider:
    """按需返回计划与当天计划，避免测试依赖数据库与 LLM。"""

    def __init__(self, plan=None, source="missing", today=None):
        self._plan, self._source, self._today = plan, source, today or []

    async def fetch(self, site_id, tenant_id, history_limit=8):
        from backend.core.service_plan_provider import ServicePlanBundle
        return ServicePlanBundle(plan=self._plan, history=[],
                              plan_source=self._source, data_source="local")

    async def today_plan(self, site_id, tenant_id):
        return self._today


def _patch_session(monkeypatch, site_id="s001"):
    async def _get(session_id, tenant_id="tenant_default"):
        return {"session_id": session_id, "site_id": site_id}
    monkeypatch.setattr("backend.db.repositories.get_work_flow_session", _get)


async def test_plan_overview_returns_full_picture(monkeypatch):
    """一个接口给足前端：护理等级 / 服务项 / 当天计划 / 进度 / 计划来源。"""
    from backend.api.v1.work_flow import get_plan_overview

    _patch_session(monkeypatch)
    today = [{"plan_item_id": "dcp-1", "project_name": "协助翻身",
              "nurse_time": "08:00-10:00", "status": "done"},
             {"plan_item_id": "dcp-2", "project_name": "用药协助",
              "nurse_time": "12:00-13:00", "status": "planned"}]
    monkeypatch.setattr(
        "backend.core.service_plan_provider.get_service_plan_provider",
        lambda: _FakeProvider(
            plan={"risk_tier": "二级", "special_notes": "高血压",
                  "service_items": [{"project_name": "协助翻身", "is_required": True}]},
            source="merged", today=today))

    out = await get_plan_overview("sess-1", current_user=USER)

    assert out["plan_missing"] is False
    assert out["plan_source"] == "merged"
    assert out["risk_tier"] == "二级"
    assert len(out["service_items"]) == 1
    assert len(out["today_plan"]) == 2
    assert out["progress"] == {"total": 2, "done": 1, "missed": 0, "pending": 1}
    assert out["message"] == ""


async def test_plan_overview_missing_plan_gives_guidance(monkeypatch):
    """没有计划时返回明确引导，并跳过当天计划查询（不渲染空表格）。"""
    from backend.api.v1.work_flow import get_plan_overview

    _patch_session(monkeypatch)
    provider = _FakeProvider(plan=None, source="missing")
    monkeypatch.setattr("backend.core.service_plan_provider.get_service_plan_provider",
                        lambda: provider)

    out = await get_plan_overview("sess-1", current_user=USER)

    assert out["plan_missing"] is True
    assert out["service_items"] == []
    assert out["today_plan"] == []
    assert out["progress"]["total"] == 0
    assert "没有照护计划" in out["message"]


async def test_plan_overview_404_for_unknown_session(monkeypatch):
    """会话不存在或跨租户 → 404（不泄露会话是否存在）。"""
    import pytest
    from fastapi import HTTPException
    from backend.api.v1.work_flow import get_plan_overview

    async def _none(session_id, tenant_id="tenant_default"):
        return None
    monkeypatch.setattr("backend.db.repositories.get_work_flow_session", _none)

    with pytest.raises(HTTPException) as exc:
        await get_plan_overview("nope", current_user=USER)
    assert exc.value.status_code == 404


async def test_confirm_rejects_unknown_status(monkeypatch):
    """回写状态只接受 done/missed——避免外部写入语义不明的值污染执行记录。"""
    import pytest
    from fastapi import HTTPException
    from backend.api.v1.work_flow import PlanItemConfirmRequest, confirm_plan_item

    with pytest.raises(HTTPException) as exc:
        await confirm_plan_item("dcp-1", PlanItemConfirmRequest(status="whatever"),
                                current_user=USER)
    assert exc.value.status_code == 400


async def test_confirm_writes_back_operator(monkeypatch):
    """回写要带上执行人，审计才有意义。"""
    from backend.api.v1.work_flow import PlanItemConfirmRequest, confirm_plan_item

    seen = {}

    class _Provider:
        async def confirm_plan_item(self, plan_item_id, tenant_id, **kw):
            seen.update(plan_item_id=plan_item_id, tenant_id=tenant_id, **kw)
            return True

    monkeypatch.setattr("backend.core.service_plan_provider.get_service_plan_provider",
                        lambda: _Provider())

    out = await confirm_plan_item(
        "dcp-1", PlanItemConfirmRequest(status="done", actual_care_time="08:05"),
        current_user=USER)

    assert out["confirmed"] is True
    assert seen["worker_id"] == "u-supervisor"
    assert seen["actual_care_time"] == "08:05"
