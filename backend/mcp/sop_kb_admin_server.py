# backend/mcp/sop_kb_admin_server.py
# 护理 SOP 知识库「管理」MCP Server（写操作，需 Token 鉴权）
#
# 端点：挂载在 /mcp/sop-kb-admin（POST /mcp/sop-kb-admin/mcp），外层套 BearerTokenMiddleware
# 独立运行：python -m backend.mcp.sop_kb_admin_server（默认 8015 端口，仍需自加门禁）
#
# 与只读的 sop-kb server 拆开的原因：写工具要门禁，读工具不该被迫带 Token，
# 分成两个子应用后，鉴权中间件只包住写的那一个。

import asyncio
import importlib.util
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from backend.config import get_settings
from backend.core.logger import get_logger
from backend.mcp.auth import assert_tenant_allowed

logger = get_logger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT_DIR / "data"

mcp = FastMCP(
    name="CareAgent-SOPKnowledgeBaseAdmin",
    instructions=(
        "CareAgent 护理能力工具 · 知识库管理（v1.0，写工具，需 Authorization: Bearer <MCP_ADMIN_TOKEN>）。"
        "工具：ingest_sop_document（导入/更新 SOP，仅允许 data/ 目录）、delete_sop_document（删除文档）、"
        "rebuild_collection（重建集合，需 confirm=DROP_AND_REBUILD）。所有工具必须传 tenant_id 且在 MCP_WRITE_TENANTS 白名单内。"
    ),
    stateless_http=True,
    json_response=True,
)


