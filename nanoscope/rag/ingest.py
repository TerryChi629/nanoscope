"""M18 · 文档 ingestion pipeline (PRD_v4 §M18.2 第 2 点)。

chunk → (embed 由检索器按需) → 带 ACL 标签落库。本模块负责 **chunking + 落库**，
embedding 在检索侧按需批量取（复用 M7 `GlmEmbedder`，凭证走环境变量）。

红线：chunk 的 tenant/scope/owner/acl 标签由调用方（运行时上下文）注入，
`ingest_document` 只透传给 `ChunkStore.add_chunk`，模型/文档内容无法伪造归属。
"""

from __future__ import annotations

from nanoscope.identity import SecurityContext
from nanoscope.rag.store import ChunkStore, DocChunkRecord


def chunk_fixed_window(text: str, *, size: int = 200, overlap: int = 40) -> list[str]:
    """固定窗口 + 重叠切分。

    以字符为单位（对 CJK 友好，无需分词器）。相邻 chunk 重叠 `overlap` 字符以保上下文
    连续。size<=0 或 overlap>=size 视为配置错误（fail-closed 抛错，不静默退化）。
    """
    if size <= 0:
        raise ValueError("size 必须为正")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap 必须在 [0, size) 内")
    text = text.strip()
    if not text:
        return []
    step = size - overlap
    chunks: list[str] = []
    i = 0
    while i < len(text):
        chunk = text[i : i + size]
        if chunk.strip():
            chunks.append(chunk)
        if i + size >= len(text):
            break
        i += step
    return chunks


def chunk_by_structure(text: str, *, max_size: int = 400) -> list[str]:
    """结构感知切分：按空行分段（段落），超长段落再退化为固定窗口。

    模拟"按标题/段落切"的结构感知策略；真实解析器（Markdown/PDF 标题树）留待接入。
    """
    if max_size <= 0:
        raise ValueError("max_size 必须为正")
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    for para in paragraphs:
        if len(para) <= max_size:
            chunks.append(para)
        else:
            # 超长段落退化为无重叠固定窗口（结构边界已由段落给出）。
            chunks.extend(chunk_fixed_window(para, size=max_size, overlap=0))
    return chunks


def ingest_document(
    store: ChunkStore,
    ctx: SecurityContext,
    *,
    title: str,
    text: str,
    scope: str,
    acl_group: str | None = None,
    strategy: str = "fixed",
    size: int = 200,
    overlap: int = 40,
) -> list[DocChunkRecord]:
    """把一篇文档切块并带 ACL 标签落库，返回写入的 chunk 记录。

    strategy: 'fixed'（固定窗口重叠）或 'structure'（结构感知）。归属标签
    （scope/acl_group）由调用方运行时注入，经 `add_chunk` 的 DB CHECK fail-closed 校验。
    """
    if strategy == "fixed":
        pieces = chunk_fixed_window(text, size=size, overlap=overlap)
    elif strategy == "structure":
        pieces = chunk_by_structure(text, max_size=size)
    else:
        raise ValueError(f"未知 chunking 策略: {strategy!r}")

    doc_id = store.register_document(ctx, title=title)
    records: list[DocChunkRecord] = []
    for idx, piece in enumerate(pieces):
        records.append(
            store.add_chunk(
                ctx,
                source_doc_id=doc_id,
                chunk_index=idx,
                content=piece,
                scope=scope,
                acl_group=acl_group,
            )
        )
    return records
