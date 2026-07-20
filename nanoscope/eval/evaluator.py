"""检索评测器 (PRD §9/§14.5)。

给定一批 (query, gold_ids) 和一个检索器，算平均 Recall@K / MRR / nDCG@K。
用于规模曲线：对同一语料在多个规模 N 上、多个检索器间做可复现 A/B。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from nanoscope.eval.metrics import mrr, ndcg_at_k, recall_at_k
from nanoscope.eval.retrieval import Retriever


@dataclass(frozen=True)
class EvalQuery:
    """一条评测请求：query 文本 + 应召回的 gold 记忆 id 集合。"""

    query: str
    gold_ids: frozenset[str]


@dataclass(frozen=True)
class RetrievalReport:
    """一个检索器在一批 query 上的聚合指标。"""

    retriever: str
    n_queries: int
    k: int
    recall_at_k: float
    mrr: float
    ndcg_at_k: float


def evaluate(
    retriever: Retriever,
    queries: Sequence[EvalQuery],
    *,
    k: int = 5,
    top_k: int | None = None,
) -> RetrievalReport:
    """在 queries 上评测 retriever，返回聚合指标（各 query 等权平均）。

    top_k 是实际取回条数（默认 = k）；指标在前 k 上算。
    """
    fetch_k = top_k if top_k is not None else k
    if not queries:
        return RetrievalReport(retriever.name, 0, k, 0.0, 0.0, 0.0)
    sum_recall = sum_mrr = sum_ndcg = 0.0
    for q in queries:
        ranked = retriever.search(q.query, fetch_k)
        gold = set(q.gold_ids)
        sum_recall += recall_at_k(ranked, gold, k)
        sum_mrr += mrr(ranked, gold)
        sum_ndcg += ndcg_at_k(ranked, gold, k)
    n = len(queries)
    return RetrievalReport(
        retriever=retriever.name,
        n_queries=n,
        k=k,
        recall_at_k=sum_recall / n,
        mrr=sum_mrr / n,
        ndcg_at_k=sum_ndcg / n,
    )
