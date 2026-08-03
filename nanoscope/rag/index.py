"""M18 · Filtered-ANN 三策略与分区索引 (PRD_v4 §M18.7)。

把项目安全约束（先 ACL 后检索）与向量检索物理结构（预建全局图）的冲突正面解决。
ACL 过滤这把"筛子"相对"图遍历"在时间轴上只能放三个位置，由此穷举出**完备三策略**：

| 策略           | 筛子位置 | 机制                                  | 代价 |
|----------------|----------|---------------------------------------|------|
| Post-filter    | 遍历之后 | 先全局取 top-k，再删越权项            | ❌ 破 recall + **越权向量已进候选**，违背红线，反面对照 |
| Pre-filter     | 遍历之前 | 先 WHERE 选可见子集，再暴力算余弦     | ✅ 正确安全，丢失 ANN 加速（O(N·可见率)） |
| Partitioned    | 建索引阶段 | 按 ACL group 各建子图，只搜可见子图 | ✅ 速度+正确兼得，选定解 |

**完备性论证**：过滤动作相对图遍历只有"前/中/后"三个位置，故策略必然且只有三种。

**ANN 底座**：真 HNSW 近似最近邻索引（`hnswlib`，PRD_v4 §M18.7.3 第二步）作为**可选**
加速层，通过各策略的 `use_ann` 开关按需启用（默认关闭）：
- **pre-filter**：**永远暴力**——它是 recall 天花板与正确性基线（§M18.7.1），不接 ANN。
- **partitioned / post-filter**：`use_ann=True` 时各分区/全局子图建一张 HNSW 图，ANN 召回
  候选后仍用**精确余弦重打分 + 确定性 tie-break**，保证同一候选集下结果可复现。
- 未安装 hnswlib（`pip install 'nanobot[rag]'` 未执行）或 `use_ann=False` 时，全部策略
  自动退化为纯 Python 暴力余弦——`SearchOutcome` 契约与隔离结构逐字节不变。

红线：索引只承担"候选召回排序"，**永不承担可见性**——可见性永远由授权 WHERE
（pre/post）或分区结构（partitioned）保证。post-filter 之所以被否决，正因它让越权
向量进了候选并被打分。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from threading import RLock

from nanoscope.eval.embedding import Embedder
from nanoscope.identity import SecurityContext
from nanoscope.rag.store import SCOPE_ORG, SCOPE_PROJECT, ChunkStore, DocChunkRecord

# 真 ANN 索引（hnswlib）为**可选依赖**（`pip install 'nanobot[rag]'`）。未安装时全部
# 策略自动退化为纯 Python 暴力余弦（正确性基线，零依赖）——契约与隔离结构不变。
try:  # pragma: no cover - 依赖是否装到取决于环境
    import hnswlib  # type: ignore
    import numpy as _np  # hnswlib 需要 numpy 数组入参

    _HNSW_OK = True
except ImportError:  # pragma: no cover
    hnswlib = None  # type: ignore
    _np = None  # type: ignore
    _HNSW_OK = False


def hnswlib_available() -> bool:
    """真 ANN 索引是否可用。False 时各策略退化暴力余弦（正确性不变）。"""
    return _HNSW_OK


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _chunk_tie_key(chunk: DocChunkRecord) -> tuple:
    """Stable semantic ordering before the random UUID fallback."""

    return (
        chunk.tenant_id,
        chunk.scope,
        chunk.owner_id or "",
        chunk.acl_group or "",
        chunk.content,
        chunk.chunk_index,
        chunk.id,
    )


def _brute_topk(
    query_vec: Sequence[float],
    chunks: Sequence[DocChunkRecord],
    vecs: Sequence[Sequence[float]],
    top_k: int,
    *,
    min_score: float = 0.0,
) -> list[tuple[DocChunkRecord, float]]:
    """暴力余弦 top-k（recall 天花板）。min_score 过滤：全库无关时 abstain。"""
    scored = [
        (c, _cosine(query_vec, v))
        for c, v in zip(chunks, vecs)
    ]
    scored = [(c, s) for c, s in scored if s > min_score]
    scored.sort(key=lambda item: (-item[1], _chunk_tie_key(item[0])))
    return scored[:top_k]


# HNSW 默认超参（PRD_v4 §M18.7.4 参数扫描的基准点）。
_DEFAULT_M = 16
_DEFAULT_EF_CONSTRUCTION = 200
_DEFAULT_EF_SEARCH = 50
# ANN 召回相对 top_k 的放大倍数：多召回候选再精确重打分补 recall。
_ANN_FANOUT_MULT = 4


class _HnswIndex:
    """封装 `hnswlib.Index(space='cosine')`：建图 + 近邻候选召回。

    **只负责"给候选下标"**——精确重打分（`_brute_topk`）、min_score 过滤、可见性判定
    都在外层。可见性永远不经此类（红线：索引只排序）。未装 hnswlib 时不应被实例化。
    """

    def __init__(
        self,
        vecs: Sequence[Sequence[float]],
        *,
        m: int = _DEFAULT_M,
        ef_construction: int = _DEFAULT_EF_CONSTRUCTION,
        ef_search: int = _DEFAULT_EF_SEARCH,
    ):
        assert _HNSW_OK  # 调用方保证；未装 hnswlib 一律走暴力分支，不会到这里
        self._n = len(vecs)
        self._dim = len(vecs[0]) if self._n else 0
        self._index = hnswlib.Index(space="cosine", dim=self._dim)
        self._index.init_index(
            max_elements=max(self._n, 1), M=m, ef_construction=ef_construction
        )
        if self._n and self._dim:
            # num_threads=1：单线程建图保证可复现（多线程插入顺序非确定）。
            self._index.add_items(
                _np.asarray(vecs, dtype=_np.float32), _np.arange(self._n), num_threads=1
            )
        self._index.set_ef(max(ef_search, 1))

    def query(self, query_vec: Sequence[float], k: int) -> list[int]:
        """返回候选的**原始下标**（近邻优先），至多 k 个。"""
        if self._n == 0 or self._dim == 0:
            return []
        k = min(k, self._n)
        labels, _ = self._index.knn_query(
            _np.asarray([query_vec], dtype=_np.float32), k=k
        )
        return [int(i) for i in labels[0]]


def _corpus_topk(
    corpus: "_EmbeddedCorpus",
    query_vec: Sequence[float],
    top_k: int,
    *,
    min_score: float = 0.0,
) -> list[tuple[DocChunkRecord, float]]:
    """在一个已嵌入语料内取 top-k。

    有 ANN 图则近邻召回候选后再用 `_brute_topk` **精确重打分**；否则直接暴力。两条路径
    共用同一精确余弦 + 确定性 tie-break——ANN 只影响"看哪些候选"，不影响"候选内如何
    排序/过滤"，故同一候选集下结果逐字节可复现（D4 一致性的基础）。
    """
    if not corpus.chunks:
        return []
    if corpus.ann is None:
        return _brute_topk(query_vec, corpus.chunks, corpus.vecs, top_k, min_score=min_score)
    fanout = min(len(corpus.chunks), max(top_k * _ANN_FANOUT_MULT, top_k))
    cand_idx = corpus.ann.query(query_vec, fanout)
    cand_chunks = [corpus.chunks[i] for i in cand_idx]
    cand_vecs = [corpus.vecs[i] for i in cand_idx]
    return _brute_topk(query_vec, cand_chunks, cand_vecs, top_k, min_score=min_score)


@dataclass
class SearchOutcome:
    """一次检索的结果 + 可观测审计（供 F1/F3 断言）。

    - results：返回给用户的 chunk（相关性降序）。
    - scored_chunk_ids：**被打分/进入候选集**的 chunk id 集合（post-filter 会把越权项
      放进这里——这正是它违背红线的证据）。
    - accessed_partitions：本次实际访问（加载并搜索）的分区 acl_group 集合
      （partitioned 策略专用，F3 断言越权子图从未被访问）。
    """

    results: list[DocChunkRecord]
    scored_chunk_ids: set[str] = field(default_factory=set)
    accessed_partitions: set[str] = field(default_factory=set)


class _EmbeddedCorpus:
    """对一组 chunk 预取 embedding（一次批量），供各策略复用。

    `use_ann=True` 且 hnswlib 可用时额外建一张 HNSW 图（`self.ann`）；否则 `self.ann=None`，
    检索退化为暴力余弦。ANN 只承担候选召回，可见性与精确排序仍在外层（红线）。
    """

    def __init__(
        self,
        chunks: Sequence[DocChunkRecord],
        embedder: Embedder,
        *,
        use_ann: bool = False,
        m: int = _DEFAULT_M,
        ef_construction: int = _DEFAULT_EF_CONSTRUCTION,
        ef_search: int = _DEFAULT_EF_SEARCH,
    ):
        self.chunks = list(chunks)
        self.vecs = embedder.embed([c.content for c in self.chunks]) if self.chunks else []
        self.embedder = embedder
        self.ann: _HnswIndex | None = None
        if use_ann and _HNSW_OK and self.chunks:
            self.ann = _HnswIndex(
                self.vecs, m=m, ef_construction=ef_construction, ef_search=ef_search
            )


class PreFilterSearcher:
    """✅ 正确性基线：授权 WHERE 先过滤 → 在可见子集内暴力算余弦（PRD_v4 §M18.7.3 第一步）。

    越权 chunk 从一开始就不进候选集，天然满足"隔离在召回前"。代价是丢失 ANN 加速。
    """

    name = "pre_filter"

    def __init__(self, store: ChunkStore, embedder: Embedder):
        self._store = store
        self._embedder = embedder

    def search(self, ctx: SecurityContext, query: str, top_k: int = 5) -> SearchOutcome:
        visible = self._store.visible_chunks(ctx)  # 授权 WHERE 硬过滤（召回前）
        corpus = _EmbeddedCorpus(visible, self._embedder)
        if not corpus.chunks:
            return SearchOutcome(results=[])
        q = self._embedder.embed([query])[0]
        hits = _brute_topk(q, corpus.chunks, corpus.vecs, top_k)
        return SearchOutcome(
            results=[c for c, _ in hits],
            scored_chunk_ids={c.id for c in corpus.chunks},  # 只打分了可见集合
        )


class PostFilterSearcher:
    """❌ 反面对照（PRD_v4 §M18.7.1）：先全局图取 top-k，再删越权项。

    **故意保留**用于证明其两处硬伤：
    1. 越权向量已进候选并被打分（`scored_chunk_ids` 含越权 id）——违背"隔离在召回前"红线；
    2. 可见率低时 top-k 被越权项占满，删掉后 recall 崩塌。
    线上绝不使用；仅评测对照（D1/F1/F2 的反面证据）。
    """

    name = "post_filter"

    def __init__(
        self,
        store: ChunkStore,
        embedder: Embedder,
        *,
        use_ann: bool = False,
        m: int = _DEFAULT_M,
        ef_construction: int = _DEFAULT_EF_CONSTRUCTION,
        ef_search: int = _DEFAULT_EF_SEARCH,
    ):
        self._store = store
        self._embedder = embedder
        self._use_ann = use_ann
        self._m = m
        self._ef_construction = ef_construction
        self._ef_search = ef_search

    def search(self, ctx: SecurityContext, query: str, top_k: int = 5) -> SearchOutcome:
        all_chunks = self._store.all_chunks_unfiltered()  # ★ 越权项也进候选（硬伤所在）
        corpus = _EmbeddedCorpus(
            all_chunks, self._embedder, use_ann=self._use_ann,
            m=self._m, ef_construction=self._ef_construction, ef_search=self._ef_search,
        )
        if not corpus.chunks:
            return SearchOutcome(results=[])
        q = self._embedder.embed([query])[0]
        # 先全局取 top-k（越权向量被打分）。
        global_hits = _corpus_topk(corpus, q, top_k)
        scored = {c.id for c in corpus.chunks}
        # 再事后删越权项：用授权 WHERE 求可见 id，过滤 top-k。
        visible_ids = {c.id for c in self._store.visible_chunks(ctx)}
        filtered = [c for c, _ in global_hits if c.id in visible_ids]
        return SearchOutcome(results=filtered, scored_chunk_ids=scored)


class PartitionedSearcher:
    """✅ 选定解（PRD_v4 §M18.7.2）：按 ACL group 分区建索引，只搜用户可见子图。

    分区结构：{(tenant, org): 大图, (tenant, project, g): 子图,
    (tenant, user, owner): 子图}。查询时用户可见分区 = 同 tenant 的 org_shared
    ∪ 其 DM 个人分区 ∪ 其所属 project 子图；只在这几个
    分区跑 ANN，结果合并。越权子图**从未被加载/访问**（F3），隔离由分区结构保证。

    本项目 ACL 维度是 org/project（部门/项目，数量级几~几十），分区数天然可控——
    这是"敢选分区索引"的答辩点。fail-safe：分区数异常膨胀时退化为 pre-filter 暴力。
    """

    name = "partitioned"
    # fail-safe 阈值：分区数超此值退化为 pre-filter 暴力（正确性永远保底）。
    _MAX_PARTITIONS = 256

    def __init__(
        self,
        store: ChunkStore,
        embedder: Embedder,
        *,
        use_ann: bool = False,
        m: int = _DEFAULT_M,
        ef_construction: int = _DEFAULT_EF_CONSTRUCTION,
        ef_search: int = _DEFAULT_EF_SEARCH,
    ):
        self._store = store
        self._embedder = embedder
        self._use_ann = use_ann
        self._m = m
        self._ef_construction = ef_construction
        self._ef_search = ef_search
        self._partitions: dict[tuple[str, str, str | None], _EmbeddedCorpus] = {}
        self._revision = -1
        self._refresh_lock = RLock()
        self._build_partitions()

    @staticmethod
    def _partition_key(c: DocChunkRecord) -> tuple[str, str, str | None]:
        if c.scope == SCOPE_ORG:
            return (c.tenant_id, SCOPE_ORG, None)
        if c.scope == SCOPE_PROJECT:
            return (c.tenant_id, SCOPE_PROJECT, c.acl_group)
        return (c.tenant_id, "user", c.owner_id)

    @staticmethod
    def _audit_partition_key(key: tuple[str, str, str | None]) -> str:
        tenant_id, scope, owner = key
        suffix = owner if owner is not None else "shared"
        return f"tenant={tenant_id}|{scope}={suffix}"

    def _build_partitions(self) -> None:
        """建索引阶段：按分区 key 把全库 chunk 分桶，各桶独立建（可选）HNSW 子图。"""
        with self._refresh_lock:
            if self._revision == self._store.revision:
                return
            while True:
                revision_before = self._store.revision
                buckets: dict[tuple[str, str, str | None], list[DocChunkRecord]] = {}
                for c in self._store.all_chunks_unfiltered():
                    buckets.setdefault(self._partition_key(c), []).append(c)
                partitions = {
                    key: _EmbeddedCorpus(
                        chunks, self._embedder, use_ann=self._use_ann,
                        m=self._m, ef_construction=self._ef_construction,
                        ef_search=self._ef_search,
                    )
                    for key, chunks in buckets.items()
                }
                revision_after = self._store.revision
                if revision_before == revision_after:
                    self._partitions = partitions
                    self._revision = revision_after
                    return

    def _refresh_if_stale(self) -> None:
        """Atomically rebuild partitions when committed ingestion advances the revision."""
        if self._store.revision != self._revision:
            self._build_partitions()

    def _visible_partition_keys(self, ctx: SecurityContext) -> list[tuple[str, str, str | None]]:
        """用户可见分区 key = org_shared ∪ DM 个人分区 ∪ 所属 project 子图。"""
        keys = [(ctx.tenant_id, SCOPE_ORG, None)]
        if ctx.audience_type == "dm":
            keys.append((ctx.tenant_id, "user", ctx.principal_id))
        for g in (ctx.roles or ()):
            keys.append((ctx.tenant_id, SCOPE_PROJECT, g))
        return keys

    def search(self, ctx: SecurityContext, query: str, top_k: int = 5) -> SearchOutcome:
        self._refresh_if_stale()
        if len(self._partitions) > self._MAX_PARTITIONS:
            # fail-safe 降级：分区爆炸 → pre-filter 暴力保底（正确性不变）。
            return PreFilterSearcher(self._store, self._embedder).search(ctx, query, top_k)

        q = self._embedder.embed([query])[0]
        merged: list[tuple[DocChunkRecord, float]] = []
        accessed: set[str] = set()
        scored: set[str] = set()
        for key in self._visible_partition_keys(ctx):
            corpus = self._partitions.get(key)
            if corpus is None or not corpus.chunks:
                continue
            accessed.add(self._audit_partition_key(key))
            hits = _corpus_topk(corpus, q, top_k)
            merged.extend(hits)
            scored.update(c.id for c in corpus.chunks)
        merged.sort(key=lambda item: (-item[1], _chunk_tie_key(item[0])))
        results = [c for c, _ in merged[:top_k]]
        if any(c.tenant_id != ctx.tenant_id for c in results):
            raise RuntimeError("partitioned search tenant isolation violation")
        return SearchOutcome(
            results=results,
            scored_chunk_ids=scored,
            accessed_partitions=accessed,
        )
