# backend/core/web_search.py
# 联网搜索服务：Tavily（优先，需 API key）+ DuckDuckGo（免费兜底）
# 进程内直调实现（不依赖 MCP）。

import asyncio

import httpx

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)


async def _search_tavily(query: str, max_results: int, api_key: str) -> list[dict]:
    """Tavily 搜索（结构化结果，https://tavily.com）。"""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key":             api_key,
                "query":               query,
                "max_results":         max_results,
                "include_answer":      False,
                "include_raw_content": False,
            },
        )
        resp.raise_for_status()
    return [
        {
            "title":   r.get("title", ""),
            "url":     r.get("url", ""),
            "snippet": r.get("content", "")[:500],
        }
        for r in resp.json().get("results", [])
    ]


async def _search_duckduckgo(query: str, max_results: int) -> list[dict]:
    """DuckDuckGo 搜索（免费，无需 API key）。

    duckduckgo-search 6.x+ 移除了 AsyncDDGS，统一使用同步 DDGS，
    用 asyncio.to_thread 包装避免阻塞事件循环。
    """
    from duckduckgo_search import DDGS

    def _sync_search() -> list[dict]:
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    "title":   r.get("title", ""),
                    "url":     r.get("href", ""),
                    "snippet": r.get("body", "")[:500],
                })
        return results

    return await asyncio.to_thread(_sync_search)


async def web_search(query: str, max_results: int = 5) -> list[dict]:
    """联网搜索：优先 Tavily，失败或未配置 key 时降级 DuckDuckGo。

    Returns:
        结果列表，每项含 title / url / snippet；全部失败时返回空列表。
    """
    settings = get_settings()

    if settings.tavily_api_key:
        try:
            results = await _search_tavily(query, max_results, settings.tavily_api_key)
            logger.info("web_search.tavily_done", hits=len(results))
            return results
        except Exception as e:
            logger.warning("web_search.tavily_failed", error=str(e))

    try:
        results = await _search_duckduckgo(query, max_results)
        logger.info("web_search.ddgs_done", hits=len(results))
        return results
    except Exception as e:
        logger.error("web_search.ddgs_failed", error=str(e))
        return []


def format_search_results(results: list[dict]) -> str:
    """把搜索结果格式化为可注入 Prompt 的文本块。"""
    if not results:
        return "（未检索到相关网络信息）"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r.get('title', '')}\n    来源：{r.get('url', '')}\n    {r.get('snippet', '')}")
    return "\n".join(lines)
