# tests/test_work_flow_mcp_tools.py
# 照护流程 MCP Server 的工具注册契约（依赖 mcp 包，缺包时整文件跳过）。

import pytest

pytest.importorskip("mcp")  # 本机未装 mcp 时跳过，不影响其他测试

EXPECTED_TOOLS = {
    "start_work_flow",
    "send_discharge_order",
    "get_work_flow_status",
    "get_work_flow_report",
}


@pytest.mark.asyncio
async def test_work_flow_mcp_exposes_expected_tools():
    """/mcp/work-flow 必须暴露 4 个约定工具。"""
    from backend.mcp.work_flow_server import mcp

    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert EXPECTED_TOOLS <= names, f"缺少工具: {EXPECTED_TOOLS - names}"
