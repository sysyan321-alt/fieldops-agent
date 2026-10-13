# backend/core/knowledge_base.py
# 护理 SOP 知识库客户端：BGE-M3 本地嵌入 + Milvus 混合检索
# 护理 SOP 检索知识库。
# 单 Collection（nursing_sop_kb），按 tenant_id 字段过滤实现多租户隔离。

import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional
import os

backend_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from pymilvus import MilvusClient, AnnSearchRequest, WeightedRanker

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)

# ── 集合名：护理 SOP 知识库 ──
COLLECTION_NAME = "nursing_sop_kb"


# ──────────────────────────────────────────────────────────────
# BGE-M3 本地嵌入模型（进程内单例，dense + sparse 双输出）
# ──────────────────────────────────────────────────────────────

class BGEMEmbedder:
    """
    BGE-M3 本地嵌入模型单例。

    一次推理同时输出：
      - dense 向量（1024 维浮点数组，用于语义相似度检索）
      - sparse 向量（{token_id: weight} 字典，用于关键词精确检索）

    进程内单例：首次调用 get_instance() 时加载模型（约5-15秒），
    后续调用直接返回同一实例，不重复加载。

    用法：
        embedder = BGEMEmbedder.get_instance()
        dense, sparse = embedder.encode_query("鼻饲管日常维护步骤？")
    """

    _instance: Optional["BGEMEmbedder"] = None   # 单例持有

    def __init__(self, model_path: str):
        # ── 兼容性补丁①：FlagEmbedding 1.3.x 依赖 transformers 内部函数 ──
        # transformers>=5.0 移除了 is_torch_fx_available，
        # 当前锁定 transformers==4.51.0 不受影响，此补丁作为保险。
        import importlib.util as _ilu
        from transformers.utils import import_utils as _tf_iu
        if not hasattr(_tf_iu, "is_torch_fx_available"):
            _tf_iu.is_torch_fx_available = (
                lambda: _ilu.find_spec("torch.fx") is not None
            )

        # ── 兼容性补丁②：FlagEmbedding 1.4.2 ↔ transformers 4.51.0 ──
        # FlagEmbedding 1.4.2 用 dtype= 传给 AutoModel.from_pretrained，
        # 但 transformers 4.51.0 用 torch_dtype=。此处把 dtype 转成 torch_dtype。
        # 全局 patch：仅在 from_pretrained 收到 dtype 时转换，其他调用不受影响。
        from transformers import AutoModel as _AutoModel
        if not getattr(_AutoModel, "_fieldops_dtype_patched", False):
            _orig_fp = _AutoModel.from_pretrained.__func__
            def _patched_fp(cls, *args, **kwargs):
                if "dtype" in kwargs and "torch_dtype" not in kwargs:
                    kwargs["torch_dtype"] = kwargs.pop("dtype")
                return _orig_fp(cls, *args, **kwargs)
            _AutoModel.from_pretrained = classmethod(_patched_fp)
            _AutoModel._fieldops_dtype_patched = True

        import torch
        from FlagEmbedding import BGEM3FlagModel

        logger.info("bge_m3.loading", model_path=model_path)

        # ── fp16 仅在 CUDA 上启用，MPS（Apple M 系列）不启用 ──
        # MPS 在 BGE-M3 attention 矩阵乘法上会触发 LLVM ERROR，
        # CPU 模式下用 fp32，速度稍慢但稳定。
        _use_fp16 = torch.cuda.is_available()

        self._model = BGEM3FlagModel(
            model_name_or_path=model_path,
            use_fp16=_use_fp16,
        )
        logger.info("bge_m3.loaded", use_fp16=_use_fp16)

    @classmethod
    def get_instance(cls) -> "BGEMEmbedder":
        """获取单例（首次调用时加载模型，后续复用）"""
        if cls._instance is None:
            # 配置里是相对路径（./models/...），拼上 backend 目录得到绝对路径
            bge_m3_model_path = os.path.join(
                backend_path, get_settings().bge_m3_model_path
            )
            # 本地权重缺失时回退到 HuggingFace 拉取 BAAI/bge-m3（与 reranker 同策略），
            # 避免空目录导致 FlagEmbedding 加载失败
            use_local = (
                os.path.isdir(bge_m3_model_path)
                and any(
                    f.endswith((".bin", ".safetensors", ".json"))
                    for f in os.listdir(bge_m3_model_path)
                )
            )
            model_id = bge_m3_model_path if use_local else "BAAI/bge-m3"
            cls._instance = BGEMEmbedder(model_id)
        return cls._instance

    def encode(
        self,
        texts: list[str],
        batch_size: int = 12,
    ) -> tuple[list[list[float]], list[dict]]:
        """
        批量编码文本，同时返回 dense 和 sparse 两种向量。

        Args:
            texts:      待编码的文本列表
            batch_size: 单次推理批大小，越大速度越快但显存占用越多；
                        12 是 16GB 显存 / 统一内存下的经验值

        Returns:
            (dense_vecs, sparse_vecs)
              dense_vecs:  list of 1024-dim float 向量，每项对应 texts[i]
              sparse_vecs: list of {token_id: weight} 字典，每项对应 texts[i]
        """
        output = self._model.encode(
            texts,
            batch_size=batch_size,
            max_length=8192,            # BGE-M3 支持最长 8192 token
            return_dense=True,          # 输出稠密语义向量
            return_sparse=True,         # 输出稀疏关键词向量
            return_colbert_vecs=False,   # ColBERT 多向量表示，本项目不用
        )
        dense_vecs = output["dense_vecs"].tolist()   # numpy → Python list
        # sparse: numpy.float16 → Python float
        # 必须转换！msgpack 不支持 numpy.float16，会抛 TypeError。
        sparse_vecs = [{int(k): float(v) for k, v in d.items()}
                       for d in output["lexical_weights"]]
        return dense_vecs, sparse_vecs

    def encode_query(self, text: str) -> tuple[list[float], dict]:
        """
        编码单条查询，返回 (dense_vec, sparse_vec)。
        查询时调用此方法（而非 encode），batch_size=1 避免 padding。
        """
        dense_list, sparse_list = self.encode([text], batch_size=1)
        return dense_list[0], sparse_list[0]


