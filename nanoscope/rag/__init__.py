"""M18 · 权限感知机密文档 RAG 子包 (PRD_v4 §M18)。

**框定纪律**：这不是"再搭一个通用 RAG"，而是把 §M11 的 principal/audience 隔离墙、
M14 硬化后的检索器，从"短记忆条目"延伸到"长文档 chunk"。卖点是**权限感知检索**——
业界绝大多数 RAG 默认全员可见，本子包的检索在向量近邻之前先过授权 WHERE。

**红线（不可违反）**：
- 与记忆检索**并存、不替换、不合并**——绝不改动或下线 `Repository.search_visible`
  与 loop.py 的 `_scoped_memory_for_message`。文档 RAG 是平行的新增子包。
- 可见性 = 确定性授权（SQL WHERE + DB CHECK + 分区结构），**索引只承担候选排序，
  永不承担可见性**。隔离必须在召回前，所有新增判定 fail-closed。
- 密钥零入库：embedding 凭证只走环境变量（复用 `nanoscope.eval.embedding.GlmEmbedder`）；
  真实机密文档绝不入库，仓库内只用程序合成语料。

**当前交付状态（框架先搭）**：
- 已交付：`ChunkStore`（scope∈{user,org,project} + DB CHECK fail-closed）、chunking
  （固定窗口重叠 + 结构感知）、`doc_search_visible`（授权 WHERE 召回前过滤 + BM25/向量/RRF
  + rerank）、Filtered-ANN 三策略（pre-filter / post-filter 反面对照 / partitioned 选定解）。
- 待用户补：真实 GLM embedding 凭证（环境变量）、脱敏机密语料；`hnswlib` 真 ANN
  索引（当前用纯 Python 暴力余弦作正确性基线，见 `index.py` 的 TODO）。
"""

from __future__ import annotations

from nanoscope.rag.index import (
    PartitionedSearcher,
    PostFilterSearcher,
    PreFilterSearcher,
    SearchOutcome,
)
from nanoscope.rag.ingest import chunk_by_structure, chunk_fixed_window, ingest_document
from nanoscope.rag.rerank import StubReranker
from nanoscope.rag.search import doc_search_visible, forbidden_doc_exposure
from nanoscope.rag.store import (
    SCOPE_ORG,
    SCOPE_PROJECT,
    SCOPE_USER,
    ChunkStore,
    DocChunkRecord,
)

__all__ = [
    "ChunkStore",
    "DocChunkRecord",
    "SCOPE_USER",
    "SCOPE_ORG",
    "SCOPE_PROJECT",
    "chunk_fixed_window",
    "chunk_by_structure",
    "ingest_document",
    "PreFilterSearcher",
    "PostFilterSearcher",
    "PartitionedSearcher",
    "SearchOutcome",
    "StubReranker",
    "doc_search_visible",
    "forbidden_doc_exposure",
]
