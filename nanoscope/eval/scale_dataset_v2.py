"""规模曲线 v2：Zipf 自然增长 + 多种子置信区间 (PRD_v4 §M15，修 H4)。

v1（`scale_dataset.py`，保留供对照）用手工 `_BURY_AT=[15,40,…]` **预设每个 gold 在
哪个规模被埋**——曲线证明的是"到达手工阈值后 grep 会掉"，M₀ 本质是数据生成参数而非
系统拐点。v2 消除这一硬伤：

1. **主题热度 Zipf 分布**：主题 t 的干扰生成强度 λ_t ∝ 1/(t+1)^s（s≈1），模拟真实 IM
   中少数话题高频、长尾话题低频。
2. **干扰随 N 连续增长**：规模 N 时同主题 look-alike 干扰数以 λ_t · N 为中心，
   叠加每个 seed 的对数正态热度扰动，模拟不同时间窗的话题突发。
3. **候选集与 gold rank 可观测**：每个 query 记录候选集大小、gold 实际 rank。
4. **多种子 + 置信区间**：每档规模跑 R≥5 个种子，报告 Recall@K 均值 ± 标准差（95% CI）。

诚实性：这是**合成集**，用于隔离"相关性排序 vs 近邻窗口"这一唯一变量；拐点以带 CI 的
区间表述，不写单点绝对值。
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass

from nanoscope.eval.evaluator import EvalQuery, evaluate
from nanoscope.eval.retrieval import Bm25Retriever, Doc, GrepRetriever
from nanoscope.eval.scale_dataset import (
    _DISTRACTOR_FRAMES,
    _GOLD_SPECS,
    ScaleDoc,
    ScaleQuery,
)

# 干扰密度基准：主题 t 的 look-alike 干扰数期望 = zipf_lambda(t) · N。
# density 选 0.02、s=1：热门主题(t=0) λ=0.02，长尾(t=7) λ=0.0025，随 N 连续增长。
_DEFAULT_DENSITY = 0.02
_DEFAULT_S = 1.0
_DEFAULT_BURST_SIGMA = 0.45


def zipf_lambda(t: int, density: float = _DEFAULT_DENSITY, s: float = _DEFAULT_S) -> float:
    """主题 t 的干扰生成强度 λ_t = density / (t+1)^s（Zipf 热度衰减）。"""
    return density / ((t + 1) ** s)


def build_at_scale_v2(
    n: int,
    seed: int,
    density: float = _DEFAULT_DENSITY,
    s: float = _DEFAULT_S,
    burst_sigma: float = _DEFAULT_BURST_SIGMA,
) -> tuple[list[ScaleDoc], list[ScaleQuery]]:
    """在规模 N 下按 Zipf 连续增长合成语料 + 查询（无手工掩埋阈值）。

    机制（可证伪）：主题 t 的 look-alike 干扰数以 zipf_lambda(t) · N 为期望，随 N
    连续增长。gold 最旧（created_at 小），干扰更新——grep 按新近度排序，同主题干扰
    数超过 top_k 时把 gold 挤出窗口（漏召回）；BM25 按相关性排序，gold 子串重合度最高
    稳居前列。二者交叉即拐点，且拐点由真实干扰密度决定，而非预设常量。
    """
    rng = random.Random(seed)
    docs: list[ScaleDoc] = []
    queries: list[ScaleQuery] = []

    # gold 事实：created_at 从 0 起（最旧）。
    for i, (_topic, content, query) in enumerate(_GOLD_SPECS):
        gid = f"gold-{i}"
        docs.append(ScaleDoc(id=gid, content=content, created_at=i))
        queries.append(ScaleQuery(query=query, gold_ids=[gid]))

    # Zipf 连续增长：随机舍入保持 E[count]=λ_t·N，同时让不同 seed 真正扰动检索难度。
    ts = 1000  # 干扰起始 created_at，恒大于 gold。
    for t, (topic, _c, _q) in enumerate(_GOLD_SPECS):
        # E[lognormal(-σ²/2, σ)] = 1，故多种子平均强度仍为 λ_t·N，
        # 但单轮能出现真实话题突发，使 CI 反映 workload 变化而非计时噪声。
        burst = rng.lognormvariate(-(burst_sigma ** 2) / 2, burst_sigma)
        expected = zipf_lambda(t, density, s) * n * burst
        count = math.floor(expected)
        if rng.random() < expected - count:
            count += 1
        for r in range(count):
            if len(docs) >= n:
                break
            if rng.random() < 0.08:
                content = f"他人只是复述问题“{_q}”，没有提供可验证答案"
            else:
                frame = rng.choice(_DISTRACTOR_FRAMES)
                content = frame.format(noise=topic)
            content += f" 编号{rng.randint(1000, 999999)}"
            docs.append(ScaleDoc(id=f"dist-{t}-{r}", content=content, created_at=ts))
            ts += 1

    # inert 噪声填满规模：不含任何主题词，不进任何 query 的召回候选集。
    while len(docs) < n:
        docs.append(
            ScaleDoc(
                id=f"inert-{ts}",
                content=f"随手记的一条杂事编号{rng.randint(1000, 999999)}",
                created_at=ts,
            )
        )
        ts += 1

    return docs, queries


@dataclass(frozen=True)
class SeedStat:
    """一个检索器在多种子上的 Recall@K 聚合：均值 ± 标准差 + 95% CI。"""

    values: list[float]

    @property
    def mean(self) -> float:
        return statistics.fmean(self.values) if self.values else 0.0

    @property
    def std(self) -> float:
        return statistics.stdev(self.values) if len(self.values) > 1 else 0.0

    @property
    def ci95(self) -> float:
        """95% 置信区间半宽 = 1.96 · std / sqrt(n)。"""
        n = len(self.values)
        if n <= 1:
            return 0.0
        return 1.96 * self.std / math.sqrt(n)


@dataclass(frozen=True)
class ScalePointV2:
    n: int
    grep: SeedStat
    bm25: SeedStat
    gold_ranks: list[int]  # 各 (seed, query) 下 gold 的 grep rank（0=未召回）
    candidate_sizes: list[int]  # 各 (seed, query) 下 grep 候选集大小


def _gold_rank_and_candidates(
    grep: GrepRetriever, queries: list[EvalQuery], big_k: int
) -> tuple[list[int], list[int]]:
    """对每个 query 返回 gold 的 grep rank（1-based，0=未召回）与候选集大小。"""
    ranks: list[int] = []
    sizes: list[int] = []
    for q in queries:
        ranked = grep.search(q.query, big_k)
        sizes.append(len(ranked))
        gold = set(q.gold_ids)
        rank = 0
        for i, doc_id in enumerate(ranked, start=1):
            if doc_id in gold:
                rank = i
                break
        ranks.append(rank)
    return ranks, sizes


def run_curve_v2(
    scales: list[int],
    seeds: list[int],
    k: int = 5,
    density: float = _DEFAULT_DENSITY,
    s: float = _DEFAULT_S,
    burst_sigma: float = _DEFAULT_BURST_SIGMA,
) -> list[ScalePointV2]:
    """在各规模上跑多种子 A/B，聚合 Recall@K 均值/方差/CI + gold rank 分布。"""
    points: list[ScalePointV2] = []
    for n in scales:
        grep_vals: list[float] = []
        bm25_vals: list[float] = []
        all_ranks: list[int] = []
        all_sizes: list[int] = []
        for seed in seeds:
            docs, qspecs = build_at_scale_v2(n, seed, density, s, burst_sigma)
            eval_docs = [Doc(id=d.id, content=d.content, created_at=d.created_at) for d in docs]
            queries = [
                EvalQuery(query=q.query, gold_ids=frozenset(q.gold_ids)) for q in qspecs
            ]
            grep = GrepRetriever(eval_docs)
            bm25 = Bm25Retriever(eval_docs)
            try:
                grep_vals.append(evaluate(grep, queries, k=k).recall_at_k)
                bm25_vals.append(evaluate(bm25, queries, k=k).recall_at_k)
                ranks, sizes = _gold_rank_and_candidates(grep, queries, big_k=max(k, 50))
                all_ranks.extend(ranks)
                all_sizes.extend(sizes)
            finally:
                bm25.close()
        points.append(
            ScalePointV2(
                n=n,
                grep=SeedStat(grep_vals),
                bm25=SeedStat(bm25_vals),
                gold_ranks=all_ranks,
                candidate_sizes=all_sizes,
            )
        )
    return points


def find_crossover_v2(points: list[ScalePointV2], sla: float = 0.8) -> int | None:
    """定位拐点 M₀：grep Recall@K **均值**首次跌破 SLA 的规模 N。无则 None。"""
    for p in points:
        if p.grep.mean < sla:
            return p.n
    return None


def format_curve_v2(points: list[ScalePointV2], k: int = 5, sla: float = 0.8) -> str:
    """把 v2 曲线渲染成带 CI 的可读文本表 + 诚实拐点结论（面试/答辩用）。"""
    lines = [
        f"规模曲线 v2（合成集，Zipf 自然增长 + 多种子 95% CI），SLA=Recall@{k}≥{sla}",
        f"{'N':>6} | {'grep R@k (mean±CI)':>22} | {'bm25 R@k (mean±CI)':>22}",
        "-" * 58,
    ]
    for p in points:
        lines.append(
            f"{p.n:>6} | "
            f"{p.grep.mean:>10.3f} ± {p.grep.ci95:<8.3f} | "
            f"{p.bm25.mean:>10.3f} ± {p.bm25.ci95:<8.3f}"
        )
    m0 = find_crossover_v2(points, sla)
    if m0 is not None:
        lines.append(
            f"\n拐点 M₀ 落在 N≈{m0} 附近（grep 均值在此跌破 SLA）；"
            f"注意这是合成集在给定 Zipf 密度下的区间结论，非单点绝对值，需连同各档 CI 一起看。"
        )
    else:
        lines.append(f"\n未观察到 grep 均值跌破 SLA(≥{sla})；当前规模范围内近邻窗口仍够用。")
    lines.append(
        "诚实标注：本表为合成集（隔离'相关性排序 vs 近邻窗口'单一变量）；"
        "真实/脱敏语料作定性佐证，脱敏数据不入库。"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    pts = run_curve_v2([50, 200, 1000, 5000], seeds=[1, 2, 3, 4, 5])
    print(format_curve_v2(pts))
