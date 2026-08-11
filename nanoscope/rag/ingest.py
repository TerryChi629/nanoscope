"""M18 · 文档 ingestion pipeline (PRD_v4 §M18.2 第 2 点)。

chunk → (embed 由检索器按需) → 带 ACL 标签落库。本模块负责 **chunking + 落库**，
embedding 在检索侧按需批量取（复用 M7 `GlmEmbedder`，凭证走环境变量）。

红线：chunk 的 tenant/scope/owner/acl 标签由调用方（运行时上下文）注入，
`ingest_document` 只透传给 `ChunkStore.add_chunk`，模型/文档内容无法伪造归属。
"""

from __future__ import annotations

from dataclasses import dataclass

from nanoscope.identity import SecurityContext
from nanoscope.rag.store import ChunkStore, DocChunkRecord


@dataclass(frozen=True)
class ChunkSpan:
    """一个可追溯回原文的 chunk，区间采用左闭右开字符下标。"""

    start: int
    end: int
    content: str

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError("chunk span 必须满足 0 <= start < end")
        if len(self.content) != self.end - self.start:
            raise ValueError("chunk content 长度必须与字符区间一致")


def _trimmed_bounds(text: str) -> tuple[int, int]:
    start = len(text) - len(text.lstrip())
    end = len(text.rstrip())
    return start, end


def _fixed_window_spans(
    text: str,
    *,
    size: int,
    overlap: int,
    base_offset: int = 0,
) -> list[ChunkSpan]:
    start, end = _trimmed_bounds(text)
    if start >= end:
        return []
    step = size - overlap
    chunks: list[ChunkSpan] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + size, end)
        content = text[cursor:chunk_end]
        if content.strip():
            chunks.append(
                ChunkSpan(
                    start=base_offset + cursor,
                    end=base_offset + chunk_end,
                    content=content,
                )
            )
        if chunk_end >= end:
            break
        cursor += step
    return chunks


def chunk_fixed_window_spans(
    text: str,
    *,
    size: int = 200,
    overlap: int = 40,
) -> list[ChunkSpan]:
    """固定窗口切分，并保留每个 chunk 在原文中的字符区间。"""
    if size <= 0:
        raise ValueError("size 必须为正")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap 必须在 [0, size) 内")
    return _fixed_window_spans(text, size=size, overlap=overlap)


def chunk_fixed_window(text: str, *, size: int = 200, overlap: int = 40) -> list[str]:
    """固定窗口 + 重叠切分。

    以字符为单位（对 CJK 友好，无需分词器）。相邻 chunk 重叠 `overlap` 字符以保上下文
    连续。size<=0 或 overlap>=size 视为配置错误（fail-closed 抛错，不静默退化）。
    """
    return [chunk.content for chunk in chunk_fixed_window_spans(text, size=size, overlap=overlap)]


def chunk_by_structure_spans(text: str, *, max_size: int = 400) -> list[ChunkSpan]:
    """按空行分段并保留原文区间，超长段落退化为无重叠固定窗口。"""
    if max_size <= 0:
        raise ValueError("max_size 必须为正")
    chunks: list[ChunkSpan] = []
    cursor = 0
    for raw_paragraph in text.split("\n\n"):
        raw_start = cursor
        cursor += len(raw_paragraph) + 2
        local_start, local_end = _trimmed_bounds(raw_paragraph)
        if local_start >= local_end:
            continue
        paragraph = raw_paragraph[local_start:local_end]
        paragraph_start = raw_start + local_start
        if len(paragraph) <= max_size:
            chunks.append(
                ChunkSpan(
                    start=paragraph_start,
                    end=paragraph_start + len(paragraph),
                    content=paragraph,
                )
            )
        else:
            chunks.extend(
                _fixed_window_spans(
                    paragraph,
                    size=max_size,
                    overlap=0,
                    base_offset=paragraph_start,
                )
            )
    return chunks


def chunk_by_structure(text: str, *, max_size: int = 400) -> list[str]:
    """结构感知切分：按空行分段（段落），超长段落再退化为固定窗口。

    模拟"按标题/段落切"的结构感知策略；真实解析器（Markdown/PDF 标题树）留待接入。
    """
    return [chunk.content for chunk in chunk_by_structure_spans(text, max_size=max_size)]


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
