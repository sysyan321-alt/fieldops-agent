# backend/mcp/__init__.py
# MCP（Model Context Protocol）能力层
#
# 对外：把护理能力（SOP 检索、照护数据查询）暴露成标准 MCP 工具，供外部 Agent / 系统调用
# 对内：backend/mcp/client.py 提供调用外部 MCP Server 的轻量客户端
