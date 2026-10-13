# tests/conftest.py
# pytest 公共夹具

import asyncio
import os
import sys
import uuid

import pytest
import pytest_asyncio

# 项目根目录加入 sys.path（care_agent/）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 测试用唯一 ID 前缀（避免污染/冲突，用例结束统一清理）
_TEST_PREFIX = "pytest-"


def make_id(name: str) -> str:
    return f"{_TEST_PREFIX}{name}-{uuid.uuid4().hex[:8]}"


def is_test_id(value: str) -> bool:
    return value.startswith(_TEST_PREFIX)


async def _db_available() -> bool:
    """检查 PG 是否可连（docker 未启动时自动跳过 DB 类用例）。"""
    try:
        from sqlalchemy import text
        from backend.dependencies import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest_asyncio.fixture(scope="function")
async def db_ready():
    """确认 DB 可用并执行迁移，不可用则跳过所有依赖它的用例。

    注意：必须是函数级（不能 session 级）。pytest-asyncio 默认每个用例独立事件循环，
    session 级 fixture 建立的 asyncpg 连接池跨循环复用会直接失效。
    迁移全部是幂等 DDL，函数级重复执行开销可忽略。
    """
    if not await _db_available():
        pytest.skip("PostgreSQL 不可用（docker-compose up -d postgres 后重跑）")
    from backend.db.migrations import run_migrations
    await run_migrations()
    # 生产默认不播种演示账号（SEED_DEMO_USERS=false），但测试需要固定账号做登录断言，
    # 所以这里显式播种——这是测试夹具的决定，不代表应用默认行为。
    from backend.db.repositories import ensure_default_users
    await ensure_default_users()
    return True


def _local_weights_present() -> bool:
    """本地是否有 BGE 嵌入/精排权重（没有的话加载会走 HuggingFace 在线拉取）。"""
    root = os.path.join(PROJECT_ROOT, "backend", "models")
    for sub in ("embedding/bge-m3", "reranker/bge-reranker-large"):
        path = os.path.join(root, sub)
        if not os.path.isdir(path):
            return False
        if not any(f.endswith((".bin", ".safetensors")) for f in os.listdir(path)):
            return False
    return True


@pytest.fixture(scope="function")
def models_ready():
    """需要真实嵌入/精排模型的用例用它：没权重就跳过，而不是触发 2GB 在线下载。

    背景：曾经有个"只用内存检查点、打桩 LLM"的用例因为 monkeypatch 打错了名字，
    悄悄走进了检索链路——本机有模型时只慢一秒，别人机器上却会去 HuggingFace
    下载整个 bge-m3。测试套件不应该依赖作者本机的模型权重。
    """
    if not _local_weights_present():
        pytest.skip("本地无 BGE 模型权重（放入 backend/models/ 后重跑）")
    return True


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engine():
    """每个用例结束后清空 asyncpg 连接池。

    pytest-asyncio 每个用例一个独立事件循环，池里的连接绑定在已关闭的 loop 上，
    下个用例复用会随机失败。dispose 后各用例在自己 loop 里重建连接。
    """
    yield
    try:
        from backend.dependencies import engine
        await engine.dispose()
    except Exception:
        pass
