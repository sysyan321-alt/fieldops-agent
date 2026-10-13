# tests/test_checkpoint.py
# 检查点（checkpointer）测试：跨实例持久化、按 Agent 命名空间隔离、整线程删除。
#
# 为什么单独测这几件事：
#   1. 持久化是 interrupt / resume 的前提——断点存不住，"站长确认后派单"就会静默失败；
#   2. 统一对话入口下多个 Agent 共用同一个 session_id，命名空间不隔离会互相覆盖；
#   3. 删除是破坏性操作（连审计期内的断点一起清），行为必须被钉死。

import uuid

import pytest

from backend.config import get_settings
from backend.core.memory import (
    active_backend,
    build_config,
    build_thread_id,
    close_memory_savers,
    delete_thread_state,
    get_memory_saver,
)


def _checkpoint() -> dict:
    """构造一个最小可用的 checkpoint（LangGraph 的 ChannelValues 形状）。"""
    return {
        "v": 4,
        "id": f"1e{uuid.uuid4().hex[:20]}",
        "ts": "2026-09-29T10:00:00+00:00",
        "channel_values": {"messages": []},
        "channel_versions": {"messages": "00000000000000000000000000000001.0.1"},
        "versions_seen": {},
        "pending_sends": [],
    }


@pytest.fixture(autouse=True)
async def _reset_saver():
    """每个用例前后都关掉连接池：psycopg 的池绑定事件循环，pytest-asyncio 每例一个 loop。"""
    await close_memory_savers()
    yield
    await close_memory_savers()


async def test_postgres_backend_used_by_default(db_ready):
    """默认配置下检查点落在 PostgreSQL，而不是进程内内存。"""
    saver = await get_memory_saver("alert")
    assert active_backend() == "postgres"
    assert saver is not None


async def test_checkpoint_survives_new_saver_instance(db_ready):
    """写入后关掉连接池重建 saver，仍然读得到——"跨进程/跨重启存活"的最小证据。"""
    thread_id = build_thread_id("pytest-ckpt", uuid.uuid4().hex[:8])
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": "alert"}}

    saver = await get_memory_saver("alert")
    await saver.aput(config, _checkpoint(), {"source": "input", "step": 0}, {})
    first = await saver.aget_tuple(config)
    assert first is not None

    await close_memory_savers()                      # 模拟进程结束
    saver2 = await get_memory_saver("alert")         # 新进程 / 新连接池
    second = await saver2.aget_tuple(config)
    assert second is not None, "检查点没有持久化到 PostgreSQL"
    assert second.checkpoint["id"] == first.checkpoint["id"]

    await delete_thread_state(thread_id)             # 收尾，避免污染库


async def test_agent_namespaces_are_isolated(db_ready):
    """同一 thread_id 下，不同 Agent 的检查点互不可见（checkpoint_ns 隔离）。"""
    session_id = uuid.uuid4().hex[:8]
    saver = await get_memory_saver("sop_qa")

    sop_config = build_config("pytest-ns", session_id, agent_type="sop_qa")
    cf_config = build_config("pytest-ns", session_id, agent_type="care_flow")
    assert sop_config["configurable"]["thread_id"] == cf_config["configurable"]["thread_id"]
    assert sop_config["configurable"]["checkpoint_ns"] != cf_config["configurable"]["checkpoint_ns"]

    await saver.aput(sop_config, _checkpoint(), {"source": "input", "step": 0}, {})

    assert await saver.aget_tuple(sop_config) is not None
    assert await saver.aget_tuple(cf_config) is None, "另一个 Agent 不该看到这条检查点"

    await delete_thread_state(sop_config["configurable"]["thread_id"])


async def test_delete_thread_removes_all_namespaces(db_ready):
    """删除按 thread_id 整体生效：所有 Agent 命名空间的检查点一起清掉。"""
    session_id = uuid.uuid4().hex[:8]
    saver = await get_memory_saver("sop_qa")
    sop_config = build_config("pytest-del", session_id, agent_type="sop_qa")
    cf_config = build_config("pytest-del", session_id, agent_type="care_flow")
    thread_id = sop_config["configurable"]["thread_id"]

    await saver.aput(sop_config, _checkpoint(), {"source": "input", "step": 0}, {})
    await saver.aput(cf_config, _checkpoint(), {"source": "input", "step": 0}, {})

    result = await delete_thread_state(thread_id)
    assert result["deleted"] is True

    assert await saver.aget_tuple(sop_config) is None
    assert await saver.aget_tuple(cf_config) is None


async def test_delete_thread_is_idempotent(db_ready):
    """删一个不存在的线程不报错（重复调用 / 已清理过）。"""
    result = await delete_thread_state(build_thread_id("pytest-none", uuid.uuid4().hex[:8]))
    assert result["thread_id"]


async def test_memory_fallback_backend(monkeypatch):
    """checkpoint_backend=memory 时不碰数据库（单测 / 离线环境可用）。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "checkpoint_backend", "memory", raising=False)
    await close_memory_savers()

    saver = await get_memory_saver("sop_qa")
    assert active_backend() == "memory"
    assert saver is not None

    session_id = uuid.uuid4().hex[:8]
    config = build_config("pytest-mem", session_id, agent_type="sop_qa")
    await saver.aput(config, _checkpoint(), {"source": "input", "step": 0}, {})
    assert await saver.aget_tuple(config) is not None

    result = await delete_thread_state(config["configurable"]["thread_id"])
    assert result["backend"] == "memory"
    assert await saver.aget_tuple(config) is None
