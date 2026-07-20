"""检索质量指标 (PRD §9/§14.5)。

纯函数，输入 ranked_ids（检索器按相关性降序返回的记忆 id）+ gold_ids
（该 query 应召回的 id 集合），输出 Recall@K / MRR / nDCG@K。

约定：ranked_ids 已是相关性降序；gold_ids 是无序集合（binary relevance）。
指标只在"隔离墙之后的可见集合"上算——可见性由 Repository 保证，与本模块无关。
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def recall_at_k(ranked_ids: Sequence[str], gold_ids: set[str], k: int) -> float:
    """Recall@K：前 K 个结果里命中的 gold 占全部 gold 的比例。

    gold 为空时返回 1.0（无可召回项即视为满足，避免除零污染均值）。
    """
    if not gold_ids:
        return 1.0
    hits = sum(1 for rid in ranked_ids[:k] if rid in gold_ids)
    return hits / len(gold_ids)


def mrr(ranked_ids: Sequence[str], gold_ids: set[str]) -> float:
    """Mean Reciprocal Rank（单 query 版）：第一个命中的名次倒数。

    没有任何 gold 命中返回 0.0。
    """
    for idx, rid in enumerate(ranked_ids, start=1):
        if rid in gold_ids:
            return 1.0 / idx
    return 0.0


def ndcg_at_k(ranked_ids: Sequence[str], gold_ids: set[str], k: int) -> float:
    """nDCG@K（binary relevance）：DCG 除以理想排序 IDCG。

    gold 为空返回 1.0。IDCG 用 min(|gold|, k) 个理想命中计算。
    """
    if not gold_ids:
        return 1.0
    dcg = 0.0
    for idx, rid in enumerate(ranked_ids[:k], start=1):
        if rid in gold_ids:
            dcg += 1.0 / math.log2(idx + 1)
    ideal_hits = min(len(gold_ids), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0