# ──────────────────────────────────────────────────────────────
# DocumentChunk：准备写入 Milvus 的单个文档块
# ──────────────────────────────────────────────────────────────

@dataclass
class DocumentChunk:
    """
    准备写入 Milvus 的单个文档块，字段与 Milvus Schema 一一对应。

    id:               全局唯一 ID（MD5 of content + document_id + chunk_index）
    content:          chunk 文本（Contextual RAG 模式下含 LLM 生成的上下文描述前缀）
    embedding:        Dense 向量（BGE-M3，1024 维）
    sparse_embedding: Sparse 向量（{token_id: weight}，BGE-M3 lexical weights）
    source_name:      来源标注（如 "鼻饲管护理SOP > 第3章 > 3.1 日常维护"）

    注：course_id 字段在护理场景下语义为「SOP 类别 ID」
        （如 feeding_tube / pressure_ulcer / fall_prevention）。
    """
    id:               str
    content:          str
    embedding:        list[float]
    sparse_embedding: dict
    course_id:        str            # SOP 类别 ID（语义见类注释）
    document_id:      str
    source_name:      str
    chunk_type:       str            # "text" / "procedure" / "table"
    chunk_index:      int
    version:          str
    tenant_id:        str = "tenant_default"
    updated_at:       int = field(default_factory=lambda: int(time.time()))


def generate_chunk_id(content: str, document_id: str, chunk_index: int) -> str:
    """生成 chunk 全局唯一 ID（MD5）。内容+位置不变则 ID 不变，支持幂等 upsert。"""
    raw = f"{document_id}_{chunk_index}_{content[:50]}"
    return hashlib.md5(raw.encode()).hexdigest()


