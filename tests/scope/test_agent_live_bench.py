from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanobot.providers.base import GenerationSettings, LLMResponse, ToolCallRequest
from nanoscope.eval.agent_bench import AgentCaseResult, aggregate_agent_board


class RecoveryProvider:
    generation = GenerationSettings(temperature=0.0, max_tokens=128)

    def __init__(self):
        self.calls = 0

    async def chat_with_retry(self, **_kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.calls <= 2:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id=f"recovery-{self.calls}",
                        name="unstable_lookup",
                        arguments={"key": "quota"},
                    )
                ],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 20, "completion_tokens": 5},
            )
        return LLMResponse(
            content="恢复成功，配额是 128。",
            usage={"prompt_tokens": 15, "completion_tokens": 8},
        )


def test_agent_live_v1_has_24_frozen_tasks_and_real_recovery_cases():
    from nanoscope.eval.agent_live_dataset import AGENT_LIVE_DATASET_VERSION, load_agent_live_v1

    tasks = load_agent_live_v1()
    groups: dict[str, int] = {}
    for task in tasks:
        groups[task.suite] = groups.get(task.suite, 0) + 1

    assert AGENT_LIVE_DATASET_VERSION == "agent-live24.v2"
    assert len(tasks) == 24
    assert len({task.id for task in tasks}) == 24
    assert groups == {
        "no_tool": 4,
        "single_tool": 6,
        "multi_tool": 4,
        "failure_recovery": 4,
        "context": 3,
        "clarify_refuse": 3,
    }
    recovery = [task for task in tasks if task.suite == "failure_recovery"]
    assert all(task.recovery_expected for task in recovery)
    assert all(task.expected_tools == ("unstable_lookup", "unstable_lookup") for task in recovery)
    prompts = [task.prompt for task in tasks]
    assert all("请用计算工具" not in prompt for prompt in prompts)
    assert all("不要调用工具" not in prompt for prompt in prompts)
    assert all("重试一次" not in prompt for prompt in prompts)


def test_agent_board_reports_partial_sequence_recovery_and_iteration_rates():
    records = [
        AgentCaseResult(
            case_id="A1",
            suite="failure_recovery",
            eligible=True,
            answer_passed=True,
            expected_tools=("unstable_lookup", "unstable_lookup"),
            actual_tools=("unstable_lookup", "unstable_lookup"),
            argument_checks=(True, True),
            partial_score=1.0,
            tool_sequence_passed=True,
            recovery_expected=True,
            recovered=True,
        ),
        AgentCaseResult(
            case_id="A2",
            suite="multi_tool",
            eligible=True,
            answer_passed=False,
            expected_tools=("lookup", "calculate"),
            actual_tools=("lookup",),
            argument_checks=(True, False),
            partial_score=0.5,
            tool_sequence_passed=False,
            max_iteration_terminated=True,
            stop_reason="max_iterations",
        ),
    ]

    board = aggregate_agent_board(records)

    assert board.partial_tsr == pytest.approx(0.75)
    assert board.tool_sequence_success_rate == 0.5
    assert board.error_recovery_rate == 1.0
    assert board.max_iteration_termination_rate == 0.5


@pytest.mark.asyncio
async def test_live_runner_exercises_real_tool_failure_and_recovery():
    from nanoscope.eval.agent_live_dataset import LiveAgentTask
    from nanoscope.eval.agent_live_runner import run_live_agent_task

    task = LiveAgentTask(
        id="FRX",
        suite="failure_recovery",
        prompt="查询 quota；若工具报告瞬态错误，必须重试一次，然后回答配额。",
        expected_facts=("128",),
        expected_tools=("unstable_lookup", "unstable_lookup"),
        expected_arguments=({"key": "quota"}, {"key": "quota"}),
        recovery_expected=True,
    )

    record = await run_live_agent_task(
        task,
        provider=RecoveryProvider(),
        model="fake-live",
    )

    assert record.answer_passed is True
    assert record.actual_tools == ("unstable_lookup", "unstable_lookup")
    assert record.argument_checks == (True, True)
    assert record.recovery_expected is True
    assert record.recovered is True
    assert record.unhandled_tool_errors == 0
    assert record.strict_success is True


def test_live_artifact_records_model_but_never_credential_values(tmp_path: Path):
    from nanoscope.eval.agent_live_runner import write_agent_live_artifact

    output = tmp_path / "AGENT_LIVE_V1.json"
    write_agent_live_artifact(
        output,
        board=aggregate_agent_board(
            [AgentCaseResult("A1", "no_tool", True, True)]
        ),
        model="deepseek-v4-flash",
        provider="deepseek",
        dataset_version="agent-live24.v2",
        credential_env_names=("DEEPSEEK_API_KEY",),
        secret_values=("must-not-appear",),
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    serialized = json.dumps(data)
    assert data["run"]["model"] == "deepseek-v4-flash"
    assert data["run"]["credential_env_names"] == ["DEEPSEEK_API_KEY"]
    assert "must-not-appear" not in serialized
    assert "records" not in data["board"]


def test_agent_checkpoint_loader_uses_case_id(tmp_path: Path):
    from nanoscope.eval.agent_live_runner import _load_agent_checkpoint

    checkpoint = tmp_path / "agent.jsonl"
    checkpoint.write_text(
        '{"case_id":"A1","answer_passed":true}\n'
        '{"case_id":"A2","answer_passed":false}\n',
        encoding="utf-8",
    )

    records = _load_agent_checkpoint(checkpoint)

    assert tuple(records) == ("A1", "A2")
    assert records["A1"]["answer_passed"] is True
