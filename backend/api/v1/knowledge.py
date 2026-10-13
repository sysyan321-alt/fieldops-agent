# backend/api/v1/knowledge.py
# 护理 SOP 语义搜索 API（与 MCP 的 search_sop 共用同一套检索实现）
#
# 定位：REST 侧的"sematnic search 入口"。SOP 问答是"检索 + 生成"，
# 这里只暴露检索能力，供前端搜索框、外部系统或内部工具直接调用。

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.core.logger import get_logger
from backend.dependencies import get_current_user

logger = get_logger(__name__)
router = APIRouter(prefix="/knowledge")

HIGH_CONFIDENCE_THRESHOLD = 0.75


class SOPKnowledgeSearchRequest(BaseModel):
    """语义搜索请求。"""
    query: str = Field(..., min_length=1, description="护理问题（中文或英文）")
    category: str | None = Field(default=None, description="SOP 类别 ID，如 feeding_tube")
    top_k: int = Field(default=3, ge=1, le=20, description="精排后返回条数")
    recall_top_k: int = Field(default=10, ge=1, le=50, description="混合召回条数")
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="过滤低于该置信度的结果")


@router.post("/search")
async def search_knowledge(
    req: SOPKnowledgeSearchRequest,
    current_user: dict = Depends(get_current_user),
):
    """护理 SOP 语义搜索：混合召回（稠密 + 稀疏）→ 重排精排 → 带置信度返回。

    与 SOP 问答 Agent 的检索节点共用 `cached_retrieve`，因此同样享受 Redis 检索缓存
    （同问二次检索 8.6s → 0.02s）与租户强隔离。
    """
    from backend.core.retrieval_cache import cached_retrieve

    tenant_id = current_user["tenant_id"]
    try:
        docs, confidence = await cached_retrieve(
            query=req.query,
            tenant_id=tenant_id,
            course_id=req.category,
            recall_top_k=req.recall_top_k,
            rerank_top_k=req.top_k,
        )
    except Exception as e:
        # 与 Agent 链路一致的降级策略：检索失败不返回 500，而是返回空结果 + 低置信度
        logger.error("knowledge.search_failed", error=str(e), query=req.query[:50])
        raise HTTPException(status_code=503, detail="检索服务暂不可用，请稍后重试")

    hits = [
        {
            "content": doc.content,
            "source_name": doc.metadata.get("source_name", ""),
            "category": doc.metadata.get("course_id", ""),
            "score": round(doc.score, 6),
        }
        for doc in docs
        if doc.score >= req.min_confidence
    ]

    logger.info("knowledge.search_done", tenant_id=tenant_id, category=req.category or "",
                hits=len(hits), confidence=round(confidence, 4))
    return {
        "query": req.query,
        "category": req.category,
        "count": len(hits),
        "confidence": round(confidence, 4),
        "is_high_confidence": confidence >= HIGH_CONFIDENCE_THRESHOLD,
        "hits": hits,
    }
