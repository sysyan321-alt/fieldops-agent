# scripts/build_knowledge_base.py
# 护理 SOP 知识库构建流水线：文档加载 → 智能分块 → (可选)Contextual RAG → BGE-M3 嵌入 → 写入 Milvus
#
# 用法：
#   # 单文件导入
#   cd <项目根目录> && PYTHONPATH=. python scripts/build_knowledge_base.py \
#       --file data/sop/feeding_tube.md --category feeding_tube
#   # 批量导入 data/sop 下所有 .md
#   PYTHONPATH=. python scripts/build_knowledge_base.py --batch-dir data/sop
#   # 跳过 Contextual RAG（不调外部 LLM，更快）
#   PYTHONPATH=. python scripts/build_knowledge_base.py --batch-dir data/sop --no-context

import uuid
import sys
import asyncio
from pathlib import Path

# 把项目根目录加入 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from langchain_community.document_loaders import TextLoader
from langchain_core.documents import Document
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
    MarkdownTextSplitter,
)

from backend.core.knowledge_base import (
    BGEMEmbedder, DocumentChunk, generate_chunk_id, KnowledgeBaseClient,
)
from backend.core.llm_factory import get_llm
from backend.core.logger import get_logger, configure_logging

configure_logging()
logger = get_logger(__name__)

# ── 模块级分块器单例 ──────────────────────────────────────────
_MD_HEADER_SPLITTER = MarkdownHeaderTextSplitter(
    headers_to_split_on=[
        ("#",   "H1"),
        ("##",  "H2"),
        ("###", "H3"),
        ("####", "H4"),
    ],
    strip_headers=False,
)

_CHAR_SPLITTER = RecursiveCharacterTextSplitter(
    chunk_size=512,
    chunk_overlap=100,
    separators=["\n\n", "\n", "。", "，", " ", ""],
)

BATCH_SIZE = 12   # BGE-M3 批量推理大小


# ── Step 1：文档加载 ────────────────────────────────────────────

def load_document(file_path: str) -> list[Document]:
    """统一文档加载入口（当前支持 .md/.markdown；PDF 走 PyPDFLoader 可扩展）"""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{file_path}")
    ext = path.suffix.lower()
    if ext in (".md", ".markdown"):
        loader = TextLoader(file_path, encoding="utf-8")
        docs = loader.load()
        print(f"  [MD]  加载完成：{len(docs[0].page_content)} 字符 ← {path.name}")
        return docs
    else:
        raise ValueError(
            f"不支持的文件类型：{ext}\n"
            f"当前支持：.md / .markdown\n"
            f"提示：可用 markitdown 将 Word/PDF 转换为 .md 后再导入"
        )


# ── Step 2：Markdown 分块 ──────────────────────────────────────

def split_markdown_documents(
    docs: list[Document],
    chunk_size: int = 512,
    chunk_overlap: int = 100,
) -> list[Document]:
    """Markdown 文档分块：MarkdownHeaderTextSplitter + MarkdownTextSplitter 两阶段"""
    splitter = MarkdownTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    header_chunks: list[Document] = []
    for doc in docs:
        sections = _MD_HEADER_SPLITTER.split_text(doc.page_content)
        source_path = doc.metadata.get("source", "")
        for section in sections:
            section.metadata["source"] = source_path
        header_chunks.extend(sections)

    final_chunks = splitter.split_documents(header_chunks)

    for chunk in final_chunks:
        source_path = chunk.metadata.get("source", "")
        filename    = Path(source_path).stem if source_path else "未知文件"
        parts = [
            chunk.metadata.get("H1", ""),
            chunk.metadata.get("H2", ""),
            chunk.metadata.get("H3", ""),
            chunk.metadata.get("H4", ""),
        ]
        parts = [p for p in parts if p]
        chunk.metadata["source_name"] = (
            f"{filename} > {' > '.join(parts)}" if parts else filename
        )

    print(f"  [MD]  分块完成：{len(docs)} 个文件 → {len(final_chunks)} 个 chunk")
    return final_chunks


def split_documents(docs: list[Document], file_path: str) -> list[Document]:
    """统一分块入口"""
    ext = Path(file_path).suffix.lower()
    if ext in (".md", ".markdown"):
        return split_markdown_documents(docs)
    else:
        raise ValueError(f"不支持的文件类型：{ext}")


# ── Step 2.5：Contextual RAG（LLM 并发为每个 chunk 生成上下文描述）──

MAX_CONTEXT_CONCURRENCY = 5

