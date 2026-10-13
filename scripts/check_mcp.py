#!/usr/bin/env python3
# scripts/check_mcp.py
# MCP Server 冒烟校验：列出工具 + 调一次真实工具
#
# 用法（后端已在 8000 端口运行）：
#   python scripts/check_mcp.py                 # 校验全部 server
#   python scripts/check_mcp.py --server sop-kb # 只校验知识库
#   python scripts/check_mcp.py --site-id s001 --tenant-id tenant_default
#
# 也可指向独立运行的 MCP Server：--base http://localhost:8011

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.config import get_settings  # noqa: E402
from backend.mcp.client import call_mcp_tool, list_mcp_tools  # noqa: E402


async def check_sop_kb(base: str, tenant_id: str) -> bool:
    print(f"\n=== SOP 知识库 MCP：{base}/mcp ===")
    tools = await list_mcp_tools(base)
    print("已注册工具：", [t["name"] for t in tools])

    status = await call_mcp_tool(base, "get_kb_status", {"tenant_id": tenant_id})
    print("知识库状态：", status)

    docs = await call_mcp_tool(base, "list_sop_documents", {"tenant_id": tenant_id})
    print(f"文档数：{len(docs)}")
    for d in docs[:3]:
        print(f"  - {d['source_name']}（{d['category']}，{d['chunks']} chunks）")

    # 检索工具会加载 BGE 模型（首次较慢）；集合为空时返回空列表，
    # 模型权重/依赖缺失时（如 scipy 不兼容）只提示，不算整体校验失败
    try:
        hits = await call_mcp_tool(
            base, "search_sop",
            {"query": "鼻饲管日常护理步骤", "tenant_id": tenant_id, "top_k": 3},
            timeout=180.0,
        )
        print(f"检索命中：{len(hits)} 条")
        for h in hits:
            print(f"  - [{h['score']:.3f}] {h['source_name']}：{h['content'][:60]}...")
    except Exception as e:
        print(f"⚠️  检索工具不可用（多为 BGE 模型依赖问题，不影响其他工具）：{e}")
    return True


async def check_service_data(base: str, site_id: str, tenant_id: str) -> bool:
    print(f"\n=== 照护数据 MCP：{base}/mcp ===")
    tools = await list_mcp_tools(base)
    print("已注册工具：", [t["name"] for t in tools])

    profile = await call_mcp_tool(
        base, "get_site_profile", {"site_id": site_id, "tenant_id": tenant_id}
    )
    print("老人档案：", {k: profile.get(k) for k in ("found", "name", "risk_tier", "location")})

    for tool, label in (
        ("get_recent_device_readings", "设备读数"),
        ("get_recent_service_records", "照护记录"),
        ("get_recent_health_indicators", "健康指标"),
    ):
        rows = await call_mcp_tool(
            base, tool, {"site_id": site_id, "tenant_id": tenant_id, "limit": 3}
        )
        print(f"{label}：{len(rows)} 条")

    alerts = await call_mcp_tool(base, "list_alerts", {"tenant_id": tenant_id, "limit": 3})
    print(f"告警列表：{len(alerts)} 条")
    return True


async def check_web_search(base: str) -> bool:
    print(f"\n=== 联网搜索 MCP：{base}/mcp ===")
    tools = await list_mcp_tools(base)
    print("已注册工具：", [t["name"] for t in tools])
    try:
        results = await call_mcp_tool(base, "web_search", {"query": "养老机构跌倒预防", "max_results": 2}, timeout=30.0)
        print(f"搜索结果：{len(results)} 条")
        for r in results[:2]:
            print(f"  - {r.get('title', '')[:50]} | {r.get('url', '')[:60]}")
    except Exception as e:
        print(f"⚠️  搜索失败（外网/搜索源问题，不算校验失败）：{e}")
    return True


async def check_write_servers(base: str, token: str, tenant_id: str, site_id: str) -> bool:
    """校验写工具：无 token 必须 401，有 token 才能列工具。"""
    ok = True
    labels = {
        "alert-ops": "告警触发与派单",
        "sop-kb-admin": "知识库管理",
        "work-flow": "作业流程",
    }
    for name in ("alert-ops", "sop-kb-admin", "work-flow"):
        url = f"{base}/{name}"
        print(f"\n=== 写工具 · {labels[name]}（{name}，需 Token）：{url}/mcp ===")
        try:
            await list_mcp_tools(url)
            print("❌ 未带 Token 竟然可访问，鉴权未生效")
            ok = False
        except Exception as e:
            print(f"未带 Token → 已拒绝（预期）：{str(e)[:80]}")
        if not token:
            print("（未提供 --admin-token，跳过带 Token 的校验）")
            continue
        try:
            tools = await list_mcp_tools(url, token=token)
            print("带 Token 已注册工具：", [t["name"] for t in tools])
        except Exception as e:
            print(f"❌ 带 Token 仍失败：{e}")
            ok = False
    return ok


async def main() -> int:
    parser = argparse.ArgumentParser(description="FieldOps MCP Server 冒烟校验")
    parser.add_argument("--server", default="all",
                        choices=["all", "sop-kb", "service-data", "web-search", "write"])
    parser.add_argument("--base", default="http://localhost:8000/mcp")
    parser.add_argument("--tenant-id", default="tenant_default")
    parser.add_argument("--site-id", default="s001")
    parser.add_argument("--admin-token", default="",
                        help="写工具 Bearer Token；留空则用 .env.local 里的 MCP_ADMIN_TOKEN")
    args = parser.parse_args()

    admin_token = args.admin_token or (get_settings().mcp_admin_token or "")

    try:
        if args.server in ("all", "sop-kb"):
            await check_sop_kb(f"{args.base}/sop-kb", args.tenant_id)
        if args.server in ("all", "service-data"):
            await check_service_data(f"{args.base}/service-data", args.site_id, args.tenant_id)
        if args.server in ("all", "web-search"):
            await check_web_search(f"{args.base}/web-search")
        if args.server in ("all", "write"):
            if not await check_write_servers(args.base, admin_token, args.tenant_id, args.site_id):
                return 1
    except Exception as e:
        print(f"\n❌ 校验失败：{type(e).__name__}: {e}")
        return 1

    print("\n✅ MCP 校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
