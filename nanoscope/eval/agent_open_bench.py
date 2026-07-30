"""Repeated-run and human-review metrics for the open Agent benchmark."""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from nanoscope.eval.agent_bench import AgentBoard, AgentCaseResult, aggregate_agent_board

HUMAN_DIMENSIONS = ("correctness", "completeness", "instruction_following", "tool_rationality")


@dataclass(frozen=True)
class HumanReview:
    case_id: str
    run_index: int
    correctness: int
    completeness: int
    instruction_following: int
    tool_rationality: int
    reviewer: str
    notes: str = ""

    @property
    def score(self) -> float:
        return sum(getattr(self, name) for name in HUMAN_DIMENSIONS) / (
            4 * len(HUMAN_DIMENSIONS)
        )


@dataclass(frozen=True)
class OpenRunRecord:
    dataset_version: str
    task_sha256: str
    model: str
    provider: str
    run_index: int
    result: AgentCaseResult

    @property
    def record_id(self) -> str:
        return f"{self.result.case_id}#{self.run_index}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "_type": "agent_open_run",
            "dataset_version": self.dataset_version,
            "task_sha256": self.task_sha256,
            "model": self.model,
            "provider": self.provider,
            "run_index": self.run_index,
            "record_id": self.record_id,
            "result": asdict(self.result),
        }


@dataclass(frozen=True)
class MeanCI:
    mean: float
    ci95_low: float
    ci95_high: float
    n: int


@dataclass(frozen=True)
class StageLatency:
    p50_ms: float
    p95_ms: float


@dataclass(frozen=True)
class OpenAgentBoard:
    dataset_version: str
    n_tasks: int
    repeats: int
    n_runs: int
    automatic: AgentBoard
    strict_tsr: MeanCI
    partial_tsr: MeanCI
    stage_latency: dict[str, StageLatency]
    human_review_status: str
    n_human_reviewed: int
    n_human_required: int
    human_score: MeanCI | None
    automatic_gate_pass: bool
    human_gate_pass: bool
    overall_gate_pass: bool

    def to_summary_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["automatic"].pop("records", None)
        return data


def aggregate_open_agent_board(
    records: Sequence[OpenRunRecord],
    *,
    dataset_version: str,
    n_tasks: int,
    repeats: int,
    reviews: Sequence[HumanReview] = (),
    minimum_strict_tsr: float = 0.8,
    minimum_human_score: float = 0.75,
) -> OpenAgentBoard:
    results = [record.result for record in records]
    automatic = aggregate_agent_board(results)
    per_run_strict = [
        sum(record.result.strict_success for record in records if record.run_index == index)
        / n_tasks
        for index in range(1, repeats + 1)
    ]
    per_run_partial = [
        sum(
            (
                record.result.partial_score
                if record.result.partial_score is not None
                else float(record.result.strict_success)
            )
            for record in records
            if record.run_index == index
        )
        / n_tasks
        for index in range(1, repeats + 1)
    ]
    expected_review_keys = {
        (record.result.case_id, record.run_index) for record in records
    }
    review_keys = {(review.case_id, review.run_index) for review in reviews}
    reviews_complete = bool(expected_review_keys) and (
        review_keys == expected_review_keys and len(reviews) == len(expected_review_keys)
    )
    human_score = _mean_ci([review.score for review in reviews]) if reviews else None
    automatic_gate = (
        len(records) == n_tasks * repeats
        and automatic.n_errored == 0
        and automatic.n_skipped == 0
        and automatic.strict_tsr >= minimum_strict_tsr
    )
    human_gate = bool(
        reviews_complete
        and human_score is not None
        and human_score.mean >= minimum_human_score
    )
    return OpenAgentBoard(
        dataset_version=dataset_version,
        n_tasks=n_tasks,
        repeats=repeats,
        n_runs=len(records),
        automatic=automatic,
        strict_tsr=_mean_ci(per_run_strict),
        partial_tsr=_mean_ci(per_run_partial),
        stage_latency={
            "total": _stage_latency([result.latency_ms for result in results]),
            "model": _stage_latency([result.model_latency_ms for result in results]),
            "tool": _stage_latency([result.tool_latency_ms for result in results]),
            "orchestration": _stage_latency(
                [
                    max(
                        0.0,
                        result.latency_ms
                        - result.model_latency_ms
                        - result.tool_latency_ms,
                    )
                    for result in results
                ]
            ),
        },
        human_review_status="completed" if reviews_complete else "pending",
        n_human_reviewed=len(review_keys & expected_review_keys),
        n_human_required=len(expected_review_keys),
        human_score=human_score,
        automatic_gate_pass=automatic_gate,
        human_gate_pass=human_gate,
        overall_gate_pass=automatic_gate and human_gate,
    )


def _mean_ci(values: Sequence[float]) -> MeanCI:
    if not values:
        return MeanCI(0.0, 0.0, 0.0, 0)
    mean = statistics.fmean(values)
    if len(values) == 1:
        return MeanCI(mean, mean, mean, 1)
    half_width = 1.96 * statistics.stdev(values) / math.sqrt(len(values))
    return MeanCI(
        mean=mean,
        ci95_low=max(0.0, mean - half_width),
        ci95_high=min(1.0, mean + half_width),
        n=len(values),
    )


def _stage_latency(values: Sequence[float]) -> StageLatency:
    ordered = sorted(values)
    if not ordered:
        return StageLatency(0.0, 0.0)

    def quantile(q: float) -> float:
        return ordered[max(0, math.ceil(q * len(ordered)) - 1)]

    return StageLatency(p50_ms=quantile(0.5), p95_ms=quantile(0.95))
