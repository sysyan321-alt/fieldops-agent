# backend/mcp/client.py
# MCP 客户端：以原生 JSON-RPC 调用 stateless MCP Server 的工具
#
# 为什么不用官方 SDK 的会话对象：本项目的 MCP Server 都是 stateless_http=True，
# 每次 POST 完全自包含、无需 initialize 握手，直接用 httpx 发 tools/call 最省事，
# 也避免为一次工具调用维护长连接会话。

import json
from typing import Any

import httpx

from backend.core.logger import get_logger

logger = get_logger(__name__)


async def call_mcp_tool(
    server_url: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout: float = 30.0,
    token: str = "",
) -> Any:
    """调用 MCP Server 的单个工具。

    Args:
        server_url: MCP Server 基础 URL，如 "http://localhost:8000/mcp/sop-kb"
                    （工具实际端点是 {server_url}/mcp）
        tool_name:  工具名，与 @mcp.tool() 注册名一致
        arguments:  工具参数字典
        timeout:    超时秒数
        token:      可选 Bearer token（写工具/受保护 Server 用）

    Returns:
        工具返回值（JSON 反序列化后的 Python 对象）
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }

    # json_response=True 的 Server 要求客户端声明 Accept: application/json，
    # 否则返回 -32600 Not Acceptable
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # trust_env=False：禁止 httpx 读取 HTTP_PROXY/ALL_PROXY 等环境变量与系统代理。
    # macOS 系统代理或抓包工具不会排除 localhost，会让本机 MCP 调用被代理成 502。
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        resp = await client.post(f"{server_url}/mcp", json=payload, headers=headers)
        resp.raise_for_status()

    data = resp.json()
    if "error" in data:
        raise ValueError(
            f"MCP tool '{tool_name}' error: "
            f"{data['error'].get('message', str(data['error']))}"
        )

    # FastMCP 对 list[dict] 的序列化行为有两种：
    #   1) 每个 dict 单独放进一个 TextContent
    #   2) 整个列表序列化进一个 TextContent
    # 所以必须遍历全部 content，不能只取 content[0]，否则多条结果只返回第一条。
    result = data.get("result", {})
    content = result.get("content", [])

    # 工具内部抛异常时 FastMCP 走 isError 通道（不是 JSON-RPC error 信封），
    # 必须显式抛错，否则调用方会把错误文本当成正常数据。
    if result.get("isError"):
        message = " ".join(
            item.get("text", "") for item in content if isinstance(item, dict)
        ).strip()
        raise ValueError(f"MCP tool '{tool_name}' failed: {message[:500]}")

    if not content:
        return []

    items: list[Any] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text", "")
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            parsed = text
        if isinstance(parsed, list):
            # 整个列表序列化在一个 TextContent 里
            return parsed
        items.append(parsed)

    if not items:
        return content
    # 只有一个结果（dict / 标量）时直接返回它本身，避免调用方拿到 [ {...} ] 这种包装
    return items[0] if len(items) == 1 else items


async def list_mcp_tools(server_url: str, timeout: float = 10.0, token: str = "") -> list[dict]:
    """列出 MCP Server 注册的全部工具（校验 / 调试用）。

    token 用于带门禁的写工具 Server（Authorization: Bearer <MCP_ADMIN_TOKEN>）。
    """
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        resp = await client.post(f"{server_url}/mcp", json=payload, headers=headers)
        resp.raise_for_status()

    return resp.json().get("result", {}).get("tools", [])
