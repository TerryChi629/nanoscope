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

**当前交付状态**：
- 已交付：`ChunkStore`（scope∈{user,org,project} + DB CHECK fail-closed）、chunking
  （固定窗口重叠 + 结构感知）、`doc_search_visible`（授权 WHERE 召回前过滤 + BM25/向量/RRF
  + rerank）、Filtered-ANN 三策略（pre-filter / post-filter 反面对照 / partitioned 选定解）、
  真 `hnswlib` HNSW 索引（可选加速层，`use_ann=True` 启用；未装时退化暴力余弦，契约不变）、
  ef_search/M 参数扫描与帕累托对照（`sweep.py`，§M18.7.4）。
- 待用户补：脱敏真实机密语料（当前用程序合成跨部门语料演示）。GLM embedding 凭证走
  环境变量 `GLM_API_KEY`（`nanoscope.eval.embedding.GlmEmbedder`）。
"""

from __future__ import annotations

from nanoscope.rag.calibration import PlattCalibrator
from nanoscope.rag.fusion import FusionExample, LinearFusionModel
from nanoscope.rag.index import (
    PartitionedSearcher,
    PostFilterSearcher,
    PreFilterSearcher,
    SearchOutcome,
    hnswlib_available,
)
from nanoscope.rag.ingest import chunk_by_structure, chunk_fixed_window, ingest_document
from nanoscope.rag.rerank import SiliconFlowReranker, StubReranker
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
    "hnswlib_available",
    "StubReranker",
    "SiliconFlowReranker",
    "doc_search_visible",
    "FusionExample",
    "LinearFusionModel",
    "PlattCalibrator",
    "forbidden_doc_exposure",
]
