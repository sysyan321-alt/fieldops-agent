# tests/test_care_plan_mcp_tools.py
# 照护计划相关 MCP 工具的契约：
#   只读工具（服务项 / 等级配置 / 当日计划）在 care-data，不需要 Token；
#   回写工具（确认执行）在 care-flow，必须过租户白名单。
#
# 这组用例守住的是"能力开放但不越权"：
#   外部系统能查业务配置、能回报执行结果，但不能通过 MCP 改业务规则。

import pytest

from backend.mcp import care_data_server, care_flow_server


# ── ① 只读工具（care-data）──────────────────────────────────

def test_care_data_exposes_plan_read_tools():
    """三个只读工具都要注册出来（外部系统靠工具名发现能力）。"""
    names = set(care_data_server.mcp._tool_manager._tools)
    assert {"list_elder_service_items", "list_care_level_configs",
            "get_today_care_plan"} <= names


async def test_service_items_tool_shape(monkeypatch):
    async def _items(elder_id, tenant_id="tenant_default"):
        return [{"project_name": "协助翻身", "frequency_name": "每2小时",
                 "is_required": True, "care_level": "介助"}]

    monkeypatch.setattr("backend.db.repositories.list_service_items", _items)

    out = await care_data_server.list_elder_service_items("e001", "tenant_default")

    assert out["found"] is True
    assert out["items"][0]["project_name"] == "协助翻身"


async def test_service_items_tool_reports_missing_as_empty(monkeypatch):
    """没有服务项要如实返回 found=False + 空数组，而不是报错或编数据。"""
    async def _empty(elder_id, tenant_id="tenant_default"):
        return []

    monkeypatch.setattr("backend.db.repositories.list_service_items", _empty)

    out = await care_data_server.list_elder_service_items("e-none", "tenant_default")

    assert out["found"] is False
    assert out["items"] == []


async def test_level_configs_tool_passes_required_only(monkeypatch):
    seen = {}

    async def _configs(care_level, tenant_id="tenant_default", required_only=False):
        seen.update(care_level=care_level, required_only=required_only)
        return [{"project_name": "协助进食"}]

    monkeypatch.setattr("backend.db.repositories.list_level_configs", _configs)

    out = await care_data_server.list_care_level_configs("介护", "tenant_default",
                                                         required_only=True)

    assert seen == {"care_level": "介护", "required_only": True}
    assert out == [{"project_name": "协助进食"}]


async def test_today_plan_tool_returns_progress(monkeypatch):
    async def _plan(elder_id, tenant_id):
        return [{"plan_item_id": "dcp-1", "project_name": "巡视", "status": "done"},
                {"plan_item_id": "dcp-2", "project_name": "翻身", "status": "planned"}]

    class _Provider:
        async def today_plan(self, elder_id, tenant_id):
            return await _plan(elder_id, tenant_id)

    monkeypatch.setattr("backend.core.care_plan_provider.get_care_plan_provider",
                        lambda: _Provider())

    out = await care_data_server.get_today_care_plan("e001", "tenant_default")

    assert out["progress"] == {"total": 2, "done": 1, "missed": 0, "pending": 1}
    assert len(out["plan"]) == 2


# ── ② 回写工具（care-flow，需 Token + 白名单）─────────────────

def test_care_flow_exposes_confirm_tool():
    names = set(care_flow_server.mcp._tool_manager._tools)
    assert "confirm_care_plan_item" in names


async def test_confirm_tool_requires_whitelisted_tenant(monkeypatch):
    """写工具必须先过租户白名单——这是 MCP 写能力的既有门禁，不能绕过。"""
    def _deny(tenant_id):
        raise PermissionError(f"tenant {tenant_id} 不在白名单")

    monkeypatch.setattr(care_flow_server, "assert_tenant_allowed", _deny)

    with pytest.raises(PermissionError):
        await care_flow_server.confirm_care_plan_item("dcp-1", "tenant_evil")


async def test_confirm_tool_rejects_bad_status(monkeypatch):
    """状态只允许 done/missed，避免外部系统写进语义不明的值。"""
    monkeypatch.setattr(care_flow_server, "assert_tenant_allowed", lambda tenant_id: None)

    out = await care_flow_server.confirm_care_plan_item("dcp-1", "tenant_default",
                                                        status="whatever")

    assert out["confirmed"] is False
    assert "done 或 missed" in out["message"]


async def test_confirm_tool_calls_provider(monkeypatch):
    monkeypatch.setattr(care_flow_server, "assert_tenant_allowed", lambda tenant_id: None)
    seen = {}

    class _Provider:
        async def confirm_plan_item(self, plan_item_id, tenant_id, **kw):
            seen.update(plan_item_id=plan_item_id, tenant_id=tenant_id, **kw)
            return True

    monkeypatch.setattr("backend.core.care_plan_provider.get_care_plan_provider",
                        lambda: _Provider())

    out = await care_flow_server.confirm_care_plan_item(
        "dcp-1", "tenant_default", status="done",
        carer_name="张三", actual_care_time="08:05")

    assert out["confirmed"] is True
    assert seen["plan_item_id"] == "dcp-1"
    assert seen["status"] == "done"
    assert seen["carer_name"] == "张三"


async def test_confirm_tool_reports_not_found(monkeypatch):
    """条目不存在（或跨租户）时返回 confirmed=False，不抛异常。"""
    monkeypatch.setattr(care_flow_server, "assert_tenant_allowed", lambda tenant_id: None)

    class _Provider:
        async def confirm_plan_item(self, *_a, **_kw):
            return False

    monkeypatch.setattr("backend.core.care_plan_provider.get_care_plan_provider",
                        lambda: _Provider())

    out = await care_flow_server.confirm_care_plan_item("dcp-none", "tenant_default")

    assert out["confirmed"] is False
    assert "不存在" in out["message"]