# ──────────────────────────────────────────────────────────────
# KnowledgeBaseClient：Milvus 知识库客户端
# ──────────────────────────────────────────────────────────────

class KnowledgeBaseClient:
    """
    Milvus 知识库客户端（MilvusClient 版）。

    单 Collection 设计（nursing_sop_kb），按 tenant_id 字段过滤实现多租户隔离。
    单例连接：_client 是类变量，整个进程只创建一次 MilvusClient 连接。
    """

    _client: Optional["MilvusClient"] = None
    _loaded: bool = False

    # HNSW 搜索时的候选集大小，精度/速度平衡点
    ANN_EF = 64
    # Hybrid 召回的候选数量，传给 Reranker 精排
    VECTOR_TOP_K = 10

    def __init__(self):
        if KnowledgeBaseClient._client is None:
            settings = get_settings()
            uri = f"http://{settings.milvus_host}:{settings.milvus_port}"
            KnowledgeBaseClient._client = MilvusClient(uri=uri)
            logger.info("milvus.connected", uri=uri)

        if not KnowledgeBaseClient._loaded:
            try:
                KnowledgeBaseClient._client.load_collection(COLLECTION_NAME)
            except Exception:
                pass   # init_milvus.py 尚未运行时忽略
            KnowledgeBaseClient._loaded = True

    # ── 写入：批量 Upsert ────────────────────────────────────

    def upsert_chunks(self, chunks: list) -> int:
        """批量写入文档块（Upsert：primary key 存在则更新，不存在则插入）。"""
        if not chunks:
            return 0
        data = [
            {
                "id":               c.id,
                "embedding":        c.embedding,
                "sparse_embedding": c.sparse_embedding,
                "content":          c.content[:4096],
                "chunk_index":      c.chunk_index,
                "document_id":      c.document_id,
                "course_id":        c.course_id,
                "tenant_id":        c.tenant_id,
                "source_name":      c.source_name,
                "chunk_type":       c.chunk_type,
                "version":          c.version,
                "updated_at":       c.updated_at,
            }
            for c in chunks
        ]
        self._client.upsert(collection_name=COLLECTION_NAME, data=data)
        logger.info("knowledge_base.chunks_upserted", count=len(chunks))
        return len(chunks)

    def delete_document_chunks(self, document_id: str) -> None:
        """删除指定文档的所有 chunk（文档更新时先删后插，幂等重建）。"""
        safe_id = document_id.replace('"', '\\"')
        self._client.delete(
            collection_name=COLLECTION_NAME,
            filter=f'document_id == "{safe_id}"',
        )
        logger.info("knowledge_base.document_deleted", document_id=document_id)

    @staticmethod
    def generate_chunk_id(content: str, document_id: str, chunk_index: int) -> str:
        """生成 chunk 唯一 ID（MD5）。"""
        raw = f"{document_id}_{chunk_index}_{content[:50]}"
        return hashlib.md5(raw.encode()).hexdigest()

    # ── 检索：Dense + Sparse 混合召回 ─────────────────────────

    def _hybrid_search(
            self,
            query_embedding: list[float],
            query_sparse: dict,
            top_k: int,
            filters: Optional[str] = None,
    ) -> list[dict]:
        """
        对 nursing_sop_kb 做 Hybrid 检索（Dense + Sparse → WeightedRanker 融合）。

        两个 AnnSearchRequest 分别构造 Dense 和 Sparse 检索请求，
        由 Milvus 在服务端并行执行后，用 WeightedRanker 加权融合排序。

        Args:
            query_embedding: Dense Query 向量（1024 维，来自 encode_query）
            query_sparse:    Sparse Query 向量（{token_id: weight}）
            top_k:           每路召回数量（融合后同样取 top_k）
            filters:         Milvus bool 表达式，如 'tenant_id == "xxx"'

        Returns:
            候选文档列表，每项含 "content" / "score" / "metadata"。
            score 是 WeightedRanker 的加权排序信号，不是概率，
            直接交给 Reranker 做精细打分。
        """
        try:
            # ── Dense ANN 检索请求 ─────────────────────────────────────
            # COSINE 度量匹配 BGE-M3 dense 向量（L2 归一化后等价于余弦相似度）
            dense_req = AnnSearchRequest(
                data=[query_embedding],
                anns_field="embedding",
                param={
                    "metric_type": "COSINE",
                    "params": {"ef": self.ANN_EF},
                },
                limit=top_k,
                expr=filters,
            )

            # ── Sparse 关键词检索请求 ──────────────────────────────────
            # IP（内积）是 BGE-M3 lexical_weights 的标准度量
            sparse_req = AnnSearchRequest(
                data=[query_sparse],
                anns_field="sparse_embedding",
                param={"metric_type": "IP"},
                limit=top_k,
                expr=filters,
            )

            output_fields = [
                "content", "source_name", "chunk_type",
                "course_id", "document_id", "chunk_index",
            ]

            # ── WeightedRanker(0.7, 0.3)：Dense 0.7 / Sparse 0.3 ──────
            # 注意：直接调底层 conn.hybrid_search，绕过 MilvusClient.hybrid_search 的高层封装。
            # pymilvus 2.4.9 的 MilvusClient.hybrid_search 在 line 351 用 `for hits in res`
            # 迭代 SearchResult，但某些版本组合下 `hits` 是 SequenceIterator 不可迭代，触发
            # `TypeError: 'SequenceIterator' object is not iterable`。
            # 底层 conn 返回 SearchResult，res[0] 是 Hits，可用 list() 正常消费。
            conn = self._client._get_connection()
            res = conn.hybrid_search(
                collection_name=COLLECTION_NAME,
                reqs=[dense_req, sparse_req],
                rerank=WeightedRanker(0.7, 0.3),   # 底层 GrpcHandler 用 rerank=（非 ranker=）
                limit=top_k,
                output_fields=output_fields,
            )
            candidates = []
            # res[0] 是第一个查询的 Hits 对象；list() 显式消费为 Hit 列表
            for hit in list(res[0]):
                entity = hit.entity or {}
                candidates.append({
                    "content": entity.get("content") or "",
                    "score":   hit.distance or 0.0,
                    "metadata": {
                        "source_name": entity.get("source_name") or "",
                        "chunk_type":  entity.get("chunk_type")  or "text",
                        "course_id":   entity.get("course_id")   or "",
                        "document_id": entity.get("document_id") or "",
                        "chunk_index": entity.get("chunk_index") or 0,
                    },
                })
            logger.info(
                "knowledge_base.hybrid_search_done",
                candidates=len(candidates),
            )
            return candidates
        except Exception as e:
            logger.error("knowledge_base.hybrid_search_failed", error=str(e))
            return []

    @staticmethod
    def _build_filter(
        tenant_id: str,
        course_id: Optional[str] = None,
    ) -> str:
        """
        构建 Milvus bool 过滤表达式。
        course_id 在护理场景下语义为 SOP 类别 ID（如 feeding_tube）。
        """
        safe_tenant = tenant_id.replace('"', '\\"')
        expr = f'tenant_id == "{safe_tenant}"'
        if course_id:
            safe_course = course_id.replace('"', '\\"')
            expr += f' and course_id == "{safe_course}"'
        return expr


if __name__ == '__main__':
    # 自检：加载模型 + 跑一次空检索，验证连通性
    model = BGEMEmbedder.get_instance()
    dense, sparse = model.encode_query(text="鼻饲管置管后的日常维护操作步骤")
    print(f'dense dim: {len(dense)}, sparse keys: {len(sparse)}')
    kb = KnowledgeBaseClient()
    results = kb._hybrid_search(dense, sparse, top_k=5)
    print(f'results: {len(results)} 条（空库时应为 0）')
