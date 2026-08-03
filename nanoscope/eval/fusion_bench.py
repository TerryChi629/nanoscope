"""Train/test benchmark for linear retrieval fusion and confidence calibration."""

from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from nanoscope.eval.artifacts import atomic_write_json
from nanoscope.eval.embedding import CachingEmbedder, GlmEmbedder
from nanoscope.eval.metrics import mrr, ndcg_at_k, recall_at_k
from nanoscope.eval.rag_bench import RagScenario, build_benchmark
from nanoscope.eval.retrieval import Bm25Retriever, Doc, rrf_fuse
from nanoscope.rag.calibration import (
    PlattCalibrator,
    brier_score,
    expected_calibration_error,
    risk_coverage_curve,
)
from nanoscope.rag.fusion import (
    FusionExample,
    LinearFusionModel,
    reciprocal_rank_features,
)
from nanoscope.rag.index import PartitionedSearcher
from nanoscope.rag.rerank import SiliconFlowReranker
from nanoscope.rag.store import ChunkStore


@dataclass(frozen=True)
class QueryRanking:
    scenario_id: str
    split: str
    gold_ids: frozenset[str]
    baseline_ids: tuple[str, ...]
    feature_rows: tuple[tuple[str, dict[str, float]], ...]


def _cluster_id(scenario_id: str) -> str:
    if "V" in scenario_id:
        prefix, suffix = scenario_id.rsplit("V", 1)
        if suffix.isdigit():
            return prefix
    return scenario_id


def build_cluster_splits(scenario_ids: list[str]) -> dict[str, str]:
    """Assign whole semantic clusters to an exact deterministic 60/20/20 split."""

    clusters = sorted(
        {_cluster_id(scenario_id) for scenario_id in scenario_ids},
        key=lambda cluster: (hashlib.sha256(cluster.encode()).hexdigest(), cluster),
    )
    if len(clusters) < 3:
        raise ValueError("at least three semantic clusters are required")
    n_train = max(1, round(len(clusters) * 0.6))
    n_dev = max(1, round(len(clusters) * 0.2))
    if n_train + n_dev >= len(clusters):
        n_train = len(clusters) - n_dev - 1
    split_by_cluster = {
        cluster: (
            "train"
            if index < n_train
            else "dev"
            if index < n_train + n_dev
            else "test"
        )
        for index, cluster in enumerate(clusters)
    }
    return {
        scenario_id: split_by_cluster[_cluster_id(scenario_id)]
        for scenario_id in scenario_ids
    }


def _query_ranking(
    store: ChunkStore,
    scenario: RagScenario,
    *,
    context,
    vector_searcher: PartitionedSearcher,
    reranker: SiliconFlowReranker,
    top_k: int,
    fanout: int,
    split: str,
) -> QueryRanking:
    visible = store.visible_chunks(context)
    by_id = {chunk.id: chunk for chunk in visible}
    docs = [
        Doc(id=chunk.id, content=chunk.content, created_at=chunk.created_at)
        for chunk in visible
    ]
    bm25 = Bm25Retriever(docs)
    try:
        bm25_ranking = bm25.search(scenario.query, fanout)
    finally:
        bm25.close()
    vector_ranking = [
        chunk.id
        for chunk in vector_searcher.search(context, scenario.query, fanout).results
        if chunk.id in by_id
    ]
    candidates = rrf_fuse(
        [bm25_ranking, vector_ranking],
        top_k=max(top_k, fanout),
    )
    candidate_texts = [by_id[candidate_id].content for candidate_id in candidates]
    order = reranker.rerank(scenario.query, candidate_texts)
    reranker_ranking = [candidates[index] for index in order]
    features = reciprocal_rank_features(
        candidates,
        bm25_ranking=bm25_ranking,
        dense_ranking=vector_ranking,
        reranker_ranking=reranker_ranking,
    )
    return QueryRanking(
        scenario_id=scenario.id,
        split=split,
        gold_ids=scenario.gold_ids,
        baseline_ids=tuple(reranker_ranking),
        feature_rows=tuple(features),
    )


def _average_metrics(
    rankings: list[tuple[list[str], frozenset[str]]],
    *,
    top_k: int,
) -> dict[str, float]:
    if not rankings:
        raise ValueError("evaluation split is empty")
    return {
        "recall_at_k": sum(
            recall_at_k(ids, set(gold), top_k) for ids, gold in rankings
        )
        / len(rankings),
        "mrr": sum(mrr(ids, set(gold)) for ids, gold in rankings) / len(rankings),
        "ndcg_at_k": sum(
            ndcg_at_k(ids, set(gold), top_k) for ids, gold in rankings
        )
        / len(rankings),
    }


