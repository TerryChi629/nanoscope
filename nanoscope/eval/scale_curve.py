"""规模曲线运行器 (PRD §14.5 / M7 验收)。

加载冻结数据集，在每个规模 N 上对 grep / bm25(/后段 rrf) 跑 Evaluator，
输出 Recall@K / MRR / nDCG@K 随 N 的曲线，并定位 grep 掉出 SLA 的拐点 M₀。

用法：python -m nanoscope.eval.scale_curve
"""

from __future__ import annotations

from dataclasses import dataclass

from nanoscope.eval.embedding import Embedder
from nanoscope.eval.evaluator import EvalQuery, RetrievalReport, evaluate
from nanoscope.eval.retrieval import (
    Bm25Retriever,
    Doc,
    GrepRetriever,
    RrfRetriever,
    VectorRetriever,
)
from nanoscope.eval.scale_dataset import load_frozen


@dataclass(frozen=True)
class ScalePoint:
    n: int
    grep: RetrievalReport
    bm25: RetrievalReport
    vector: RetrievalReport | None = None
    rrf: RetrievalReport | None = None


def _docs_and_queries(dataset: dict) -> tuple[list[Doc], list[EvalQuery]]:
    docs = [
        Doc(id=d["id"], content=d["content"], created_at=d["created_at"])
        for d in dataset["docs"]
    ]
    queries = [
        EvalQuery(query=q["query"], gold_ids=frozenset(q["gold_ids"]))
        for q in dataset["queries"]
    ]
    return docs, queries


def run_curve(
    k: int = 5,
    frozen: dict | None = None,
    embedder: Embedder | None = None,
) -> list[ScalePoint]:
    """在冻结集各规模上评测 grep vs bm25（可选 vector/rrf），返回曲线点列表。

    传入 embedder（如 GlmEmbedder）时额外评测 vector（GLM 向量近邻）与
    rrf（BM25+向量 倒数秩融合），用于展示向量补 BM25 语义盲区、RRF 取两者之长。
    """
    data = frozen or load_frozen()
    points: list[ScalePoint] = []
    for n in data["scales"]:
        ds = data["datasets"][str(n)]
        docs, queries = _docs_and_queries(ds)
        grep = GrepRetriever(docs)
        bm25 = Bm25Retriever(docs)
        try:
            vector_rep = rrf_rep = None
            if embedder is not None:
                vec = VectorRetriever(docs, embedder)
                rrf = RrfRetriever([bm25, vec])
                vector_rep = evaluate(vec, queries, k=k)
                rrf_rep = evaluate(rrf, queries, k=k)
            points.append(
                ScalePoint(
                    n=n,
                    grep=evaluate(grep, queries, k=k),
                    bm25=evaluate(bm25, queries, k=k),
                    vector=vector_rep,
                    rrf=rrf_rep,
                )
            )
        finally:
            bm25.close()
    return points


def find_crossover(points: list[ScalePoint], sla: float = 0.8) -> int | None:
    """定位拐点 M₀：grep 的 Recall@K 首次跌破 SLA 的规模 N。无则 None。"""
    for p in points:
        if p.grep.recall_at_k < sla:
            return p.n
    return None


def format_curve(points: list[ScalePoint], k: int = 5, sla: float = 0.8) -> str:
    """把曲线渲染成可读文本表 + 拐点结论（面试/答辩用）。

    有 vector/rrf 时追加两列（Recall@K），展示向量补 BM25 盲区、RRF 取两者之长。
    """
    has_rag = any(p.vector is not None for p in points)
    header = f"{'N':>6} | {'grep R@k':>9} {'bm25 R@k':>9}"
    if has_rag:
        header += f" {'vec R@k':>9} {'rrf R@k':>9}"
    lines = [
        f"规模曲线 (Recall@{k})，SLA=Recall@{k}≥{sla}",
        header,
        "-" * len(header),
    ]
    for p in points:
        row = f"{p.n:>6} | {p.grep.recall_at_k:>9.3f} {p.bm25.recall_at_k:>9.3f}"
        if has_rag:
            v = p.vector.recall_at_k if p.vector else float("nan")
            r = p.rrf.recall_at_k if p.rrf else float("nan")
            row += f" {v:>9.3f} {r:>9.3f}"
        lines.append(row)
    m0 = find_crossover(points, sla)
    if m0 is not None:
        lines.append(f"\n拐点 M₀ ≈ N={m0}：grep 的 Recall@{k} 在此跌破 SLA，应上 BM25 检索。")
    else:
        lines.append(f"\n未观察到 grep 跌破 SLA(≥{sla})；当前规模范围内基线全量注入仍够用。")
    if has_rag:
        lines.append("vector 补 BM25 的语义/词序盲区（如「橘猫豆豆」↔「豆豆的橘猫」）；rrf 融合取两者之长。")
    return "\n".join(lines)


if __name__ == "__main__":
    pts = run_curve()
    print(format_curve(pts))
