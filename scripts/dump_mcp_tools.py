#!/usr/bin/env python3
# scripts/dump_mcp_tools.py
# 导出所有 MCP Server 的工具定义（含 JSON Schema）到 文档/mcp_tools_schema.json
#
# 用途：把「工具清单」变成机器可读、可 diff 的产物 —— 接口变更时重跑一次即可看到差异。
#
# 用法（后端需在 8000 端口运行）：
#   python scripts/dump_mcp_tools.py
#   python scripts/dump_mcp_tools.py --base http://localhost:8000/mcp --out 文档/mcp_tools_schema.json

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.config import get_settings  # noqa: E402
from backend.mcp.client import list_mcp_tools  # noqa: E402

# 只读工具免鉴权，写工具需要 MCP_ADMIN_TOKEN
SERVERS = [
    {"name": "sop-kb", "cn": "护理知识库检索", "auth": "none"},
    {"name": "service-data", "cn": "照护数据查询", "auth": "none"},
    {"name": "web-search", "cn": "联网搜索", "auth": "none"},
    {"name": "sop-kb-admin", "cn": "知识库管理", "auth": "token"},
    {"name": "alert-ops", "cn": "告警触发与派单", "auth": "token"},
    {"name": "work-flow", "cn": "照护流程", "auth": "token"},
]


async def main() -> int:
    parser = argparse.ArgumentParser(description="导出 MCP 工具定义与 JSON Schema")
    parser.add_argument("--base", default="http://localhost:8000/mcp")
    parser.add_argument("--out", default="文档/mcp_tools_schema.json")
    args = parser.parse_args()

    token = get_settings().mcp_admin_token or ""
    result = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "base_url": args.base,
        "note": "由 scripts/dump_mcp_tools.py 自动生成；工具定义变更后请重新导出并提交。",
        "servers": {},
    }

    total = 0
    for server in SERVERS:
        url = f"{args.base}/{server['name']}"
        use_token = token if server["auth"] == "token" else ""
        try:
            tools = await list_mcp_tools(url, token=use_token)
        except Exception as e:
            print(f"❌ {server['cn']}（{server['name']}）导出失败：{e}")
            return 1
        result["servers"][server["name"]] = {
            "cn_name": server["cn"],
            "endpoint": f"{url}/mcp",
            "auth": "Bearer Token" if server["auth"] == "token" else "无（只读）",
            "tool_count": len(tools),
            "tools": tools,
        }
        total += len(tools)
        print(f"✅ {server['cn']}（{server['name']}）：{len(tools)} 个工具 "
              f"→ {[t['name'] for t in tools]}")

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = Path(__file__).parent.parent / out_path
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n共 {len(SERVERS)} 个 Server / {total} 个工具 → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
