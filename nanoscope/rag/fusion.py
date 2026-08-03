"""Small, explainable linear fusion model for retrieval candidates."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

FEATURE_NAMES = ("bm25_rr", "dense_rr", "reranker_rr", "agreement")


def _sigmoid(value: float) -> float:
    clipped = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-clipped))


@dataclass(frozen=True)
class FusionExample:
    features: Mapping[str, float]
    relevant: bool


@dataclass(frozen=True)
class LinearFusionModel:
    """Pointwise logistic ranker; its logit is used as the ranking score."""

    weights: tuple[float, ...]
    bias: float = 0.0
    feature_names: tuple[str, ...] = FEATURE_NAMES

    def __post_init__(self) -> None:
        if len(self.weights) != len(self.feature_names):
            raise ValueError("weights and feature_names must have equal length")

    def score(self, features: Mapping[str, float]) -> float:
        return self.bias + sum(
            weight * float(features.get(name, 0.0))
            for name, weight in zip(self.feature_names, self.weights)
        )

    def probability(self, features: Mapping[str, float]) -> float:
        return _sigmoid(self.score(features))

    def rank(
        self,
        candidates: Sequence[tuple[str, Mapping[str, float]]],
    ) -> list[str]:
        scored = [
            (candidate_id, self.score(features))
            for candidate_id, features in candidates
        ]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return [candidate_id for candidate_id, _ in scored]

    @classmethod
    def fit(
        cls,
        examples: Sequence[FusionExample],
        *,
        feature_names: tuple[str, ...] = FEATURE_NAMES,
        learning_rate: float = 0.2,
        epochs: int = 400,
        l2: float = 0.01,
    ) -> LinearFusionModel:
        if not examples:
            raise ValueError("at least one fusion example is required")
        if not any(example.relevant for example in examples):
            raise ValueError("fusion examples must contain a positive label")
        if all(example.relevant for example in examples):
            raise ValueError("fusion examples must contain a negative label")
        weights = [0.0] * len(feature_names)
        bias = 0.0
        scale = 1.0 / len(examples)
        for _ in range(epochs):
            weight_gradient = [0.0] * len(weights)
            bias_gradient = 0.0
            for example in examples:
                values = [float(example.features.get(name, 0.0)) for name in feature_names]
                prediction = _sigmoid(bias + sum(w * x for w, x in zip(weights, values)))
                error = prediction - float(example.relevant)
                for index, value in enumerate(values):
                    weight_gradient[index] += error * value
                bias_gradient += error
            for index in range(len(weights)):
                gradient = weight_gradient[index] * scale + l2 * weights[index]
                weights[index] -= learning_rate * gradient
            bias -= learning_rate * bias_gradient * scale
        return cls(tuple(weights), bias, feature_names)


def reciprocal_rank_features(
    candidate_ids: Sequence[str],
    *,
    bm25_ranking: Sequence[str],
    dense_ranking: Sequence[str],
    reranker_ranking: Sequence[str],
) -> list[tuple[str, dict[str, float]]]:
    """Build bounded rank features without mixing incomparable raw scores."""

    def positions(ranking: Sequence[str]) -> dict[str, int]:
        return {candidate_id: rank for rank, candidate_id in enumerate(ranking, start=1)}

    bm25_positions = positions(bm25_ranking)
    dense_positions = positions(dense_ranking)
    reranker_positions = positions(reranker_ranking)
    output: list[tuple[str, dict[str, float]]] = []
    for candidate_id in candidate_ids:
        bm25_rank = bm25_positions.get(candidate_id)
        dense_rank = dense_positions.get(candidate_id)
        reranker_rank = reranker_positions.get(candidate_id)
        output.append(
            (
                candidate_id,
                {
                    "bm25_rr": 1.0 / bm25_rank if bm25_rank else 0.0,
                    "dense_rr": 1.0 / dense_rank if dense_rank else 0.0,
                    "reranker_rr": 1.0 / reranker_rank if reranker_rank else 0.0,
                    "agreement": float(bm25_rank is not None and dense_rank is not None),
                },
            )
        )
    return output
