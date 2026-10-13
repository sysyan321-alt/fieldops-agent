# scripts/init_milvus.py
# 初始化护理 SOP 知识库 Milvus 集合（schema + 索引）
# 执行：cd <项目根目录> && PYTHONPATH=. python scripts/init_milvus.py
#
# 前置：docker-compose --env-file .env.local up -d（启动 Milvus 容器）

import sys
from pathlib import Path

# 把项目根目录加入 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from pymilvus import MilvusClient, DataType
from backend.config import get_settings
from backend.core.knowledge_base import COLLECTION_NAME


MILVUS_URI = f"http://{get_settings().milvus_host}:{get_settings().milvus_port}"
VECTOR_DIM = 1024                  # BGE-M3 稠密向量维度


def build_schema(client: MilvusClient):
    """构建集合 schema：稠密 + 稀疏双向量 + 标量字段（含 tenant_id）。"""
    schema = client.create_schema(auto_id=False, enable_dynamic_field=True)
    schema.add_field("id",               DataType.VARCHAR, is_primary=True, max_length=64)
    schema.add_field("embedding",        DataType.FLOAT_VECTOR, dim=VECTOR_DIM)      # 稠密
    schema.add_field("sparse_embedding", DataType.SPARSE_FLOAT_VECTOR)              # 稀疏
    schema.add_field("content",          DataType.VARCHAR, max_length=4096)
    schema.add_field("tenant_id",        DataType.VARCHAR, max_length=64)           # 多租户隔离
    schema.add_field("chunk_index",      DataType.INT64)
    schema.add_field("document_id",      DataType.VARCHAR, max_length=64)
    schema.add_field("course_id",        DataType.VARCHAR, max_length=64)   # 语义：SOP 类别 ID
    schema.add_field("source_name",      DataType.VARCHAR, max_length=256)
    schema.add_field("chunk_type",       DataType.VARCHAR, max_length=32)
    schema.add_field("version",          DataType.VARCHAR, max_length=32)
    schema.add_field("updated_at",       DataType.INT64)
    return schema


def build_index_params(client: MilvusClient):
    """构建索引：稠密 HNSW、稀疏 SPARSE_INVERTED、标量 INVERTED。"""
    ip = client.prepare_index_params()
    # 稠密向量：HNSW + COSINE（语义相似度）
    ip.add_index(field_name="embedding", index_type="HNSW", metric_type="COSINE",
                 params={"M": 16, "efConstruction": 256})
    # 稀疏向量：SPARSE_INVERTED_INDEX + IP（内积）；drop_ratio_build 丢弃最低权重 token 省存储
    ip.add_index(field_name="sparse_embedding", index_type="SPARSE_INVERTED_INDEX",
                 metric_type="IP", params={"drop_ratio_build": 0.2})
    # 标量字段：INVERTED 索引，加速 filter 过滤
    ip.add_index(field_name="tenant_id", index_type="INVERTED")
    ip.add_index(field_name="course_id", index_type="INVERTED")
    return ip


def main():
    print(f"连接 Milvus：{MILVUS_URI}")
    client = MilvusClient(uri=MILVUS_URI)

    if client.has_collection(COLLECTION_NAME):
        print(f"🗑️  删除旧集合 '{COLLECTION_NAME}'...")
        client.drop_collection(COLLECTION_NAME)

    # create_collection 传 index_params 会一并建索引并加载
    client.create_collection(
        collection_name=COLLECTION_NAME,
        schema=build_schema(client),
        index_params=build_index_params(client),
    )
    print(f"✅ 集合 '{COLLECTION_NAME}' 创建完成（含索引，已加载）")
    print("当前集合：", client.list_collections())
    print("⚠️  集合已重建，原有数据已清空，请运行 build_knowledge_base.py 导入护理 SOP 文档。")


if __name__ == "__main__":
    main()
