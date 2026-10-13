# backend/core/retrieval_cache.py
# SOP 检索的缓存封装：把"Hybrid 召回 + BGE 精排"的结果按查询缓存
#
# 为什么要缓：一次检索 = BGE-M3 编码（CPU）+ Milvus 混合检索 + CrossEncoder 精排（CPU），
# 是 SOP 问答链路上最贵的一段；护理场景问题高度重复（"鼻饲管怎么护理"一天被问很多次），
# 命中缓存能把这段直接省掉。

import asyncio
import time
from typing import Optional

from backend.core.cache import get_cache, hash_text, make_key
from backend.core.logger import get_logger

logger = get_logger(__name__)


def _serialize(docs: list, confidence: float) -> dict:
    return {
        "confidence": float(confidence),
        "docs": [
            {
                "content": d.content,
                "score": float(d.score),
                "original_index": int(d.original_index),
                "metadata": d.metadata or {},
            }
            for d in docs
        ],
    }


def _deserialize(payload: dict) -> tuple[list, float]:
    from backend.core.reranker import RankedDocument

    docs = [
        RankedDocument(
            content=d.get("content", ""),
            score=float(d.get("score", 0.0)),
            original_index=int(d.get("original_index", 0)),
            metadata=d.get("metadata", {}) or {},
        )
        for d in payload.get("docs", [])
    ]
    return docs, float(payload.get("confidence", 0.0))


async def cached_retrieve(
    query: str,
    tenant_id: str,
    course_id: Optional[str] = None,
    recall_top_k: int = 10,
    rerank_top_k: int = 3,
    key_query: Optional[str] = None,
) -> tuple[list, float]:
    """带缓存的检索：未命中时回落到同步 retrieve（放线程池，不阻塞事件循环）。

    Args:
        query:     真正拿去检索的文本（VAGUE 分支下是 HyDE 生成的假设文档）。
        key_query: 缓存键用哪个问句。默认与 query 相同；但 HyDE 分支必须传**用户原话**——
                   假设文档每次由 LLM 生成、逐字不重复，用它当 key 会让缓存永远不命中
                   （实测同一问题连问两次仍是 cache_miss）。

    Returns:
        (ranked_docs, confidence) —— 与 backend.core.reranker.retrieve 返回值一致
    """
    from backend.config import get_settings
    from backend.core.reranker import retrieve

    cache = get_cache()
    # 缓存键的语义是"用户问了同一句话"，所以键取用户原话（key_query），
    # 值取实际检索结果——HyDE 的随机性只影响命中后要不要重算，不影响键的稳定性。
    cache_query = key_query or query
    key = make_key(
        "retrieval", tenant_id, course_id or "all",
        f"r{recall_top_k}", f"k{rerank_top_k}", hash_text(cache_query),
    )

    cached = await cache.get_json(key)
    if cached:
        docs, confidence = _deserialize(cached)
        logger.info("retrieve.cache_hit", tenant_id=tenant_id, course_id=course_id or "",
                    docs=len(docs), confidence=round(confidence, 4))
        return docs, confidence

    started = time.perf_counter()
    docs, confidence = await asyncio.to_thread(
        retrieve, query, tenant_id, course_id, recall_top_k, rerank_top_k,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    # 只在有结果时写缓存：空结果不缓存，保证新导入的 SOP 立即可见
    if docs:
        await cache.set_json(
            key, _serialize(docs, confidence),
            ttl_seconds=get_settings().cache_ttl_retrieval,
        )

    logger.info("retrieve.cache_miss", tenant_id=tenant_id, course_id=course_id or "",
                docs=len(docs), confidence=round(confidence, 4), elapsed_ms=elapsed_ms)
    return docs, confidence
