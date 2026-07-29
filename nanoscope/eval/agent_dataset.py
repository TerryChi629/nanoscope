"""Versioned synthetic Agent v1 dataset used by the deterministic runner benchmark."""

from __future__ import annotations

from dataclasses import dataclass

AGENT_DATASET_VERSION = "agent60.v1"


@dataclass(frozen=True)
class AgentTask:
    id: str
    suite: str
    prompt: str
    expected_answer: str
    expected_tools: tuple[str, ...] = ()
    expected_arguments: tuple[dict[str, str], ...] = ()
    context_facts: tuple[str, ...] = ()


def _tasks(
    suite: str,
    count: int,
    *,
    tools: tuple[str, ...] = (),
    context: bool = False,
) -> list[AgentTask]:
    prefix = {
        "no_tool": "NT",
        "single_tool": "ST",
        "multi_tool": "MT",
        "failure_recovery": "FR",
        "multi_turn": "MC",
        "compacted_context": "CC",
        "clarify_refuse": "CR",
    }[suite]
    records: list[AgentTask] = []
    for index in range(1, count + 1):
        answer = f"{suite}-answer-{index}"
        arguments = tuple(
            {"key": f"{suite}-{index}-{tool_index}"}
            for tool_index, _tool in enumerate(tools, start=1)
        )
        facts = (f"context-fact-{index}",) if context else ()
        records.append(
            AgentTask(
                id=f"{prefix}{index:02d}",
                suite=suite,
                prompt=f"Complete deterministic task {suite} #{index}.",
                expected_answer=answer,
                expected_tools=tools,
                expected_arguments=arguments,
                context_facts=facts,
            )
        )
    return records


def load_agent_v1_tasks() -> list[AgentTask]:
    """Return the frozen 60-case shape; changing it requires a new dataset version."""
    tasks = [
        *_tasks("no_tool", 8),
        *_tasks("single_tool", 12, tools=("lookup",)),
        *_tasks("multi_tool", 12, tools=("lookup", "calculate")),
        *_tasks("failure_recovery", 8, tools=("lookup",)),
        *_tasks("multi_turn", 8, tools=("lookup",), context=True),
        *_tasks("compacted_context", 6, context=True),
        *_tasks("clarify_refuse", 6),
    ]
    if len(tasks) != 60 or len({task.id for task in tasks}) != 60:
        raise AssertionError("agent60.v1 dataset shape changed")
    return tasks