def run_fusion_benchmark(
    rankings: list[QueryRanking],
    *,
    top_k: int = 5,
) -> dict:
    train = [ranking for ranking in rankings if ranking.split == "train"]
    dev = [ranking for ranking in rankings if ranking.split == "dev"]
    test = [ranking for ranking in rankings if ranking.split == "test"]
    if not train or not dev or not test:
        raise ValueError("train/dev/test must all contain semantic clusters")

    examples = [
        FusionExample(features, candidate_id in ranking.gold_ids)
        for ranking in train
        for candidate_id, features in ranking.feature_rows
    ]
    model = LinearFusionModel.fit(examples)

    dev_scores: list[float] = []
    dev_labels: list[bool] = []
    for ranking in dev:
        for candidate_id, features in ranking.feature_rows:
            dev_scores.append(model.score(features))
            dev_labels.append(candidate_id in ranking.gold_ids)
    calibrator = PlattCalibrator.fit(dev_scores, dev_labels)

    baseline_test: list[tuple[list[str], frozenset[str]]] = []
    learned_test: list[tuple[list[str], frozenset[str]]] = []
    candidate_probabilities: list[float] = []
    candidate_labels: list[bool] = []
    top_probabilities: list[float] = []
    top_labels: list[bool] = []
    for ranking in test:
        learned = model.rank(ranking.feature_rows)
        baseline_test.append((list(ranking.baseline_ids), ranking.gold_ids))
        learned_test.append((learned, ranking.gold_ids))
        features = dict(ranking.feature_rows)
        top_probabilities.append(
            calibrator.predict(model.score(features[learned[0]]))
        )
        top_labels.append(learned[0] in ranking.gold_ids)
        for candidate_id, candidate_features in ranking.feature_rows:
            candidate_probabilities.append(
                calibrator.predict(model.score(candidate_features))
            )
            candidate_labels.append(candidate_id in ranking.gold_ids)

    return {
        "dataset_version": "rag96.v2",
        "split": {
            "method": "sha256 semantic-cluster 60/20/20",
            "train_queries": len(train),
            "dev_queries": len(dev),
            "test_queries": len(test),
        },
        "model": {
            "feature_names": list(model.feature_names),
            "weights": list(model.weights),
            "bias": model.bias,
        },
        "calibrator": asdict(calibrator),
        "test": {
            "baseline": _average_metrics(baseline_test, top_k=top_k),
            "learned": _average_metrics(learned_test, top_k=top_k),
            "candidate_brier": brier_score(
                candidate_probabilities,
                candidate_labels,
            ),
            "candidate_ece": expected_calibration_error(
                candidate_probabilities,
                candidate_labels,
                n_bins=min(5, len(candidate_labels)),
            ),
            "query_risk_coverage": risk_coverage_curve(
                top_probabilities,
                top_labels,
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="reports/runs/RAG_FUSION_CALIBRATION_V1.json",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--fanout", type=int, default=20)
    parser.add_argument("--requests-per-second", type=float, default=2.0)
    args = parser.parse_args(argv)
    missing = [
        name
        for name in ("GLM_API_KEY", "SILICONFLOW_API_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        print(f"缺少 {', '.join(missing)}；融合评测未执行")
        return 2

    with tempfile.TemporaryDirectory(prefix="fusion_bench_") as temporary:
        store = ChunkStore(Path(temporary) / "rag.db")
        try:
            benchmark = build_benchmark(store)
            embedder = CachingEmbedder(GlmEmbedder())
            reranker = SiliconFlowReranker(
                requests_per_second=args.requests_per_second,
            )
            vector_searcher = PartitionedSearcher(store, embedder)
            answerable = [
                scenario
                for scenario in benchmark.scenarios
                if scenario.answerable
            ]
            split_by_id = build_cluster_splits(
                [scenario.id for scenario in answerable]
            )
            rankings = [
                _query_ranking(
                    store,
                    scenario,
                    context=benchmark.asker,
                    vector_searcher=vector_searcher,
                    reranker=reranker,
                    top_k=args.top_k,
                    fanout=args.fanout,
                    split=split_by_id[scenario.id],
                )
                for scenario in answerable
            ]
            artifact = run_fusion_benchmark(rankings, top_k=args.top_k)
            artifact["artifact_type"] = "rag_fusion_calibration"
            artifact["schema_version"] = "1.0"
            artifact["security"] = {
                "authorization": "ChunkStore.visible_chunks before ranking",
                "candidate_touch": 0,
                "exposure": 0,
            }
            atomic_write_json(args.out, artifact)
            print(args.out)
        finally:
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
