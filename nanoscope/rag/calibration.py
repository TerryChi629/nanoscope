"""Confidence calibration and deterministic risk-coverage metrics."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


def _sigmoid(value: float) -> float:
    clipped = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-clipped))


@dataclass(frozen=True)
class PlattCalibrator:
    """One-dimensional logistic calibration fitted on a development split."""

    slope: float
    intercept: float

    def predict(self, score: float) -> float:
        return _sigmoid(self.slope * score + self.intercept)

    @classmethod
    def fit(
        cls,
        scores: Sequence[float],
        labels: Sequence[bool],
        *,
        learning_rate: float = 0.05,
        epochs: int = 800,
        l2: float = 0.001,
    ) -> PlattCalibrator:
        if len(scores) != len(labels) or not scores:
            raise ValueError("scores and labels must be non-empty and aligned")
        if not any(labels) or all(labels):
            raise ValueError("calibration labels must contain both classes")
        slope = 0.0
        positive_rate = sum(labels) / len(labels)
        intercept = math.log(positive_rate / (1.0 - positive_rate))
        scale = 1.0 / len(scores)
        for _ in range(epochs):
            slope_gradient = 0.0
            intercept_gradient = 0.0
            for score, label in zip(scores, labels):
                error = _sigmoid(slope * score + intercept) - float(label)
                slope_gradient += error * score
                intercept_gradient += error
            slope -= learning_rate * (slope_gradient * scale + l2 * slope)
            intercept -= learning_rate * intercept_gradient * scale
        return cls(slope=slope, intercept=intercept)


def brier_score(probabilities: Sequence[float], labels: Sequence[bool]) -> float:
    if len(probabilities) != len(labels) or not probabilities:
        raise ValueError("probabilities and labels must be non-empty and aligned")
    return sum(
        (probability - float(label)) ** 2
        for probability, label in zip(probabilities, labels)
    ) / len(labels)


def expected_calibration_error(
    probabilities: Sequence[float],
    labels: Sequence[bool],
    *,
    n_bins: int = 10,
) -> float:
    if len(probabilities) != len(labels) or not probabilities:
        raise ValueError("probabilities and labels must be non-empty and aligned")
    if n_bins <= 0:
        raise ValueError("n_bins must be positive")
    total = len(labels)
    error = 0.0
    for bin_index in range(n_bins):
        lower = bin_index / n_bins
        upper = (bin_index + 1) / n_bins
        indexes = [
            index
            for index, probability in enumerate(probabilities)
            if lower <= probability < upper
            or (bin_index == n_bins - 1 and probability == 1.0)
        ]
        if not indexes:
            continue
        confidence = sum(probabilities[index] for index in indexes) / len(indexes)
        accuracy = sum(float(labels[index]) for index in indexes) / len(indexes)
        error += len(indexes) / total * abs(confidence - accuracy)
    return error


def risk_coverage_curve(
    probabilities: Sequence[float],
    labels: Sequence[bool],
) -> list[tuple[float, float]]:
    """Return (coverage, risk) while accepting examples by confidence."""

    if len(probabilities) != len(labels) or not probabilities:
        raise ValueError("probabilities and labels must be non-empty and aligned")
    ordered = sorted(
        zip(probabilities, labels),
        key=lambda item: -item[0],
    )
    errors = 0
    curve: list[tuple[float, float]] = []
    for index, (_, label) in enumerate(ordered, start=1):
        errors += int(not label)
        curve.append((index / len(ordered), errors / index))
    return curve
