# tests/test_web_search.py
# web_search 服务单元测试（mock httpx，不依赖外网）
#
# 注意 settings 的打补丁方式：必须替换模块属性 ws.get_settings 本身，
# 不能写 setattr(ws.get_settings, "__call__", ...) —— 对普通函数对象设 __call__
# 不改变调用行为（函数调用走类型层的 tp_call），补丁等于没打，
# 结果就是"本机恰好配了 TAVILY_API_KEY 才过、别人机器上全挂"。

import pytest

from backend.core.web_search import format_search_results


def _stub_settings(tavily_api_key: str):
    """构造一个只带 tavily_api_key 的 settings 替身。"""
    return lambda: type("S", (), {"tavily_api_key": tavily_api_key})()


def test_format_search_results_empty():
    assert format_search_results([]) == "（未检索到相关网络信息）"


def test_format_search_results():
    results = [
        {"title": "跌倒预防指南", "url": "https://example.com/a", "snippet": "老年人跌倒预防要点"},
        {"title": "压疮护理", "url": "https://example.com/b", "snippet": "每2小时翻身"},
    ]
    text = format_search_results(results)
    assert "[1] 跌倒预防指南" in text
    assert "https://example.com/b" in text


@pytest.mark.asyncio
async def test_web_search_tavily_success(monkeypatch):
    """Tavily 正常返回：走 Tavily 分支。"""
    from backend.core import web_search as ws

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [
                {"title": "t1", "url": "u1", "content": "c1"},
                {"title": "t2", "url": "u2", "content": "c2"},
            ]}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            assert "tavily.com" in url
            return _FakeResp()

    monkeypatch.setattr(ws.httpx, "AsyncClient", lambda **kw: _FakeClient())
    # 替换模块属性本身，保证走 Tavily 分支（不依赖环境变量）
    monkeypatch.setattr(ws, "get_settings", _stub_settings("test-key"))

    results = await ws.web_search("老年人跌倒预防")
    assert len(results) == 2
    assert results[0] == {"title": "t1", "url": "u1", "snippet": "c1"}


@pytest.mark.asyncio
async def test_web_search_fallback_to_ddg(monkeypatch):
    """Tavily 失败 → 降级 DuckDuckGo。"""
    from backend.core import web_search as ws

    async def _boom_tavily(*args, **kwargs):
        raise RuntimeError("tavily down")

    async def _fake_ddg(query, max_results):
        return [{"title": "ddg", "url": "u", "snippet": "s"}]

    monkeypatch.setattr(ws, "_search_tavily", _boom_tavily)
    monkeypatch.setattr(ws, "_search_duckduckgo", _fake_ddg)
    monkeypatch.setattr(ws, "get_settings", _stub_settings("test-key"))

    results = await ws.web_search("test")
    assert results == [{"title": "ddg", "url": "u", "snippet": "s"}]


@pytest.mark.asyncio
async def test_web_search_all_failed_returns_empty(monkeypatch):
    """两个引擎都失败 → 返回空列表（节点兜底为空结果）。"""
    from backend.core import web_search as ws

    async def _boom(*args, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(ws, "_search_tavily", _boom)
    monkeypatch.setattr(ws, "_search_duckduckgo", _boom)
    monkeypatch.setattr(ws, "get_settings", _stub_settings(""))

    assert await ws.web_search("test") == []


@pytest.mark.asyncio
async def test_web_search_without_tavily_key_skips_tavily(monkeypatch):
    """没配 Tavily Key 时应直接走 DuckDuckGo——不能因为环境缺 Key 就去请求 Tavily。"""
    from backend.core import web_search as ws

    called = {"tavily": False}

    async def _tavily(*args, **kwargs):
        called["tavily"] = True
        return [{"title": "不该走到这里", "url": "", "snippet": ""}]

    async def _ddg(query, max_results):
        return [{"title": "ddg", "url": "u", "snippet": "s"}]

    monkeypatch.setattr(ws, "_search_tavily", _tavily)
    monkeypatch.setattr(ws, "_search_duckduckgo", _ddg)
    monkeypatch.setattr(ws, "get_settings", _stub_settings(""))

    results = await ws.web_search("test")

    assert called["tavily"] is False
    assert results == [{"title": "ddg", "url": "u", "snippet": "s"}]
