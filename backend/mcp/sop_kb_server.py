# backend/mcp/sop_kb_server.py
# 作业规范 知识库 MCP Server（只读）：把 sop_qa Agent 的检索能力暴露成标准工具
#
# 端点：挂载在 /mcp/sop-kb，工具端点为 POST /mcp/sop-kb/mcp
# 独立运行：python -m backend.mcp.sop_kb_server（默认 8011 端口，需要独立进程时用）

from mcp.server.fastmcp import FastMCP

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)

# stateless_http + json_response：每次 POST 自包含，客户端无需握手，便于调试与横向扩展
mcp = FastMCP(
    name="FieldOps-SOPKnowledgeBase",
    instructions=(
        "FieldOps 作业能力工具 · 知识库检索（v1.0，只读，免鉴权）。"
        "工具：search_sop（混合召回+精排检索）、list_sop_documents（列文档）、get_kb_status（知识库状态）。"
        "所有工具必须传 tenant_id。"
    ),
    stateless_http=True,
    json_response=True,
)


def _milvus_client():
    """构造 Milvus 客户端（延迟导入，避免未装 pymilvus 时影响其他模块）。"""
    from pymilvus import MilvusClient

    s = get_settings()
    return MilvusClient(uri=f"http://{s.milvus_host}:{s.milvus_port}")


@mcp.tool()
async def search_sop(
    query: str,
    tenant_id: str,
    category: str | None = None,
    top_k: int = 3,
) -> list[dict]:
    """检索作业规范知识库（Hybrid 召回 + BGE-Reranker 精排）。

    适用场景：作业标准、操作规范、风险处置流程类问题（如"配电间巡检步骤"）。

    Args:
        query:     作业问题，中文或英文
        tenant_id: 租户 ID，用于数据隔离，必填（如 tenant_default）
        category:  可选，规范类别 ID（如 power_room_inspection / confined_space），用于缩小检索范围
        top_k:     精排后返回条数，默认 3

    Returns:
        按相关性降序的片段列表，每项含 content / source_name / category /
        score / confidence / is_high_confidence（confidence >= 0.75 视为高置信）
    """
    # 走带缓存的检索：内部已把 BGE-M3 编码 + CrossEncoder 精排放线程池，
    # 命中 Redis 缓存时直接返回（相同问题不重复算）
    from backend.core.retrieval_cache import cached_retrieve

    ranked_docs, confidence = await cached_retrieve(
        query=query,
        tenant_id=tenant_id,
        course_id=category,
        rerank_top_k=top_k,
    )

    logger.info(
        "mcp.sop_kb.search_done",
        query_preview=query[:50],
        category=category or "",
        hits=len(ranked_docs),
        confidence=round(confidence, 4),
    )

    return [
        {
            "content": doc.content,
            "source_name": doc.metadata.get("source_name", ""),
            "category": doc.metadata.get("course_id", ""),
            "score": round(doc.score, 6),
            "confidence": round(confidence, 4),
            "is_high_confidence": confidence >= 0.75,
        }
        for doc in ranked_docs
    ]


@mcp.tool()
async def list_sop_documents(tenant_id: str) -> list[dict]:
    """列出知识库里的 SOP 文档清单（按文档聚合 chunk 数）。

    Args:
        tenant_id: 租户 ID，必填

    Returns:
        文档列表，每项含 document_id / source_name / category / version / chunks
    """
    from backend.core.knowledge_base import COLLECTION_NAME

    client = _milvus_client()
    if not client.has_collection(COLLECTION_NAME):
        return []

    client.load_collection(COLLECTION_NAME)
    rows = client.query(
        collection_name=COLLECTION_NAME,
        filter=f'tenant_id == "{tenant_id}"',
        output_fields=["document_id", "source_name", "course_id", "version"],
        limit=16384,
    )

    docs: dict[str, dict] = {}
    for r in rows:
        doc_id = r.get("document_id", "")
        item = docs.setdefault(
            doc_id,
            {
                "document_id": doc_id,
                "source_name": r.get("source_name", ""),
                "category": r.get("course_id", ""),
                "version": r.get("version", ""),
                "chunks": 0,
            },
        )
        item["chunks"] += 1

    result = sorted(docs.values(), key=lambda d: d["source_name"])
    logger.info("mcp.sop_kb.list_documents", tenant_id=tenant_id, documents=len(result))
    return result


@mcp.tool()
async def get_kb_status(tenant_id: str) -> dict:
    """查看知识库状态：集合是否存在、当前租户的 chunk 总数。

    Args:
        tenant_id: 租户 ID，必填

    Returns:
        {collection, exists, tenant_id, chunks}
    """
    from backend.core.knowledge_base import COLLECTION_NAME

    client = _milvus_client()
    if not client.has_collection(COLLECTION_NAME):
        return {
            "collection": COLLECTION_NAME,
            "exists": False,
            "tenant_id": tenant_id,
            "chunks": 0,
        }

    client.load_collection(COLLECTION_NAME)
    rows = client.query(
        collection_name=COLLECTION_NAME,
        filter=f'tenant_id == "{tenant_id}"',
        output_fields=["count(*)"],
    )
    chunks = int(rows[0]["count(*)"]) if rows else 0
    logger.info("mcp.sop_kb.status", tenant_id=tenant_id, chunks=chunks)
    return {
        "collection": COLLECTION_NAME,
        "exists": True,
        "tenant_id": tenant_id,
        "chunks": chunks,
    }


if __name__ == "__main__":
    import uvicorn

    port = 8011
    print(f"SOP 知识库 MCP Server → http://localhost:{port}/mcp")
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=port)
