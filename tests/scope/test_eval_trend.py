from __future__ import annotations

import pytest

from nanoscope.eval.nightly import run_offline_nightly
from nanoscope.eval.trend import RegressionPolicy, compare_metric, metric_stat


def test_regression_requires_practical_drop_and_separated_intervals():
    baseline = metric_stat([0.95, 0.96, 0.94, 0.95])
    noisy_current = metric_stat([0.91, 0.98, 0.90, 0.97])
    bad_current = metric_stat([0.80, 0.81, 0.79, 0.80])
    policy = RegressionPolicy(direction="higher", relative_tolerance=0.02)

    assert compare_metric("tsr", baseline, noisy_current, policy).regression is False
    decision = compare_metric("tsr", baseline, bad_current, policy)
    assert decision.intervals_separated is True
    assert decision.regression is True


def test_lower_is_better_metric_detects_latency_regression():
    baseline = metric_stat([100, 102, 98])
    current = metric_stat([140, 142, 138])

    decision = compare_metric(
        "p95",
        baseline,
        current,
        RegressionPolicy(direction="lower", relative_tolerance=0.20),
    )

    assert decision.regression is True
    assert decision.relative_degradation == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_offline_nightly_collects_three_systems():
    stats = await run_offline_nightly(".", rounds=2)

    assert stats["agent.strict_tsr"].mean == 1.0
    assert stats["security.violations"].mean == 0.0
    assert stats["rag.recall_at_k"].mean > 0.0
