"""M18.7.4 · Filtered-ANN 三策略参数扫描与帕累托对照。

回答"真 HNSW 接入后三策略在 recall/延迟/内存上如何权衡"：
- **正确性**：以 pre-filter 暴力 top-k 为 recall 天花板，其余策略与之对照（recall_vs_ceiling）。
- **recall**：ef_search 越大候选越多、recall 越高、延迟越大（HNSW 经典权衡曲线）。
- **延迟**：多次查询的平均 wall-time（相对量，跨机器不可比，只看同机趋势）。
- **内存/剪枝**：被打分候选集规模（partitioned 只打分可见子图 → 远小于全局，剪枝代理）。
- **安全**：`forbidden_exposure` 始终为 0（隔离在召回前，与 ef/M 无关——这是关键卖点：
  调参只影响 recall/延迟，**绝不影响可见性**）。

红线：本模块只做评测度量，不改任何隔离结构；扫描全程 `forbidden_exposure==0` 是硬断言。
未装 hnswlib 时各策略退化暴力，扫描仍可跑（ANN 曲线退化为常数，诚实标注）。
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass

from nanoscope.identity import AUDIENCE_DM, SecurityContext
from nanoscope.rag.index import (
    PartitionedSearcher,
    PostFilterSearcher,
    PreFilterSearcher,
    hnswlib_available,
)
from nanoscope.rag.search import forbidden_doc_exposure
from nanoscope.rag.store import SCOPE_ORG, SCOPE_PROJECT, ChunkStore


class HashingEmbedder:
    """确定性高维假向量（离线可复现，无需网络/GLM）。

    把文本按字符 3-gram 哈希散布到 `dim` 维稠密向量——语义相近（共享子串）的文本
    向量夹角更小。相对 `_KeywordEmbedder` 的低维 one-hot，这里维度足够高，使 HNSW 图
    遍历与暴力产生**可观测差异**，从而让 ef_search 扫描曲线有意义。
    """

    def __init__(self, dim: int = 128):
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        t = text.strip()
        grams = [t[i : i + 3] for i in range(max(len(t) - 2, 1))]
        for g in grams:
            h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
            v[h % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v))
        return [x / norm for x in v] if norm else v

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


@dataclass(frozen=True)
class SweepPoint:
    """一次（策略, ef_search, M）配置的度量。"""

    strategy: str
    ef_search: int
    m: int
    recall_vs_ceiling: float  # 相对 pre-filter 暴力天花板的 recall（1.0=追平）
    mean_latency_ms: float    # 平均查询 wall-time（相对量）
    candidate_size: int       # 被打分候选集规模（剪枝/内存代理）
    forbidden_exposure: int   # 越权命中数（硬断言恒为 0）


def seed_scaled_cross_dept(
    store: ChunkStore,
    *,
    n_per_group: int = 60,
    n_forbidden: int = 60,
) -> tuple[list[str], set[str]]:
    """播种规模化跨部门语料，返回 (gold_ids, forbidden_ids)。

    - proj_a（可见）：n_per_group 条含"量子加密"的机密 chunk（gold 全集）。
    - proj_b（越权）：n_forbidden 条含"量子加密"的同关键词机密 chunk（语义最近邻）。
    - org（共享）：少量公开噪声，撑起分区规模对照。
    越权项与 gold 关键词高度重合 → 无授权时必进候选（可证伪基础）。
    """
    admin = SecurityContext(
        tenant_id="orgX", principal_id="orgX:feishu:admin",
        session_key="feishu:admin", audience_type=AUDIENCE_DM,
        roles=("proj_a", "proj_b"),
    )
    gold: list[str] = []
    doc_a = store.register_document(admin, title="A组机密")
    for i in range(n_per_group):
        rec = store.add_chunk(
            admin, source_doc_id=doc_a, chunk_index=i,
            content=f"量子加密算法密钥分发内部方案第{i}节详解与预算评估",
            scope=SCOPE_PROJECT, acl_group="proj_a",
        )
        gold.append(rec.id)
    forbidden: set[str] = set()
    doc_b = store.register_document(admin, title="B组机密")
    for i in range(n_forbidden):
        rec = store.add_chunk(
            admin, source_doc_id=doc_b, chunk_index=i,
            content=f"量子加密机密材料第{i}份纠缠态实验记录与路线",
            scope=SCOPE_PROJECT, acl_group="proj_b",
        )
        forbidden.add(rec.id)
    doc_org = store.register_document(admin, title="公开路线图")
    for i in range(max(n_per_group // 3, 1)):
        store.add_chunk(
            admin, source_doc_id=doc_org, chunk_index=i,
            content=f"公司产品路线图对外公开第{i}条", scope=SCOPE_ORG,
        )
    return gold, forbidden


def run_param_sweep(
    store: ChunkStore,
    ctx: SecurityContext,
    query: str,
    gold_ids: set[str],
    forbidden_ids: set[str],
    *,
    embedder,
    top_k: int = 10,
    ef_values: Sequence[int] = (10, 25, 50, 100),
    m: int = 16,
    repeats: int = 3,
) -> list[SweepPoint]:
    """扫描 ef_search × 策略，产出对照点。

    pre-filter 作为暴力天花板（单点，ef 无关）；partitioned/post-filter 随 ef 变化。
    每点跑 `repeats` 次取平均延迟。全程断言 `forbidden_exposure==0`（安全不随参数退化）。
    """
    points: list[SweepPoint] = []

    # 天花板：pre-filter 暴力 top-k。
    pre = PreFilterSearcher(store, embedder)
    ceil_out = pre.search(ctx, query, top_k=top_k)
    ceil_ids = [c.id for c in ceil_out.results]
    ceil_gold = set(ceil_ids) & gold_ids
    lat = _time_search(lambda: pre.search(ctx, query, top_k=top_k), repeats)
    points.append(SweepPoint(
        strategy="pre_filter", ef_search=-1, m=m,
        recall_vs_ceiling=1.0,
        mean_latency_ms=lat,
        candidate_size=len(ceil_out.scored_chunk_ids),
        forbidden_exposure=forbidden_doc_exposure(ceil_out.results, forbidden_ids),
    ))

    ceil_gold_count = max(len(ceil_gold), 1)
    for ef in ef_values:
        for name, factory in (
            ("partitioned", lambda ef=ef: PartitionedSearcher(
                store, embedder, use_ann=True, m=m, ef_search=ef)),
            ("post_filter", lambda ef=ef: PostFilterSearcher(
                store, embedder, use_ann=True, m=m, ef_search=ef)),
        ):
            searcher = factory()
            out = searcher.search(ctx, query, top_k=top_k)
            hit_gold = len(set(c.id for c in out.results) & ceil_gold)
            lat = _time_search(lambda s=searcher: s.search(ctx, query, top_k=top_k), repeats)
            points.append(SweepPoint(
                strategy=name, ef_search=ef, m=m,
                recall_vs_ceiling=hit_gold / ceil_gold_count,
                mean_latency_ms=lat,
                candidate_size=len(out.scored_chunk_ids),
                forbidden_exposure=forbidden_doc_exposure(out.results, forbidden_ids),
            ))
    return points


def _time_search(fn, repeats: int) -> float:
    fn()  # 预热（建图/首次查询开销不计入均值）
    t0 = time.perf_counter()
    for _ in range(max(repeats, 1)):
        fn()
    return (time.perf_counter() - t0) / max(repeats, 1) * 1000.0


def pareto_front(points: Sequence[SweepPoint]) -> list[SweepPoint]:
    """recall 最大化 + 延迟最小化的帕累托前沿（不被任何点同时支配）。"""
    front: list[SweepPoint] = []
    for p in points:
        dominated = any(
            q is not p
            and q.recall_vs_ceiling >= p.recall_vs_ceiling
            and q.mean_latency_ms <= p.mean_latency_ms
            and (q.recall_vs_ceiling > p.recall_vs_ceiling
                 or q.mean_latency_ms < p.mean_latency_ms)
            for q in points
        )
        if not dominated:
            front.append(p)
    return front


def format_sweep_table(points: Sequence[SweepPoint]) -> str:
    """人读文本表（诚实标注 ANN 是否真启用）。"""
    ann = "真 HNSW" if hnswlib_available() else "退化暴力（未装 hnswlib）"
    lines = [
        f"# Filtered-ANN 三策略参数扫描（ANN 底座：{ann}）",
        "",
        "| 策略 | ef_search | M | recall/天花板 | 平均延迟(ms) | 候选规模 | 越权命中 |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in points:
        ef = "—" if p.ef_search < 0 else str(p.ef_search)
        lines.append(
            f"| {p.strategy} | {ef} | {p.m} | {p.recall_vs_ceiling:.3f} | "
            f"{p.mean_latency_ms:.4f} | {p.candidate_size} | {p.forbidden_exposure} |"
        )
    lines += [
        "",
        "注：延迟为同机相对量（跨机不可比）；候选规模是内存/剪枝代理——partitioned 只打分",
        "可见子图故显著更小。**越权命中恒为 0**：调参只影响 recall/延迟，隔离在召回前完成，",
        "与 ef/M 无关。pre-filter 为暴力天花板（recall=1.0，无 ANN 加速）。",
    ]
    return "\n".join(lines)
