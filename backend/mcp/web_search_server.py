# backend/mcp/web_search_server.py
# 联网搜索 MCP Server（只读）：Tavily → DuckDuckGo 兜底
#
# 端点：挂载在 /mcp/web-search（POST /mcp/web-search/mcp）
# 独立运行：python -m backend.mcp.web_search_server（默认 8013 端口）

from mcp.server.fastmcp import FastMCP

from backend.core.logger import get_logger

logger = get_logger(__name__)

mcp = FastMCP(
    name="FieldOps-WebSearch",
    instructions=(
        "FieldOps 作业能力工具 · 联网搜索（v1.0，只读，免鉴权）。"
        "工具：web_search（Tavily 优先，未配置或失败时降级 DuckDuckGo），用于知识库覆盖不到时的补充检索。"
    ),
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
async def web_search(query: str, max_results: int = 5) -> list[dict]:
    """联网搜索（作业规范 知识库覆盖不到时补充互联网信息）。

    自动选择可用后端：配置了 TAVILY_API_KEY 时优先 Tavily（结果更结构化），
    否则/失败时降级到免费的 DuckDuckGo。

    Args:
        query:       搜索关键词，中文或英文
        max_results: 返回条数，默认 5

    Returns:
        结果列表，每项含 title / url / snippet / content
    """
    from backend.core.web_search import web_search as _web_search

    results = await _web_search(query, max_results=max_results)
    logger.info("mcp.web_search.done", query_preview=query[:50], hits=len(results or []))
    return results or []


if __name__ == "__main__":
    import uvicorn

    port = 8013
    print(f"联网搜索 MCP Server → http://localhost:{port}/mcp")
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=port)
