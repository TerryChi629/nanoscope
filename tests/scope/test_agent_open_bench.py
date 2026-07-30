from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from nanobot.providers.base import GenerationSettings, LLMResponse, ToolCallRequest
from nanoscope.eval.agent_bench import AgentCaseResult
from nanoscope.eval.agent_open_bench import (
    HumanReview,
    OpenRunRecord,
    aggregate_open_agent_board,
)


class TimedLookupProvider:
    generation = GenerationSettings(temperature=0.0, max_tokens=128)

    def __init__(self):
        self.calls = 0

    async def chat_with_retry(self, **_kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="lookup-1",
                        name="lookup",
                        arguments={"key": "region"},
                    )
                ],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 10, "completion_tokens": 3},
            )
        return LLMResponse(
            content="服务位于华东，建议检查跨地域切流。",
            usage={"prompt_tokens": 12, "completion_tokens": 8},
        )


def _record(
    case_id: str,
    run_index: int,
    *,
    success: bool = True,
) -> OpenRunRecord:
    return OpenRunRecord(
        dataset_version="agent-open30.v1",
        task_sha256=f"hash-{case_id}",
        model="fake-model",
        provider="fake-provider",
        run_index=run_index,
        result=AgentCaseResult(
            case_id=case_id,
            suite="synthesis",
            eligible=True,
            answer_passed=success,
            partial_score=float(success),
            latency_ms=10 + run_index,
            model_latency_ms=8 + run_index,
            tool_latency_ms=1,
        ),
    )


def test_agent_open_v1_has_30_deidentified_tasks_in_six_suites():
    from nanoscope.eval.agent_open_dataset import (
        AGENT_OPEN_DATASET_VERSION,
        load_agent_open_v1,
    )

    tasks = load_agent_open_v1()
    groups: dict[str, int] = {}
    for task in tasks:
        groups[task.suite] = groups.get(task.suite, 0) + 1

    assert AGENT_OPEN_DATASET_VERSION == "agent-open30.v1"
    assert len(tasks) == 30
    assert len({task.id for task in tasks}) == 30
    assert groups == {
        "synthesis": 5,
        "single_tool_open": 5,
        "multi_tool_open": 5,
        "failure_recovery_open": 5,
        "multi_turn": 5,
        "clarify_safety": 5,
    }
    assert all(task.rubric for task in tasks)


def test_open_board_reports_three_run_ci_and_fails_closed_without_human_review():
    records = [
        _record(case_id, run_index)
        for run_index in range(1, 4)
        for case_id in ("A1", "A2")
    ]

    board = aggregate_open_agent_board(
        records,
        dataset_version="agent-open30.v1",
        n_tasks=2,
        repeats=3,
    )

    assert board.strict_tsr.mean == 1.0
    assert board.strict_tsr.n == 3
    assert board.human_review_status == "pending"
    assert board.automatic_gate_pass is True
    assert board.human_gate_pass is False
    assert board.overall_gate_pass is False
    assert board.stage_latency["model"].p95_ms > 0
    assert board.stage_latency["orchestration"].p50_ms >= 0


def test_open_board_requires_complete_human_review_matrix():
    records = [
        _record(case_id, run_index)
        for run_index in range(1, 4)
        for case_id in ("A1", "A2")
    ]
    reviews = [
        HumanReview(case_id, run_index, 4, 4, 4, 4, "reviewer")
        for run_index in range(1, 4)
        for case_id in ("A1", "A2")
    ]

    board = aggregate_open_agent_board(
        records,
        dataset_version="agent-open30.v1",
        n_tasks=2,
        repeats=3,
        reviews=reviews,
    )

    assert board.human_review_status == "completed"
    assert board.human_score is not None
    assert board.human_score.mean == 1.0
    assert board.overall_gate_pass is True


def test_allowed_tool_budget_accepts_equivalent_path_but_rejects_repetition():
    no_optional_call = AgentCaseResult(
        case_id="A1",
        suite="multi_tool_open",
        eligible=True,
        answer_passed=True,
        expected_tools=("lookup",),
        allowed_tools=("calculate",),
        actual_tools=("lookup",),
        argument_checks=(True,),
        tool_sequence_passed=True,
    )
    one_optional_call = AgentCaseResult(
        case_id="A2",
        suite="multi_tool_open",
        eligible=True,
        answer_passed=True,
        expected_tools=("lookup",),
        allowed_tools=("calculate",),
        actual_tools=("lookup", "calculate"),
        argument_checks=(True,),
        tool_sequence_passed=True,
    )
    repeated_optional_call = AgentCaseResult(
        case_id="A3",
        suite="multi_tool_open",
        eligible=True,
        answer_passed=True,
        expected_tools=("lookup",),
        allowed_tools=("calculate",),
        actual_tools=("lookup", "calculate", "calculate"),
        argument_checks=(True,),
        forbidden_tool_calls=1,
        tool_sequence_passed=False,
    )

    board = aggregate_open_agent_board(
        [
            OpenRunRecord(
                "agent-open30.v1",
                f"hash-{record.case_id}",
                "model",
                "provider",
                index,
                record,
            )
            for index, record in enumerate(
                (no_optional_call, one_optional_call, repeated_optional_call),
                start=1,
            )
        ],
        dataset_version="agent-open30.v1",
        n_tasks=1,
        repeats=3,
    )

    assert no_optional_call.strict_success is True
    assert one_optional_call.strict_success is True
    assert repeated_optional_call.strict_success is False
    assert board.automatic.tool_selection_precision == pytest.approx(5 / 6)
    assert board.automatic.tool_selection_recall == 1.0
    assert 0 <= board.automatic.tool_selection_recall <= 1


