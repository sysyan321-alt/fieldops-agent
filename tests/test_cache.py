# tests/test_cache.py
# Redis 缓存层测试：key 构造规则 + 真实读写与租户隔离（Redis 不可用时自动 skip）

import pytest

from backend.core.cache import get_cache, hash_text, make_key


def test_hash_text_normalizes_whitespace_and_case():
    """同一问题的大小写/空白差异应命中同一 key。"""
    assert hash_text("配电间 如何巡检") == hash_text("  配电间   如何巡检 ")
    assert hash_text("Confined Space") == hash_text("confined space")


def test_hash_text_stable_and_short():
    h = hash_text("高处作业怎么做")
    assert len(h) == 16 and h == hash_text("高处作业怎么做")
    assert h != hash_text("设备温升怎么控制")


def test_make_key_skips_empty_parts_and_prefixes():
    key = make_key("retrieval", "tenant_default", None, "r10", "k3", "abc123")
    assert key == "fieldops:retrieval:tenant_default:r10:k3:abc123"


@pytest.mark.asyncio
async def test_cache_roundtrip_and_tenant_isolation():
    """真实 Redis 读写 + 租户维度不串味；Redis 不可用则跳过。"""
    cache = get_cache()
    if not await cache.ping():
        pytest.skip("Redis 不可用（缓存层为 fail-open，设计上允许缺失）")

    key_a = make_key("retrieval", "tenant_a", "r10", "k3", "hash-x")
    key_b = make_key("retrieval", "tenant_b", "r10", "k3", "hash-x")
    payload = {"confidence": 0.91, "docs": [{"content": "A 租户内容"}]}

    try:
        assert await cache.set_json(key_a, payload, ttl_seconds=30)
        assert await cache.get_json(key_a) == payload
        # 另一租户同 query 不应命中
        assert await cache.get_json(key_b) is None
    finally:
        await cache.delete_prefix(make_key("retrieval", "tenant_a"))
        await cache.delete_prefix(make_key("retrieval", "tenant_b"))


@pytest.mark.asyncio
async def test_cache_fail_open_when_disabled(monkeypatch):
    """缓存关闭时读写都应当静默降级，不抛异常。"""
    from backend.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "cache_enabled", False, raising=False)
    cache = get_cache()
    cache._client = None
    cache._down_until = 0.0

    assert await cache.get_json("fieldops:test:disabled") is None
    assert await cache.set_json("fieldops:test:disabled", {"a": 1}, ttl_seconds=10) is False
    assert await cache.ping() is False
