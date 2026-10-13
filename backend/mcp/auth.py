# backend/mcp/auth.py
# MCP 写工具的门禁：静态 Bearer Token + 租户白名单
#
# 设计取舍：MCP 的写工具（改知识库、派工单）能造成真实副作用，默认"不配置即禁用"
# （fail-closed）。这里用 ASGI 中间件在 MCP 子应用外层做 Token 校验，
# 比在每个工具里读请求头更简单，也能一并保护 tools/list。

import hmac

from starlette.responses import JSONResponse

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)


def allowed_tenants() -> list[str]:
    """写工具允许操作的租户白名单（MCP_WRITE_TENANTS，逗号分隔）。"""
    raw = get_settings().mcp_write_tenants or ""
    return [t.strip() for t in raw.split(",") if t.strip()]


def assert_tenant_allowed(tenant_id: str) -> None:
    """写工具统一入口校验：租户必须在白名单内，否则拒绝执行。"""
    tenants = allowed_tenants()
    if tenant_id not in tenants:
        logger.warning("mcp.write_tenant_denied", tenant_id=tenant_id, allowed=tenants)
        raise PermissionError(
            f"租户 {tenant_id} 不在 MCP 写工具白名单内（MCP_WRITE_TENANTS={','.join(tenants) or '空'}）"
        )


class BearerTokenMiddleware:
    """给 MCP 子应用加 Bearer Token 门禁（未配置 Token 时直接 503，fail-closed）。"""

    def __init__(self, app, header_name: str = "authorization"):
        self.app = app
        self.header_name = header_name.lower()

    def __getattr__(self, item):
        """把属性访问透传到底层 ASGI 应用。

        主应用启动时要用 `sub_app.router.lifespan_context(sub_app)` 手工驱动子应用 lifespan，
        中间件必须能透出 `.router`，否则 FastAPI 启动阶段就会 AttributeError。
        """
        return getattr(self.app, item)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        expected = get_settings().mcp_admin_token or ""
        if not expected:
            response = JSONResponse(
                {"detail": "MCP 写工具未启用：请在 .env.local 配置 MCP_ADMIN_TOKEN"},
                status_code=503,
            )
            await response(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        provided = headers.get(self.header_name, "")
        prefix = "bearer "
        token = provided[len(prefix):] if provided.lower().startswith(prefix) else ""

        if not token or not hmac.compare_digest(token, expected):
            logger.warning("mcp.auth_rejected", path=scope.get("path", ""))
            response = JSONResponse(
                {"detail": "未授权：请在请求头携带 Authorization: Bearer <MCP_ADMIN_TOKEN>"},
                status_code=401,
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
