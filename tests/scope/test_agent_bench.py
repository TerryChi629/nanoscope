from __future__ import annotations

import pytest

from nanoscope.eval.agent_bench import (
    AgentCaseResult,
    ModelPrice,
    aggregate_agent_board,
    build_case_result,
)


def test_strict_success_requires_answer_tools_arguments_and_safe_completion():
    passed = AgentCaseResult(
        case_id="A1",
        suite="single_tool",
        eligible=True,
        answer_passed=True,
        expected_tools=("read_file",),
        actual_tools=("read_file",),
        argument_checks=(True,),
    )
    wrong_argument = AgentCaseResult(
        case_id="A2",
        suite="single_tool",
        eligible=True,
        answer_passed=True,
        expected_tools=("read_file",),
        actual_tools=("read_file",),
        argument_checks=(False,),
    )

    assert passed.strict_success is True
    assert wrong_argument.strict_success is False


def test_agent_board_uses_explicit_scored_denominators_and_tool_multisets():
    records = [
        AgentCaseResult(
            case_id="A1",
            suite="multi_tool",
            eligible=True,
            answer_passed=True,
            expected_tools=("search", "read"),
            actual_tools=("search", "read", "read"),
            argument_checks=(True, True, True),
            prompt_tokens=80,
            completion_tokens=20,
            latency_ms=100,
        ),
        AgentCaseResult(
            case_id="A2",
            suite="no_tool",
            eligible=True,
            answer_passed=True,
            prompt_tokens=40,
            completion_tokens=10,
            latency_ms=200,
        ),
        AgentCaseResult(
            case_id="A3",
            suite="live_only",
            eligible=True,
            answer_passed=False,
            skipped=True,
        ),
    ]

    board = aggregate_agent_board(records)

    assert board.n_total == 3
    assert board.n_eligible == 2
    assert board.n_success == 2
    assert board.strict_tsr == 1.0
    assert board.tool_selection_precision == pytest.approx(2 / 3)
    assert board.tool_selection_recall == 1.0
    assert board.redundant_tool_rate == pytest.approx(1 / 3)
    assert board.tokens_per_success == 75.0


def test_agent_board_prices_cached_and_uncached_tokens_separately():
    record = AgentCaseResult(
        case_id="A1",
        suite="cost",
        eligible=True,
        answer_passed=True,
        prompt_tokens=1_000_000,
        cached_tokens=250_000,
        completion_tokens=100_000,
    )
    price = ModelPrice(
        input_per_million=2.0,
        cached_input_per_million=0.5,
        output_per_million=10.0,
    )

    board = aggregate_agent_board([record], price=price)

    assert board.cost_total == pytest.approx(2.625)
    assert board.cost_per_success == pytest.approx(2.625)


def test_build_case_result_adapts_runner_usage():
    result = build_case_result(
        "A1",
        "adapter",
        eligible=True,
        answer_passed=True,
        expected_tools=["read"],
        actual_tools=["read"],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 2},
    )

    assert result.expected_tools == ("read",)
    assert result.prompt_tokens == 10
    assert result.cached_tokens == 2
