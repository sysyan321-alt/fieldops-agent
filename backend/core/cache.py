# backend/core/cache.py
# Redis 缓存层：可用则用，不可用静默降级（fail-open）
#
# 设计原则（缓存是加速项，不是正确性依赖）：
#   1. fail-open：Redis 连接失败/超时一律当"未命中"，业务继续走原路径
#   2. 短连接超时 + 失败冷却：连接不可用时 60s 内不再尝试，避免每个请求都等超时
#   3. 只在结果有效时写缓存：空结果不缓存，保证"导入新 SOP 后立刻能查到"
#   4. key 里带租户维度：缓存不能成为绕过租户隔离的旁路

import hashlib
import json
import time
from typing import Any, Optional

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)

_KEY_PREFIX = "fieldops"
_FAILURE_COOLDOWN_SECONDS = 60


def hash_text(text: str) -> str:
    """查询文本 → 稳定短哈希（同一问题归一化后命中同一 key）。"""
    normalized = " ".join((text or "").strip().lower().split())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def make_key(*parts: Any) -> str:
    """拼缓存 key：fieldops:<段1>:<段2>..."""
    return ":".join([_KEY_PREFIX, *[str(p) for p in parts if p not in (None, "")]])


class CacheClient:
    """异步 Redis 缓存客户端（进程内单例）。"""

    def __init__(self) -> None:
        self._client = None
        self._down_until = 0.0
        self._warned = False

    def _enabled(self) -> bool:
        return bool(get_settings().cache_enabled)

    async def _get_client(self):
        """惰性创建客户端；处于失败冷却期时直接返回 None。"""
        if not self._enabled():
            return None
        if self._client is not None and time.time() < self._down_until:
            return None
        if self._client is None:
            import redis.asyncio as aioredis

            s = get_settings()
            self._client = aioredis.Redis(
                host=s.redis_host,
                port=s.redis_port,
                db=s.redis_db,
                decode_responses=True,
                socket_connect_timeout=0.5,   # 缓存不可用时不能拖慢主链路
                socket_timeout=0.5,
            )
        return self._client

    def _mark_down(self, error: Exception, op: str) -> None:
        self._down_until = time.time() + _FAILURE_COOLDOWN_SECONDS
        if not self._warned:
            logger.warning("cache.unavailable", op=op, error=str(error)[:120],
                           cooldown_seconds=_FAILURE_COOLDOWN_SECONDS)
            self._warned = True

    async def get_json(self, key: str) -> Optional[Any]:
        """读缓存；未命中/不可用/解析失败都返回 None。"""
        client = await self._get_client()
        if client is None:
            return None
        try:
            raw = await client.get(key)
        except Exception as e:
            self._mark_down(e, "get")
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    async def set_json(self, key: str, value: Any, ttl_seconds: int) -> bool:
        """写缓存；失败只记日志不影响业务。"""
        client = await self._get_client()
        if client is None:
            return False
        try:
            await client.set(key, json.dumps(value, ensure_ascii=False), ex=ttl_seconds)
            return True
        except Exception as e:
            self._mark_down(e, "set")
            return False

    async def delete_prefix(self, prefix: str) -> int:
        """按前缀清理缓存（如知识库更新后失效检索缓存）。"""
        client = await self._get_client()
        if client is None:
            return 0
        try:
            deleted = 0
            async for key in client.scan_iter(match=f"{prefix}*", count=200):
                await client.delete(key)
                deleted += 1
            return deleted
        except Exception as e:
            self._mark_down(e, "delete_prefix")
            return 0

    async def ping(self) -> bool:
        """健康检查：Redis 是否可用且缓存已启用。"""
        client = await self._get_client()
        if client is None:
            return False
        try:
            return bool(await client.ping())
        except Exception as e:
            self._mark_down(e, "ping")
            return False


_cache: Optional[CacheClient] = None


def get_cache() -> CacheClient:
    """获取全局缓存客户端。"""
    global _cache
    if _cache is None:
        _cache = CacheClient()
    return _cache
