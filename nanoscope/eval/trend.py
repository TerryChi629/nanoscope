"""Multi-round confidence intervals and regression decisions for nightly evaluation."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class MetricStat:
    values: tuple[float, ...]

    @property
    def mean(self) -> float:
        return statistics.fmean(self.values) if self.values else 0.0

    @property
    def std(self) -> float:
        return statistics.stdev(self.values) if len(self.values) > 1 else 0.0

    @property
    def ci95(self) -> float:
        return 1.96 * self.std / math.sqrt(len(self.values)) if len(self.values) > 1 else 0.0

    @property
    def lower(self) -> float:
        return self.mean - self.ci95

    @property
    def upper(self) -> float:
        return self.mean + self.ci95


@dataclass(frozen=True)
class RegressionPolicy:
    direction: str
    relative_tolerance: float
    absolute_tolerance: float = 0.0

    def __post_init__(self) -> None:
        if self.direction not in {"higher", "lower"}:
            raise ValueError("direction must be 'higher' or 'lower'")


@dataclass(frozen=True)
class RegressionDecision:
    metric: str
    baseline: MetricStat
    current: MetricStat
    absolute_change: float
    relative_degradation: float
    intervals_separated: bool
    regression: bool

    def to_dict(self) -> dict:
        return asdict(self)


def metric_stat(values: Sequence[float]) -> MetricStat:
    return MetricStat(tuple(float(value) for value in values))


def compare_metric(
    metric: str,
    baseline: MetricStat,
    current: MetricStat,
    policy: RegressionPolicy,
) -> RegressionDecision:
    """Require both practical degradation and separated 95% CIs."""
    absolute_change = current.mean - baseline.mean
    scale = abs(baseline.mean) or 1.0
    if policy.direction == "higher":
        degradation = max(0.0, baseline.mean - current.mean)
        intervals_separated = current.upper < baseline.lower
    else:
        degradation = max(0.0, current.mean - baseline.mean)
        intervals_separated = current.lower > baseline.upper
    relative_degradation = degradation / scale
    practical = (
        degradation > policy.absolute_tolerance
        and relative_degradation > policy.relative_tolerance
    )
    return RegressionDecision(
        metric=metric,
        baseline=baseline,
        current=current,
        absolute_change=absolute_change,
        relative_degradation=relative_degradation,
        intervals_separated=intervals_separated,
        regression=practical and intervals_separated,
    )
