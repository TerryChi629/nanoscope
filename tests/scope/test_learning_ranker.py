from __future__ import annotations

import math

import pytest

from nanoscope.eval.rag_bench import build_benchmark
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
from nanoscope.rag.search import doc_search_visible
from nanoscope.rag.store import ChunkStore
from nanoscope.rag.sweep import HashingEmbedder


def test_linear_fusion_learns_to_promote_agreed_relevant_candidates():
    examples = [
        FusionExample(
            {"bm25_rr": 1.0, "dense_rr": 1.0, "reranker_rr": 1.0, "agreement": 1.0},
            True,
        ),
        FusionExample(
            {"bm25_rr": 0.5, "dense_rr": 0.5, "reranker_rr": 0.5, "agreement": 1.0},
            True,
        ),
        FusionExample(
            {"bm25_rr": 1.0, "dense_rr": 0.0, "reranker_rr": 0.2, "agreement": 0.0},
            False,
        ),
        FusionExample(
            {"bm25_rr": 0.0, "dense_rr": 1.0, "reranker_rr": 0.1, "agreement": 0.0},
            False,
        ),
    ]
    model = LinearFusionModel.fit(examples)
    candidates = [
        ("hard_negative", examples[2].features),
        ("gold", examples[0].features),
    ]

    assert model.rank(candidates) == ["gold", "hard_negative"]
    assert model.probability(examples[0].features) > model.probability(examples[2].features)


def test_reciprocal_features_keep_each_retrieval_signal_auditable():
    rows = dict(
        reciprocal_rank_features(
            ["a", "b"],
            bm25_ranking=["a", "b"],
            dense_ranking=["b"],
            reranker_ranking=["b", "a"],
        )
    )

    assert rows["a"] == {
        "bm25_rr": 1.0,
        "dense_rr": 0.0,
        "reranker_rr": 0.5,
        "agreement": 0.0,
    }
    assert rows["b"]["agreement"] == 1.0
    assert rows["b"]["dense_rr"] == 1.0


def test_platt_calibration_improves_shifted_scores():
    scores = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
    labels = [False, False, False, True, True, True]
    raw = [1.0 / (1.0 + math.exp(-score)) for score in scores]
    calibrator = PlattCalibrator.fit(scores, labels)
    calibrated = [calibrator.predict(score) for score in scores]

    assert brier_score(calibrated, labels) < brier_score(raw, labels)
    assert expected_calibration_error(calibrated, labels, n_bins=3) < 0.35
    assert calibrator.predict(15.0) > calibrator.predict(10.0)


def test_risk_coverage_accepts_high_confidence_correct_cases_first():
    curve = risk_coverage_curve(
        [0.95, 0.8, 0.4, 0.1],
        [True, True, False, False],
    )

    assert curve[0] == pytest.approx((0.25, 0.0))
    assert curve[1] == pytest.approx((0.5, 0.0))
    assert curve[-1] == pytest.approx((1.0, 0.5))


def test_calibrated_search_abstains_below_threshold(tmp_path):
    store = ChunkStore(tmp_path / "rag.db")
    benchmark = build_benchmark(store)
    model = LinearFusionModel((1.0, 1.0, 1.0, 1.0))

    results = doc_search_visible(
        store,
        benchmark.asker,
        "量子加密预算",
        embedder=HashingEmbedder(),
        fusion_model=model,
        calibrator=PlattCalibrator(slope=0.0, intercept=-10.0),
        confidence_threshold=0.9,
    )

    assert results == []
    store.close()
