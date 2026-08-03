from __future__ import annotations

from nanoscope.eval.fusion_bench import (
    QueryRanking,
    build_cluster_splits,
    run_fusion_benchmark,
)


def _features(good: bool) -> dict[str, float]:
    if good:
        return {
            "bm25_rr": 1.0,
            "dense_rr": 1.0,
            "reranker_rr": 1.0,
            "agreement": 1.0,
        }
    return {
        "bm25_rr": 0.2,
        "dense_rr": 0.0,
        "reranker_rr": 0.1,
        "agreement": 0.0,
    }


def _ranking(cluster: str, variant: int, split: str) -> QueryRanking:
    gold = f"{cluster}-gold"
    negative = f"{cluster}-negative"
    return QueryRanking(
        scenario_id=f"{cluster}V{variant}",
        split=split,
        gold_ids=frozenset({gold}),
        baseline_ids=(negative, gold),
        feature_rows=(
            (negative, _features(False)),
            (gold, _features(True)),
        ),
    )


def test_semantic_variants_never_cross_splits():
    ids = [f"C{cluster}V{variant}" for cluster in range(10) for variant in range(1, 5)]
    splits = build_cluster_splits(ids)

    assert all(
        len({splits[f"C{cluster}V{variant}"] for variant in range(1, 5)}) == 1
        for cluster in range(10)
    )
    assert list(splits.values()).count("train") == 24
    assert list(splits.values()).count("dev") == 8
    assert list(splits.values()).count("test") == 8


def test_fusion_benchmark_trains_on_train_and_evaluates_test_once():
    rankings = [
        *(_ranking("train", variant, "train") for variant in range(1, 5)),
        *(_ranking("dev", variant, "dev") for variant in range(1, 5)),
        *(_ranking("test", variant, "test") for variant in range(1, 5)),
    ]

    artifact = run_fusion_benchmark(rankings)

    assert artifact["split"] == {
        "method": "sha256 semantic-cluster 60/20/20",
        "train_queries": 4,
        "dev_queries": 4,
        "test_queries": 4,
    }
    assert artifact["test"]["baseline"]["mrr"] == 0.5
    assert artifact["test"]["learned"]["mrr"] == 1.0
    assert 0.0 <= artifact["test"]["candidate_brier"] <= 1.0
    assert 0.0 <= artifact["test"]["candidate_ece"] <= 1.0
