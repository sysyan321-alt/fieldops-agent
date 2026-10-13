# tests/test_work_flow_discharge.py
# 退住工单投递：只写检查点、不跑图；租户/状态守卫与 thread_id 拼键一致性。
# 不依赖真实 LLM / DB / MCP——只验证 deliver_discharge_order 的规则与键构造。

from unittest.mock import AsyncMock

import pytest

from backend.agents.work_flow import discharge as discharge_mod
from backend.core.memory import build_thread_id


def _patch_session(monkeypatch, row):
    monkeypatch.setattr(discharge_mod, "get_work_flow_session", AsyncMock(return_value=row))


@pytest.mark.asyncio
async def test_deliver_rejects_empty_order():
    """空 dict 工单没有意义：直接抛错，不静默忽略。"""
    with pytest.raises(ValueError):
        await discharge_mod.deliver_discharge_order("cf-x", "tenant_default", {})


@pytest.mark.asyncio
async def test_deliver_not_found(monkeypatch):
    """会话不存在 / 跨租户 → found=False，不写检查点。"""
    _patch_session(monkeypatch, None)
    out = await discharge_mod.deliver_discharge_order(
        "cf-x", "tenant_default", {"order_type": "discharge"})
    assert out["found"] is False
    assert out["status"] == "not_found"


@pytest.mark.asyncio
async def test_deliver_rejects_finished_session(monkeypatch):
    """已结束的会话不再接收工单（要下发需开新会话）。"""
    _patch_session(monkeypatch, {"session_id": "cf-x", "site_id": "s001", "status": "finished"})
    out = await discharge_mod.deliver_discharge_order(
        "cf-x", "tenant_default", {"order_type": "discharge"})
    assert out["found"] is False
    assert out["status"] == "already_finished"


@pytest.mark.asyncio
async def test_deliver_writes_checkpoint_with_consistent_thread_key(monkeypatch):
    """正常投递：只调 aupdate_state 写 discharge_order，thread_id / checkpoint_ns 与编排器一致。"""
    _patch_session(monkeypatch, {
        "session_id": "cf-abc", "site_id": "s001",
        "status": "in_progress", "current_stage": "daily_care",
    })

    captured: dict = {}

    class _FakeGraph:
        async def aupdate_state(self, config, values, **kwargs):
            captured["config"] = config
            captured["values"] = values

        async def ainvoke(self, *args, **kwargs):  # 不应被调用
            raise AssertionError("投递工单不应推进对话（不该 ainvoke）")

    async def _fake_build():
        return _FakeGraph()

    import backend.agents.work_flow.graph as graph_mod
    monkeypatch.setattr(graph_mod, "build_work_flow_graph", _fake_build)

    order = {"order_type": "discharge", "reason": "转院"}
    out = await discharge_mod.deliver_discharge_order("cf-abc", "tenant_default", order)

    assert out["found"] is True
    assert out["status"] == "delivered"
    # 只写一个字段
    assert captured["values"] == {"discharge_order": order}
    # 键必须与 Orchestrator 的 build_config(site_id, session_id, agent_type="work_flow") 一致
    cfg = captured["config"]["configurable"]
    assert cfg["thread_id"] == build_thread_id("s001", "cf-abc")
    assert cfg["checkpoint_ns"] == "work_flow"


@pytest.mark.asyncio
async def test_deliver_returns_error_when_write_fails(monkeypatch):
    """检查点写入异常 → found=False + status=error（不把异常抛给调用方）。"""
    _patch_session(monkeypatch, {"session_id": "cf-x", "site_id": "s001", "status": "in_progress"})

    class _BoomGraph:
        async def aupdate_state(self, *args, **kwargs):
            raise RuntimeError("checkpointer down")

    async def _fake_build():
        return _BoomGraph()

    import backend.agents.work_flow.graph as graph_mod
    monkeypatch.setattr(graph_mod, "build_work_flow_graph", _fake_build)

    out = await discharge_mod.deliver_discharge_order(
        "cf-x", "tenant_default", {"order_type": "discharge"})
    assert out["found"] is False
    assert out["status"] == "error"
