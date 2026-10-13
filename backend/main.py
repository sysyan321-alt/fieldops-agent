# backend/main.py
# FastAPI 应用入口

from contextlib import asynccontextmanager, AsyncExitStack
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.config import get_settings
from backend.core.logger import configure_logging, get_logger
from backend.api.router import api_router

# ── 启动时配置日志 ──────────────────────────────────────────
configure_logging()
logger = get_logger(__name__)

# ── MCP 子应用（挂载在 /mcp/*，对外暴露护理能力工具）──────────
# 说明：Starlette 的 app.mount() 不会自动运行子应用的 lifespan，
# 因此下面在 lifespan 里手工嵌套进入；这里懒构建一次，保证 mount 与 lifespan 用同一实例。
_mcp_apps: dict[str, object] = {}


def _get_mcp_apps() -> dict[str, object]:
    """构建并缓存 MCP 子应用（可用 MCP_ENABLED=false 关闭）。"""
    if not _mcp_apps and get_settings().mcp_enabled:
        from backend.mcp.alert_ops_server import mcp as alert_ops_mcp
        from backend.mcp.care_data_server import mcp as care_data_mcp
        from backend.mcp.care_flow_server import mcp as care_flow_mcp
        from backend.mcp.sop_kb_admin_server import mcp as sop_kb_admin_mcp
        from backend.mcp.sop_kb_server import mcp as sop_kb_mcp
        from backend.mcp.web_search_server import mcp as web_search_mcp
        from backend.mcp.auth import BearerTokenMiddleware

        _mcp_apps["sop-kb"] = sop_kb_mcp.streamable_http_app()
        _mcp_apps["care-data"] = care_data_mcp.streamable_http_app()
        _mcp_apps["web-search"] = web_search_mcp.streamable_http_app()
        # 写工具：外面套一层 Bearer Token 门禁（未配置 MCP_ADMIN_TOKEN 时一律 503，fail-closed）
        _mcp_apps["sop-kb-admin"] = BearerTokenMiddleware(sop_kb_admin_mcp.streamable_http_app())
        _mcp_apps["alert-ops"] = BearerTokenMiddleware(alert_ops_mcp.streamable_http_app())
        _mcp_apps["care-flow"] = BearerTokenMiddleware(care_flow_mcp.streamable_http_app())
    return _mcp_apps


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期钩子。"""
    settings = get_settings()
    logger.info("app.starting", env=settings.app_env, port=settings.app_port)

    # 启动时执行幂等数据库迁移（表结构增量补丁，失败不阻断启动）
    from backend.db.migrations import run_migrations
    try:
        await run_migrations()
    except Exception as e:
        logger.warning("app.migrations_failed", error=str(e))

    # 本地模型懒加载（首次使用时加载，启动时仅记录日志）
    logger.info("app.models_lazy_load", message="models will load on first use")

    # 检查点后端预热：默认建 AsyncPostgresSaver 连接池并建表，
    # 避免第一个请求既要等建表又要等 LLM。失败不阻断启动（auto 模式回落到内存）
    from backend.core.memory import get_memory_saver, close_memory_savers, active_backend
    try:
        await get_memory_saver("startup")
        logger.info("app.checkpointer_ready", backend=active_backend())
    except Exception as e:
        logger.warning("app.checkpointer_failed", error=str(e)[:160])

    # 出站通知投递器：扫发件箱（outbound_events）把业务事件回调给 SaaS
    # 可用 OUTBOUND_ENABLED=false 关闭；未配置租户回调地址时事件留在发件箱等配置
    from backend.core.outbound import start_outbound_worker, stop_outbound_worker
    start_outbound_worker()

    async with AsyncExitStack() as stack:
        for name, sub_app in _get_mcp_apps().items():
            await stack.enter_async_context(sub_app.router.lifespan_context(sub_app))
            logger.info("app.mcp_mounted", name=name, path=f"/mcp/{name}")
        yield

    await stop_outbound_worker()
    await close_memory_savers()
    logger.info("app.shutdown")


def create_app() -> FastAPI:
    """创建 FastAPI 应用。"""
    settings = get_settings()

    app = FastAPI(
        title="CareAgent API",
        description="养老机构 AI 照护智能体系统",
        version="1.0.0",
        lifespan=lifespan,
        debug=settings.app_debug,
    )

    # CORS
    # 改造说明：原来是 allow_origins=["*"] + allow_credentials=True —— 这是**无效组合**，
    # 浏览器在带凭证且来源为通配时会直接拒绝该响应，等于没配。
    # 本项目用 Bearer Token 鉴权、不依赖 Cookie，所以默认 allow_credentials=False，
    # 通配来源合法可用；需要 Cookie 场景时把 CORS_ORIGINS 配成明确白名单再开凭证。
    origins = [o.strip() for o in (settings.cors_origins or "").split(",") if o.strip()]
    if origins:
        allow_credentials = settings.cors_allow_credentials
        if "*" in origins and allow_credentials:
            logger.warning(
                "app.cors_invalid_combo",
                message="allow_origins 含通配符时不能开 allow_credentials（浏览器会拒绝），已强制关闭；"
                        "需要凭证请把 CORS_ORIGINS 配成明确来源白名单",
            )
            allow_credentials = False
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=allow_credentials,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        logger.info("app.cors_enabled", origins=origins, credentials=allow_credentials)
    else:
        # 留空 = 不启用 CORS：同源部署（Vite 代理 / Nginx 反代）下不需要它
        logger.info("app.cors_disabled", message="未配置 CORS_ORIGINS，仅同源访问")

    # 挂载路由
    app.include_router(api_router)

    # 挂载 MCP Server（标准 MCP 端点：POST /mcp/<name>/mcp）
    for name, sub_app in _get_mcp_apps().items():
        app.mount(f"/mcp/{name}", sub_app)

    # 健康检查：/health 是存活探针（不探依赖，很便宜，适合重启策略）；
    #          /health/ready 是就绪探针（真探 DB/Redis/Milvus，不就绪返回 503）
    @app.get("/health")
    async def health_check():
        return {"status": "ok", "service": "CareAgent", "version": "1.0.0"}

    @app.get("/health/ready")
    async def health_ready():
        from fastapi.responses import JSONResponse

        from backend.core.health import readiness

        payload, ready = await readiness()
        payload.update({"service": "CareAgent", "version": "1.0.0"})
        return JSONResponse(status_code=200 if ready else 503, content=payload)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "backend.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_debug,
    )