def _load_module(script_name: str, module_name: str):
    """按文件路径加载 scripts/ 下的脚本（scripts 不是包，用 importlib 按文件加载）。"""
    script = ROOT_DIR / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(module_name, script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resolve_safe_path(file_path: str) -> Path:
    """只允许导入 data/ 目录下的 .md / .txt，防止调用方读走服务器任意文件。"""
    raw = Path(file_path)
    target = (raw if raw.is_absolute() else ROOT_DIR / raw).resolve()
    if DATA_DIR.resolve() not in target.parents:
        raise PermissionError(f"只允许导入 {DATA_DIR} 目录下的文件，收到：{file_path}")
    if target.suffix.lower() not in (".md", ".txt"):
        raise ValueError(f"只支持 .md / .txt 文档，收到：{target.suffix}")
    if not target.is_file():
        raise FileNotFoundError(f"文件不存在：{target}")
    return target


def _flush_collection() -> None:
    """flush 集合：Milvus 的写入/删除在 flush 前对 query（count/过滤检索）不可见。

    工具化场景要求"改完即可查"，否则调用方紧接着的 list/search 会看到旧数据。
    注意 pymilvus 2.4.9 的 MilvusClient 没有 flush()，只能用底层连接。
    """
    from pymilvus import MilvusClient

    from backend.core.knowledge_base import COLLECTION_NAME

    s = get_settings()
    client = MilvusClient(uri=f"http://{s.milvus_host}:{s.milvus_port}")
    if not client.has_collection(COLLECTION_NAME):
        return
    try:
        client._get_connection().flush([COLLECTION_NAME])
    except Exception as e:  # flush 失败不影响写入本身，只是可见性稍晚
        logger.warning("mcp.sop_kb_admin.flush_failed", error=str(e))


async def _invalidate_retrieval_cache(reason: str) -> int:
    """知识库变更后清掉检索缓存，避免用户拿到变更前的旧答案。"""
    from backend.core.cache import get_cache, make_key

    deleted = await get_cache().delete_prefix(make_key("retrieval"))
    logger.info("mcp.sop_kb_admin.cache_invalidated", reason=reason, keys=deleted)
    return deleted


@mcp.tool()
async def ingest_sop_document(
    file_path: str,
    category: str,
    tenant_id: str,
    version: str = "1.0",
    use_context: bool = True,
) -> dict:
    """导入（或新增）一篇护理 SOP 文档到知识库。

    对应脚本：scripts/build_knowledge_base.py
    流程：读取 → 智能分块 →（可选）Contextual RAG 增强 → BGE-M3 嵌入 → 写入 Milvus。

    Args:
        file_path:   文档路径，必须在 data/ 目录下（如 data/sop/feeding_tube.md）
        category:    SOP 类别 ID（如 feeding_tube），写入 course_id 字段供检索过滤
        tenant_id:   租户 ID，必填且必须在 MCP_WRITE_TENANTS 白名单内
        version:     文档版本号，默认 1.0
        use_context: 是否启用 Contextual RAG 增强（需调 LLM，关闭可显著加快导入）

    Returns:
        {status, document_id, category, file_path, version, use_context}
    """
    assert_tenant_allowed(tenant_id)
    target = _resolve_safe_path(file_path)
    document_id = str(uuid.uuid4())

    build_pipeline = _load_module("build_knowledge_base.py", "care_agent_build_kb").build_pipeline
    await build_pipeline(
        file_path=str(target),
        course_id=category,
        document_id=document_id,
        tenant_id=tenant_id,
        version=version,
        use_context=use_context,
    )

    _flush_collection()
    await _invalidate_retrieval_cache("ingest")

    logger.info("mcp.sop_kb_admin.ingested", document_id=document_id, category=category,
                tenant_id=tenant_id, use_context=use_context)
    return {
        "status": "ingested",
        "document_id": document_id,
        "category": category,
        "file_path": str(target.relative_to(ROOT_DIR)),
        "version": version,
        "use_context": use_context,
    }


@mcp.tool()
async def delete_sop_document(document_id: str, tenant_id: str) -> dict:
    """删除知识库里某篇 SOP 文档的全部 chunk（先校验该文档属于此租户）。

    Args:
        document_id: 文档 ID（ingest_sop_document 返回，或 list_sop_documents 查出）
        tenant_id:   租户 ID，必填且必须在白名单内

    Returns:
        {status, document_id, deleted_chunks}
    """
    assert_tenant_allowed(tenant_id)

    from pymilvus import MilvusClient

    from backend.core.knowledge_base import COLLECTION_NAME, KnowledgeBaseClient

    s = get_settings()
    client = MilvusClient(uri=f"http://{s.milvus_host}:{s.milvus_port}")
    if not client.has_collection(COLLECTION_NAME):
        return {"status": "collection_missing", "document_id": document_id, "deleted_chunks": 0}

    client.load_collection(COLLECTION_NAME)
    safe_id = document_id.replace('"', '\\"')
    owned = client.query(
        collection_name=COLLECTION_NAME,
        filter=f'document_id == "{safe_id}" and tenant_id == "{tenant_id}"',
        output_fields=["count(*)"],
    )
    owned_chunks = int(owned[0]["count(*)"]) if owned else 0
    if owned_chunks == 0:
        # 不存在与跨租户返回同样的结果，避免泄漏 ID 是否存在
        return {"status": "not_found", "document_id": document_id, "deleted_chunks": 0}

    KnowledgeBaseClient().delete_document_chunks(document_id)
    _flush_collection()
    await _invalidate_retrieval_cache("delete")
    logger.info("mcp.sop_kb_admin.deleted", document_id=document_id,
                tenant_id=tenant_id, chunks=owned_chunks)
    return {"status": "deleted", "document_id": document_id, "deleted_chunks": owned_chunks}


@mcp.tool()
async def rebuild_collection(tenant_id: str, confirm: str) -> dict:
    """重建 Milvus 集合（**drop 整个集合，所有租户的数据都会被清空**）。

    对应脚本：scripts/init_milvus.py；重建后需重新导入 SOP 文档。
    因为影响面是全局的，必须显式传 confirm="DROP_AND_REBUILD" 才执行。

    Args:
        tenant_id: 租户 ID，必填且必须在白名单内
        confirm:   确认字符串，必须等于 DROP_AND_REBUILD
    """
    assert_tenant_allowed(tenant_id)
    if confirm != "DROP_AND_REBUILD":
        raise ValueError('重建集合影响全部租户，需显式传 confirm="DROP_AND_REBUILD"')

    from pymilvus import MilvusClient

    from backend.core.knowledge_base import COLLECTION_NAME

    def _rebuild() -> list[str]:
        s = get_settings()
        client = MilvusClient(uri=f"http://{s.milvus_host}:{s.milvus_port}")
        if client.has_collection(COLLECTION_NAME):
            client.drop_collection(COLLECTION_NAME)
        # 复用建库脚本，schema 与索引定义只保留一处
        _load_module("init_milvus.py", "care_agent_init_milvus").main()
        return list(client.list_collections())

    collections = await asyncio.to_thread(_rebuild)
    await _invalidate_retrieval_cache("rebuild")
    logger.warning("mcp.sop_kb_admin.collection_rebuilt", tenant_id=tenant_id,
                   collections=collections)
    return {
        "status": "rebuilt",
        "collection": COLLECTION_NAME,
        "collections": collections,
        "note": "集合已清空，请重新导入 SOP 文档",
    }


if __name__ == "__main__":
    import uvicorn

    port = 8015
    print(f"SOP 知识库管理 MCP Server → http://localhost:{port}/mcp（独立运行时请自行加鉴权）")
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=port)