CONTEXTUAL_CHUNK_PROMPT = """\
<document>
{document_text}
</document>

以下是需要在整个文档中定位的 chunk：
<chunk>
{chunk_content}
</chunk>

请用一句简洁的中文，描述这段内容在整个文档中的位置和作用，以便改善检索效果。
只输出这一句描述，不要加任何前缀或标签。"""


async def generate_chunk_context(
    llm,
    document_text: str,
    chunk_content: str,
    semaphore: asyncio.Semaphore,
) -> str:
    """用 LLM 为单个 chunk 生成一句定位描述。失败时返回空字符串降级。"""
    async with semaphore:
        try:
            from langchain_core.messages import HumanMessage
            prompt = CONTEXTUAL_CHUNK_PROMPT.format(
                document_text=document_text,
                chunk_content=chunk_content,
            )
            resp = await llm.ainvoke([HumanMessage(content=prompt)])
            ctx = (
                resp.text
                if hasattr(resp, "text") and not callable(resp.text)
                else str(resp.content)
            ).strip()
            return ctx
        except Exception as e:
            print(f"   [warning] 上下文生成失败，保留原始 chunk：{e}")
            return ""


async def add_context(
    chunks: list[Document],
    docs: list[Document],
    concurrency: int = MAX_CONTEXT_CONCURRENCY,
) -> list[Document]:
    """Contextual RAG：并发为所有 chunk 生成上下文描述，拼接到 chunk 文本前方。"""
    full_doc_text = "\n\n".join(d.page_content for d in docs)[:8000]
    llm       = get_llm("sop_qa", temperature=0)   # 复用 sop_qa 路由（Qwen-max）
    semaphore = asyncio.Semaphore(concurrency)

    contexts = await asyncio.gather(*[
        generate_chunk_context(llm, full_doc_text, c.page_content, semaphore)
        for c in chunks
    ])
    enriched = 0
    for chunk, ctx in zip(chunks, contexts):
        if ctx:
            chunk.page_content = f"{ctx}\n\n{chunk.page_content}"
            enriched += 1

    print(f"  上下文增强完成：{enriched}/{len(chunks)} 个 chunk 已添加描述")
    return chunks


# ── Step 3：BGE-M3 嵌入 ────────────────────────────────────────

def embed_chunks(
    chunks: list[Document],
    course_id: str,           # SOP 类别 ID
    document_id: str,
    tenant_id: str = "tenant_default",
    version: str = "1.0",
) -> list[DocumentChunk]:
    """对 chunk 列表做 BGE-M3 嵌入，返回 DocumentChunk 列表（含 dense + sparse 向量）。"""
    embedder = BGEMEmbedder.get_instance()
    all_doc_chunks: list[DocumentChunk] = []
    total = len(chunks)
    for batch_start in range(0, total, BATCH_SIZE):
        batch = chunks[batch_start: batch_start + BATCH_SIZE]
        texts = [c.page_content for c in batch]
        dense_vecs, sparse_vecs = embedder.encode(texts, batch_size=BATCH_SIZE)
        for i, (chunk, dense, sparse) in enumerate(zip(batch, dense_vecs, sparse_vecs)):
            global_index = batch_start + i
            all_doc_chunks.append(DocumentChunk(
                id=generate_chunk_id(chunk.page_content, document_id, global_index),
                content=chunk.page_content,
                embedding=dense,
                sparse_embedding=sparse,
                course_id=course_id,
                document_id=document_id,
                source_name=chunk.metadata.get("source_name", ""),
                chunk_type=chunk.metadata.get("chunk_type", "text"),
                chunk_index=global_index,
                version=version,
                tenant_id=tenant_id,
            ))
        done = min(batch_start + BATCH_SIZE, total)
        print(f"  嵌入进度：{done}/{total}")
    print(f"  嵌入完成：{len(all_doc_chunks)} 个 DocumentChunk")
    return all_doc_chunks


# ── Step 4：写入 Milvus ────────────────────────────────────────

def write_to_milvus(doc_chunks: list[DocumentChunk]) -> None:
    """将 DocumentChunk 列表写入 Milvus（upsert 幂等）。"""
    if not doc_chunks:
        print("  ⚠️  无 chunk 可写入，跳过")
        return
    kb = KnowledgeBaseClient()
    written = kb.upsert_chunks(doc_chunks)
    print(f"  ✅ 写入完成：{written} 个 chunk → {KnowledgeBaseClient.__module__}")


# ── 主流水线 ─────────────────────────────────────────────────

