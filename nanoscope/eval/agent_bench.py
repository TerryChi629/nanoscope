"""Agent 任务效果、工具行为、成本、性能和上下文质量的确定性聚合。"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class ModelPrice:
    """每百万 token 的价格，价格表版本由统一运行清单记录。"""

    input_per_million: float
    output_per_million: float
    cached_input_per_million: float = 0.0


@dataclass(frozen=True)
class AgentCaseResult:
    """一条冻结 Agent 任务的可评分结果。"""

    case_id: str
    suite: str
    eligible: bool
    answer_passed: bool
    expected_tools: tuple[str, ...] = ()
    actual_tools: tuple[str, ...] = ()
    argument_checks: tuple[bool, ...] = ()
    forbidden_tool_calls: int = 0
    unhandled_tool_errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    usage_estimated: bool = False
    latency_ms: float = 0.0
    model_latency_ms: float = 0.0
    tool_latency_ms: float = 0.0
    retries: int = 0
    stop_reason: str = "completed"
    context_facts_expected: int = 0
    context_facts_retained: int = 0
    security_violation: bool = False
    skipped: bool = False
    error: str | None = None
    partial_score: float | None = None
    tool_sequence_passed: bool | None = None
    recovery_expected: bool = False
    recovered: bool = False
    max_iteration_terminated: bool = False
    answer: str = ""
    actual_arguments: tuple[dict, ...] = ()
    tool_statuses: tuple[str, ...] = ()

    @property
    def strict_success(self) -> bool:
        required = Counter(self.expected_tools)
        actual = Counter(self.actual_tools)
        return (
            self.eligible
            and not self.skipped
            and self.error is None
            and self.answer_passed
            and all(actual[name] >= count for name, count in required.items())
            and all(self.argument_checks)
            and self.forbidden_tool_calls == 0
            and self.unhandled_tool_errors == 0
            and not self.security_violation
            and self.stop_reason == "completed"
        )


@dataclass
class AgentBoard:
    n_total: int
    n_eligible: int
    n_success: int
    n_skipped: int
    n_errored: int
    strict_tsr: float
    tool_selection_precision: float
    tool_selection_recall: float
    argument_accuracy: float | None
    redundant_tool_rate: float
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    tokens_per_success: float | None
    cost_total: float | None
    cost_per_success: float | None
    usage_estimated_rate: float
    latency_p50_ms: float
    latency_p95_ms: float
    retry_rate: float
    context_fidelity: float | None
    partial_tsr: float
    tool_sequence_success_rate: float | None
    error_recovery_rate: float | None
    max_iteration_termination_rate: float
    records: list[dict] = field(default_factory=list)

    def to_summary_dict(self) -> dict:
        data = asdict(self)
        data.pop("records", None)
        data["_type"] = "agent_board"
        return data


def _quantile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _tool_counts(records: Sequence[AgentCaseResult]) -> tuple[int, int, int]:
    true_positive = 0
    expected = 0
    actual = 0
    for record in records:
        expected_counts = Counter(record.expected_tools)
        actual_counts = Counter(record.actual_tools)
        expected += sum(expected_counts.values())
        actual += sum(actual_counts.values())
        true_positive += sum(
            min(count, actual_counts.get(name, 0)) for name, count in expected_counts.items()
        )
    return true_positive, expected, actual


def aggregate_agent_board(
    records: Sequence[AgentCaseResult],
    *,
    price: ModelPrice | None = None,
) -> AgentBoard:
    """按同一可评分集合聚合 Agent 五维看板，所有分母显式可审计。"""
    scored = [r for r in records if r.eligible and not r.skipped]
    successes = [r for r in scored if r.strict_success]
    true_positive, expected_tools, actual_tools = _tool_counts(scored)
    argument_checks = [check for r in scored for check in r.argument_checks]
    prompt_tokens = sum(r.prompt_tokens for r in scored)
    completion_tokens = sum(r.completion_tokens for r in scored)
    cached_tokens = sum(r.cached_tokens for r in scored)
    billable_input = max(0, prompt_tokens - cached_tokens)

    cost_total = None
    if price is not None:
        cost_total = (
            billable_input * price.input_per_million
            + cached_tokens * price.cached_input_per_million
            + completion_tokens * price.output_per_million
        ) / 1_000_000

    context_expected = sum(r.context_facts_expected for r in scored)
    context_retained = sum(r.context_facts_retained for r in scored)
    partial_scores = [
        r.partial_score if r.partial_score is not None else float(r.strict_success)
        for r in scored
    ]
    sequence_records = [
        r for r in scored
        if r.expected_tools or r.actual_tools or r.tool_sequence_passed is not None
    ]
    recovery_records = [r for r in scored if r.recovery_expected]
    return AgentBoard(
        n_total=len(records),
        n_eligible=len(scored),
        n_success=len(successes),
        n_skipped=sum(1 for r in records if r.skipped),
        n_errored=sum(1 for r in scored if r.error is not None),
        strict_tsr=len(successes) / len(scored) if scored else 0.0,
        tool_selection_precision=true_positive / actual_tools if actual_tools else 1.0,
        tool_selection_recall=true_positive / expected_tools if expected_tools else 1.0,
        argument_accuracy=(
            sum(argument_checks) / len(argument_checks) if argument_checks else None
        ),
        redundant_tool_rate=(
            (actual_tools - true_positive) / actual_tools if actual_tools else 0.0
        ),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        tokens_per_success=(
            sum(r.prompt_tokens + r.completion_tokens for r in successes) / len(successes)
            if successes
            else None
        ),
        cost_total=cost_total,
        cost_per_success=cost_total / len(successes) if cost_total is not None and successes else None,
        usage_estimated_rate=(
            sum(1 for r in scored if r.usage_estimated) / len(scored) if scored else 0.0
        ),
        latency_p50_ms=_quantile([r.latency_ms for r in scored], 0.5),
        latency_p95_ms=_quantile([r.latency_ms for r in scored], 0.95),
        retry_rate=sum(1 for r in scored if r.retries > 0) / len(scored) if scored else 0.0,
        context_fidelity=context_retained / context_expected if context_expected else None,
        partial_tsr=sum(partial_scores) / len(partial_scores) if partial_scores else 0.0,
        tool_sequence_success_rate=(
            sum(
                1
                for r in sequence_records
                if (
                    r.tool_sequence_passed
                    if r.tool_sequence_passed is not None
                    else r.actual_tools == r.expected_tools
                )
            )
            / len(sequence_records)
            if sequence_records
            else None
        ),
        error_recovery_rate=(
            sum(1 for r in recovery_records if r.recovered) / len(recovery_records)
            if recovery_records
            else None
        ),
        max_iteration_termination_rate=(
            sum(
                1
                for r in scored
                if r.max_iteration_terminated or r.stop_reason == "max_iterations"
            )
            / len(scored)
            if scored
            else 0.0
        ),
        records=[asdict(r) | {"strict_success": r.strict_success} for r in records],
    )


def build_case_result(
    case_id: str,
    suite: str,
    *,
    expected_tools: Sequence[str] = (),
    actual_tools: Sequence[str] = (),
    usage: Mapping[str, int] | None = None,
    **kwargs,
) -> AgentCaseResult:
    """把 AgentHook/Runner 的标准字段适配成冻结评测记录。"""
    usage = usage or {}
    return AgentCaseResult(
        case_id=case_id,
        suite=suite,
        expected_tools=tuple(expected_tools),
        actual_tools=tuple(actual_tools),
        prompt_tokens=int(usage.get("prompt_tokens", 0)),
        completion_tokens=int(usage.get("completion_tokens", 0)),
        cached_tokens=int(usage.get("cached_tokens", 0)),
        **kwargs,
    )
