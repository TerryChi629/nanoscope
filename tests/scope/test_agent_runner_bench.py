from __future__ import annotations

from collections import Counter

import pytest

from nanoscope.eval.agent_dataset import AGENT_DATASET_VERSION, load_agent_v1_tasks
from nanoscope.eval.agent_runner_bench import run_agent_task, run_agent_v1


def test_agent60_v1_has_frozen_shape():
    tasks = load_agent_v1_tasks()

    assert AGENT_DATASET_VERSION == "agent60.v1"
    assert len(tasks) == 60
    assert len({task.id for task in tasks}) == 60
    assert Counter(task.suite for task in tasks) == {
        "no_tool": 8,
        "single_tool": 12,
        "multi_tool": 12,
        "failure_recovery": 8,
        "multi_turn": 8,
        "compacted_context": 6,
        "clarify_refuse": 6,
    }


@pytest.mark.asyncio
async def test_agent_task_uses_real_runner_tool_loop():
    task = next(task for task in load_agent_v1_tasks() if task.suite == "multi_tool")

    record = await run_agent_task(task)

    assert record.strict_success is True
    assert record.actual_tools == ("lookup", "calculate")
    assert record.argument_checks == (True, True)
    assert record.prompt_tokens == 60
    assert record.completion_tokens == 14


@pytest.mark.asyncio
async def test_agent_v1_board_scores_all_frozen_tasks():
    records, board = await run_agent_v1()

    assert len(records) == 60
    assert board.n_eligible == 60
    assert board.n_success == 60
    assert board.strict_tsr == 1.0
    assert board.tool_selection_precision == 1.0
    assert board.tool_selection_recall == 1.0
    assert board.context_fidelity == 1.0