async def build_pipeline(
    file_path:   str,
    course_id:   str,
    document_id: str,
    tenant_id:   str = "tenant_default",
    version:     str = "1.0",
    use_context: bool = True,
) -> None:
    """知识库建库完整流水线（四步 + 可选 Contextual RAG）。"""
    print(f"\n{'='*55}")
    print(f" CareAgent 护理 SOP 知识库构建")
    print(f" 文件      ：{file_path}")
    print(f" SOP 类别  ：{course_id}")
    print(f" 文档 ID   ：{document_id}")
    print(f" 租户      ：{tenant_id}")
    print(f" Contextual RAG：{'启用' if use_context else '跳过（--no-context）'}")
    print(f"{'='*55}\n")

    print("📖 Step 1/4  读取文档…")
    docs = load_document(file_path)

    print("\n✂️  Step 2/4  智能分块…")
    chunks = split_documents(docs, file_path)

    if use_context and chunks:
        print(f"\n🧠 Step 2.5  Contextual RAG 上下文增强"
              f"（并发={MAX_CONTEXT_CONCURRENCY}）…")
        chunks = await add_context(chunks, docs)

    print("\n🔢 Step 3/4  BGE-M3 嵌入…")
    doc_chunks = embed_chunks(
        chunks,
        course_id=course_id,
        document_id=document_id,
        tenant_id=tenant_id,
        version=version,
    )

    print("\n💾 Step 4/4  写入 Milvus…")
    write_to_milvus(doc_chunks)

    print(f"\n🎉 完成！共处理 {len(doc_chunks)} 个 chunk")
    print(f"   document_id = {document_id}")
    print(f"   SOP 类别    = {course_id}")


async def build_batch(
    dir_path:    str,
    tenant_id:   str = "tenant_default",
    version:     str = "1.0",
    use_context: bool = True,
) -> None:
    """批量导入目录下所有 .md 文件，文件名（去扩展名）作为 SOP 类别 ID。"""
    dir_p = Path(dir_path)
    if not dir_p.is_dir():
        raise FileNotFoundError(f"目录不存在：{dir_path}")

    md_files = sorted(dir_p.glob("*.md"))
    if not md_files:
        print(f"⚠️  目录 {dir_path} 下没有 .md 文件")
        return

    print(f"\n{'='*55}")
    print(f" CareAgent 护理 SOP 知识库批量构建")
    print(f" 目录      ：{dir_path}")
    print(f" 文件数    ：{len(md_files)}")
    print(f" 租户      ：{tenant_id}")
    print(f" Contextual RAG：{'启用' if use_context else '跳过'}")
    print(f"{'='*55}")

    total_chunks = 0
    for md_file in md_files:
        # 文件名（去 .md）作为 SOP 类别 ID，如 feeding_tube
        course_id = md_file.stem
        document_id = str(uuid.uuid4())
        print(f"\n\n▶ 处理文件：{md_file.name}（类别={course_id}）")
        await build_pipeline(
            file_path=str(md_file),
            course_id=course_id,
            document_id=document_id,
            tenant_id=tenant_id,
            version=version,
            use_context=use_context,
        )
        total_chunks += 1
    print(f"\n\n{'='*55}")
    print(f"🎉 批量构建完成！共处理 {len(md_files)} 个文件")
    print(f"{'='*55}")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description="导入护理 SOP 文档到 Milvus 知识库")
    parser.add_argument(
        "--file", default=None,
        help="单个 .md 文件路径（与 --batch-dir 二选一）",
    )
    parser.add_argument(
        "--batch-dir", default=None,
        help="批量导入目录下所有 .md 文件",
    )
    parser.add_argument("--category", default=None, help="SOP 类别 ID（单文件时必填）")
    parser.add_argument("--document-id", default=None, help="已有文档 ID；默认自动生成")
    parser.add_argument("--tenant-id", default="tenant_default")
    parser.add_argument("--version", default="1.0")
    parser.add_argument("--no-context", action="store_true",
                        help="跳过 Contextual RAG，不调用外部 LLM")
    args = parser.parse_args()

    use_context = not args.no_context

    if args.batch_dir:
        asyncio.run(build_batch(
            dir_path=args.batch_dir,
            tenant_id=args.tenant_id,
            version=args.version,
            use_context=use_context,
        ))
    elif args.file:
        if not args.category:
            # 默认用文件名作类别
            args.category = Path(args.file).stem
        doc_id = args.document_id or str(uuid.uuid4())
        asyncio.run(build_pipeline(
            file_path=args.file,
            course_id=args.category,
            document_id=doc_id,
            tenant_id=args.tenant_id,
            version=args.version,
            use_context=use_context,
        ))
    else:
        parser.print_help()
        print("\n⚠️  必须指定 --file 或 --batch-dir")
        sys.exit(1)
