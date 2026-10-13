# backend/core/health.py
# 依赖探活：/health 是存活探针（很便宜、只看进程），/health/ready 是就绪探针
# （真去问一遍 DB / Redis / Milvus）。
#
# 为什么要分开：
#   原来 /health 恒返回 ok，DB 挂了它也说 ok —— 容器编排会继续往这个实例打流量，
#   运维也看不到真实状态。但把依赖探测塞进 /health 又会让重启策略误判
#   （数据库短暂抖动不该重启应用进程），所以拆成两个是标准做法。
#
# required 语义：
#   required=True  —— 依赖不可用会导致主链路不可用（PostgreSQL、Milvus 向量库）
#   required=False —— 设计上允许缺失（Redis 缓存是 fail-open）

from __future__ import annotations

import asyncio
import time

from backend.core.logger import get_logger

logger = get_logger(__name__)

# 单依赖探测超时：就绪探针不能被一个卡住的依赖拖死
_PROBE_TIMEOUT = 2.0


async def _probe_postgres() -> dict:
    from sqlalchemy import text

    from backend.dependencies import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        await session.execute(text("SELECT 1"))
    return {"ok": True, "required": True}


async def _probe_redis() -> dict:
    from backend.core.cache import get_cache

    ok = await get_cache().ping()
    return {"ok": bool(ok), "required": False,
            "note": "缓存为 fail-open：不可用时自动旁路，不影响主链路" if not ok else ""}


async def _probe_milvus() -> dict:
    from backend.core.knowledge_base import COLLECTION_NAME, KnowledgeBaseClient

    def _sync_check() -> dict:
        client = KnowledgeBaseClient()._client
        collections = list(client.list_collections())
        return {"ok": True, "required": True,
                "collection": COLLECTION_NAME,
                "collection_exists": COLLECTION_NAME in collections,
                "collections": collections}

    return await asyncio.to_thread(_sync_check)


_PROBES = {
    "postgres": _probe_postgres,
    "redis": _probe_redis,
    "milvus": _probe_milvus,
}


async def _run_probe(name: str, probe) -> tuple[str, dict]:
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(probe(), timeout=_PROBE_TIMEOUT)
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return name, result
    except asyncio.TimeoutError:
        return name, {"ok": False, "required": name != "redis",
                      "error": f"探测超时（>{_PROBE_TIMEOUT}s）"}
    except Exception as e:  # noqa: BLE001
        # 探活失败的原因要能看出来（连接拒绝 / 认证失败 / 集合不存在）
        return name, {"ok": False, "required": name != "redis",
                      "error": f"{type(e).__name__}: {str(e)[:160]}"}


async def collect_dependencies() -> dict:
    """并发探测所有依赖，返回 {name: {ok, required, ...}}。"""
    pairs = await asyncio.gather(
        *(_run_probe(name, probe) for name, probe in _PROBES.items())
    )
    return dict(pairs)


def summarize(dependencies: dict) -> tuple[str, bool]:
    """汇总就绪状态：返回 (status, ready)。

    只有 required 依赖全绿才算 ready；Redis 这类可缺失依赖不参与判定，
    但会在 dependencies 里如实标出 ok/note，不会假装它是好的。
    """
    required_ok = all(d.get("ok") for d in dependencies.values() if d.get("required"))
    all_ok = all(d.get("ok") for d in dependencies.values())
    if required_ok and all_ok:
        return "ok", True
    if required_ok:
        return "degraded", True
    return "unavailable", False


async def readiness() -> tuple[dict, bool]:
    """就绪探针的完整结果（供 /health/ready 使用）。"""
    dependencies = await collect_dependencies()
    status, ready = summarize(dependencies)
    if not ready:
        logger.warning("health.not_ready", status=status,
                       failed=[k for k, v in dependencies.items() if not v.get("ok")])
    return {"status": status, "dependencies": dependencies}, ready