def test_open_checkpoint_rejects_cross_model_or_changed_task(tmp_path: Path):
    from nanoscope.eval.agent_open_dataset import load_agent_open_v1
    from nanoscope.eval.agent_open_runner import load_open_checkpoint, task_sha256

    task = load_agent_open_v1()[0]
    raw = _record(task.id, 1).to_dict()
    raw["task_sha256"] = task_sha256(task)
    checkpoint = tmp_path / "open.jsonl"
    checkpoint.write_text(json.dumps(raw, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="model"):
        load_open_checkpoint(
            checkpoint,
            tasks=[task],
            model="different-model",
            provider="fake-provider",
            repeats=3,
        )


@pytest.mark.asyncio
async def test_open_runner_records_model_and_tool_stage_latency():
    from nanoscope.eval.agent_open_dataset import load_agent_open_v1
    from nanoscope.eval.agent_open_runner import run_open_agent_task

    task = next(task for task in load_agent_open_v1() if task.id == "OSL1")
    record = await run_open_agent_task(
        task,
        provider=TimedLookupProvider(),
        model="fake-model",
        provider_name="fake-provider",
        run_index=1,
    )

    assert record.result.strict_success is True
    assert record.result.model_latency_ms > 0
    assert record.result.tool_latency_ms > 0
    assert record.result.latency_ms >= (
        record.result.model_latency_ms + record.result.tool_latency_ms
    )


def test_human_review_template_and_summary_do_not_store_credentials(tmp_path: Path):
    from nanoscope.eval.agent_open_dataset import load_agent_open_v1
    from nanoscope.eval.agent_open_runner import (
        write_agent_open_artifact,
        write_human_review_template,
    )

    task = load_agent_open_v1()[0]
    records = [_record(task.id, run_index) for run_index in range(1, 4)]
    reviews = tmp_path / "reviews.jsonl"
    write_human_review_template(reviews, records, [task])
    review_rows = [
        json.loads(line) for line in reviews.read_text(encoding="utf-8").splitlines()
    ]
    assert all(row["status"] == "pending" for row in review_rows)
    assert all(row["correctness"] is None for row in review_rows)

    board = aggregate_open_agent_board(
        records,
        dataset_version="agent-open30.v1",
        n_tasks=1,
        repeats=3,
    )
    output = tmp_path / "AGENT_OPEN_V1.json"
    write_agent_open_artifact(
        output,
        board=board,
        model="fake-model",
        provider="fake-provider",
        dataset_sha256="dataset-hash",
        raw_trace_path="raw.jsonl",
        human_review_path="reviews.jsonl",
        credential_env_names=("DEEPSEEK_API_KEY",),
    )

    artifact = json.loads(output.read_text(encoding="utf-8"))
    serialized = json.dumps(artifact)
    assert artifact["gate"]["human_review_status"] == "pending"
    assert artifact["gate"]["overall"] is False
    assert artifact["run"]["credential_env_names"] == ["DEEPSEEK_API_KEY"]
    assert "must-not-appear-secret" not in serialized
    assert "records" not in artifact["board"]["automatic"]


def test_baseline_generator_registers_open_artifact_without_overwriting_live(
    tmp_path: Path,
):
    from nanoscope.eval.generate_baselines import generate_offline_baselines

    open_artifact = tmp_path / "AGENT_OPEN_V1.json"
    open_artifact.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "artifact_type": "agent_open",
                "run": {"dataset_version": "agent-open30.v1"},
                "gate": {"overall": False},
                "board": {},
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "baselines"

    outputs = generate_offline_baselines(
        Path(__file__).parents[2],
        output_dir,
        agent_open_path=open_artifact,
    )

    manifest = json.loads(outputs["manifest"].read_text(encoding="utf-8"))
    unified = json.loads(outputs["unified"].read_text(encoding="utf-8"))
    catalog = json.loads((output_dir / "EVAL_DATASETS_V1.json").read_text(encoding="utf-8"))
    assert manifest["artifacts"]["agent_open"]["gate"] is False
    assert unified["board"]["gates"]["agent_open"] is False
    assert unified["board"]["gates"]["overall"] is False
    assert catalog["datasets"]["agent_open"]["version"] == "agent-open30.v1"


@pytest.mark.asyncio
async def test_open_runner_concurrent_checkpoint_keeps_every_completed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from nanoscope.eval import agent_open_runner
    from nanoscope.eval.agent_open_dataset import load_agent_open_v1

    task = load_agent_open_v1()[0]

    async def fake_run(
        _task,
        *,
        provider,
        model,
        provider_name,
        run_index,
    ):
        del provider
        await asyncio.sleep(0.01 * (4 - run_index))
        result = _record(task.id, run_index)
        return OpenRunRecord(
            dataset_version=result.dataset_version,
            task_sha256=agent_open_runner.task_sha256(task),
            model=model,
            provider=provider_name,
            run_index=run_index,
            result=result.result,
        )

    monkeypatch.setattr(agent_open_runner, "run_open_agent_task", fake_run)
    checkpoint = tmp_path / "open.jsonl"
    records, _board = await agent_open_runner.run_agent_open_v1(
        provider=object(),
        model="fake-model",
        provider_name="fake-provider",
        checkpoint_path=checkpoint,
        repeats=3,
        tasks=[task],
        max_workers=2,
    )

    checkpoint_rows = [
        json.loads(line) for line in checkpoint.read_text(encoding="utf-8").splitlines()
    ]
    assert [record.run_index for record in records] == [1, 2, 3]
    assert [row["run_index"] for row in checkpoint_rows] == [1, 2, 3]
