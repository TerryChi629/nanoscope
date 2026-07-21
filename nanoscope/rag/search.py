"""M18 · 权限感知文档检索单入口 (PRD_v4 §M18.2 第 4 点)。

`doc_search_visible(ctx, query, top_k)` 对齐 `Repository.search_visible` 契约：
授权 WHERE 先过滤 → 可见集合内 BM25 + 向量(ANN) + RRF 融合 → rerank → 返回 top_k。
**隔离在召回前**，红线不破。

红线：与记忆检索并存、不替换。可见性由 `ChunkStore.visible_where` / 分区结构确定，
检索器只排序。检索到的 chunk 若要注入 prompt，须经 M12 `wrap_untrusted_memory`
不可信数据块包裹（防注入红线一致，D7）。
"""

from __future__ import annotations

from collections.abc import Sequence

from nanoscope.eval.embedding import Embedder
from nanoscope.eval.retrieval import Bm25Retriever, Doc, VectorRetriever, rrf_fuse
from nanoscope.identity import SecurityContext
from nanoscope.rag.index import PreFilterSearcher
from nanoscope.rag.rerank import Reranker, StubReranker
from nanoscope.rag.store import ChunkStore, DocChunkRecord


def doc_search_visible(
    store: ChunkStore,
    ctx: SecurityContext,
    query: str,
    *,
    embedder: Embedder,
    top_k: int = 5,
    reranker: Reranker | None = None,
    fanout: int = 20,
) -> list[DocChunkRecord]:
    """权限感知文档检索：授权 WHERE 召回前过滤 → BM25+向量 RRF 融合 → rerank → top_k。

    1. 授权 WHERE 先取可见 chunk 集合（越权 chunk 从不进候选，隔离在召回前）。
    2. 在可见集合内：BM25（trigram）+ 向量（ANN 底座 = pre-filter 暴力）各出一路 ranking。
    3. RRF 融合两路（复用 M14 硬化融合：确定性 tie-break + 空输入 abstain）。
    4. reranker 对融合 top-N 做 cross-encoder 重排（默认 StubReranker）。
    5. 返回 top_k 对应的 chunk 记录。
    """
    visible = store.visible_chunks(ctx)
    if not visible:
        return []
    by_id = {c.id: c for c in visible}
    docs = [Doc(id=c.id, content=c.content, created_at=c.created_at) for c in visible]

    # 向量路：复用 pre-filter 语义（在可见集合内暴力余弦），底层同 M14 VectorRetriever。
    vec = VectorRetriever(docs, embedder)
    vec_ranking = vec.search(query, fanout)

    # BM25 路：可见集合内 trigram 相关性排序。
    bm25 = Bm25Retriever(docs)
    try:
        bm25_ranking = bm25.search(query, fanout)
    finally:
        bm25.close()

    fused = rrf_fuse([bm25_ranking, vec_ranking], top_k=max(top_k, fanout))
    if not fused:
        return []  # abstention：两路皆空。

    # rerank：对融合候选做 cross-encoder 重排。
    rr = reranker or StubReranker()
    cand_ids = fused[: max(top_k, fanout)]
    cand_texts = [by_id[cid].content for cid in cand_ids]
    order = rr.rerank(query, cand_texts)
    reranked_ids = [cand_ids[i] for i in order]
    return [by_id[cid] for cid in reranked_ids[:top_k]]


def forbidden_doc_exposure(
    results: Sequence[DocChunkRecord], forbidden_chunk_ids: set[str]
) -> int:
    """检索结果里命中了多少条越权（禁止对该用户可见）chunk。目标 = 0（D1/M18.3 第 4 点）。

    等价于把 M6 的 `forbidden_prompt_exposure==0` 扩展到文档通道。
    """
    return sum(1 for r in results if r.id in forbidden_chunk_ids)


# 暴露 PreFilterSearcher 作为 doc_search_visible 的"纯向量路"等价实现，便于评测对照。
__all__ = ["doc_search_visible", "forbidden_doc_exposure", "PreFilterSearcher"]
