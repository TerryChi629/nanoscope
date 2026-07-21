"""M18 · Filtered-ANN 三策略与分区索引 (PRD_v4 §M18.7)。

把项目安全约束（先 ACL 后检索）与向量检索物理结构（预建全局图）的冲突正面解决。
ACL 过滤这把"筛子"相对"图遍历"在时间轴上只能放三个位置，由此穷举出**完备三策略**：

| 策略           | 筛子位置 | 机制                                  | 代价 |
|----------------|----------|---------------------------------------|------|
| Post-filter    | 遍历之后 | 先全局取 top-k，再删越权项            | ❌ 破 recall + **越权向量已进候选**，违背红线，反面对照 |
| Pre-filter     | 遍历之前 | 先 WHERE 选可见子集，再暴力算余弦     | ✅ 正确安全，丢失 ANN 加速（O(N·可见率)） |
| Partitioned    | 建索引阶段 | 按 ACL group 各建子图，只搜可见子图 | ✅ 速度+正确兼得，选定解 |

**完备性论证**：过滤动作相对图遍历只有"前/中/后"三个位置，故策略必然且只有三种。

**当前实现**：ANN 底座用**纯 Python 暴力余弦**（正确性基线，PRD_v4 §M18.7.3 第一步，
零新依赖）。`hnswlib` 真近似索引留待接入（见下方 TODO），届时替换 `_ann_topk` 即可，
三策略的隔离结构与 `SearchOutcome` 契约不变。

红线：索引只承担"候选召回排序"，**永不承担可见性**——可见性永远由授权 WHERE
（pre/post）或分区结构（partitioned）保证。post-filter 之所以被否决，正因它让越权
向量进了候选并被打分。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from nanoscope.eval.embedding import Embedder
from nanoscope.identity import SecurityContext
from nanoscope.rag.store import SCOPE_ORG, SCOPE_PROJECT, ChunkStore, DocChunkRecord

# TODO(用户补 hnswlib)：接入 `hnswlib.Index(space='cosine')` 替换 `_brute_topk`，
# 为 partitioned 策略每个 acl_group 建独立子图；pre-filter 在可见子集上建临时图或暴力。
# 当前用暴力余弦保证正确性基线可离线跑，参数扫描（ef_search/M）待真索引接入后补 §M18.3.2。


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _brute_topk(
    query_vec: Sequence[float],
    chunks: Sequence[DocChunkRecord],
    vecs: Sequence[Sequence[float]],
    top_k: int,
    *,
    min_score: float = 0.0,
) -> list[tuple[DocChunkRecord, float]]:
    """暴力余弦 top-k（ANN 底座占位）。min_score 过滤：全库无关时 abstain。"""
    scored = [
        (c, _cosine(query_vec, v))
        for c, v in zip(chunks, vecs)
    ]
    scored = [(c, s) for c, s in scored if s > min_score]
    scored.sort(key=lambda cs: (-cs[1], cs[0].id))
    return scored[:top_k]


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
    """对一组 chunk 预取 embedding（一次批量），供各策略复用。"""

    def __init__(self, chunks: Sequence[DocChunkRecord], embedder: Embedder):
        self.chunks = list(chunks)
        self.vecs = embedder.embed([c.content for c in self.chunks]) if self.chunks else []
        self.embedder = embedder


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

    def __init__(self, store: ChunkStore, embedder: Embedder):
        self._store = store
        self._embedder = embedder

    def search(self, ctx: SecurityContext, query: str, top_k: int = 5) -> SearchOutcome:
        all_chunks = self._store.all_chunks_unfiltered()  # ★ 越权项也进候选（硬伤所在）
        corpus = _EmbeddedCorpus(all_chunks, self._embedder)
        if not corpus.chunks:
            return SearchOutcome(results=[])
        q = self._embedder.embed([query])[0]
        # 先全局取 top-k（越权向量被打分）。
        global_hits = _brute_topk(q, corpus.chunks, corpus.vecs, top_k)
        scored = {c.id for c in corpus.chunks}
        # 再事后删越权项：用授权 WHERE 求可见 id，过滤 top-k。
        visible_ids = {c.id for c in self._store.visible_chunks(ctx)}
        filtered = [c for c, _ in global_hits if c.id in visible_ids]
        return SearchOutcome(results=filtered, scored_chunk_ids=scored)


class PartitionedSearcher:
    """✅ 选定解（PRD_v4 §M18.7.2）：按 ACL group 分区建索引，只搜用户可见子图。

    分区结构：{org_shared: 大图, project_<g>: 子图, user_<owner>: 子图}。查询时
    用户可见分区 = org_shared ∪ 其 DM 个人分区 ∪ 其所属 project 子图；只在这几个
    分区跑 ANN，结果合并。越权子图**从未被加载/访问**（F3），隔离由分区结构保证。

    本项目 ACL 维度是 org/project（部门/项目，数量级几~几十），分区数天然可控——
    这是"敢选分区索引"的答辩点。fail-safe：分区数异常膨胀时退化为 pre-filter 暴力。
    """

    name = "partitioned"
    # fail-safe 阈值：分区数超此值退化为 pre-filter 暴力（正确性永远保底）。
    _MAX_PARTITIONS = 256

    def __init__(self, store: ChunkStore, embedder: Embedder):
        self._store = store
        self._embedder = embedder
        self._partitions: dict[str, _EmbeddedCorpus] = {}
        self._build_partitions()

    @staticmethod
    def _partition_key(c: DocChunkRecord) -> str:
        if c.scope == SCOPE_ORG:
            return "org_shared"
        if c.scope == SCOPE_PROJECT:
            return f"project_{c.acl_group}"
        return f"user_{c.owner_id}"  # SCOPE_USER

    def _build_partitions(self) -> None:
        """建索引阶段：按分区 key 把全库 chunk 分桶，各桶独立预取 embedding。"""
        buckets: dict[str, list[DocChunkRecord]] = {}
        for c in self._store.all_chunks_unfiltered():
            buckets.setdefault(self._partition_key(c), []).append(c)
        self._partitions = {
            key: _EmbeddedCorpus(chunks, self._embedder)
            for key, chunks in buckets.items()
        }

    def _visible_partition_keys(self, ctx: SecurityContext) -> list[str]:
        """用户可见分区 key = org_shared ∪ DM 个人分区 ∪ 所属 project 子图。"""
        keys = ["org_shared"]
        if ctx.audience_type == "dm":
            keys.append(f"user_{ctx.principal_id}")
        for g in (ctx.roles or ()):
            keys.append(f"project_{g}")
        return keys

    def search(self, ctx: SecurityContext, query: str, top_k: int = 5) -> SearchOutcome:
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
            accessed.add(key)  # ★ 只访问可见分区
            hits = _brute_topk(q, corpus.chunks, corpus.vecs, top_k)
            merged.extend(hits)
            scored.update(c.id for c in corpus.chunks)
        merged.sort(key=lambda cs: (-cs[1], cs[0].id))
        return SearchOutcome(
            results=[c for c, _ in merged[:top_k]],
            scored_chunk_ids=scored,
            accessed_partitions=accessed,
        )
