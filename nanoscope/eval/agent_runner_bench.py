"""Run the frozen Agent dataset through the real AgentRunner control flow."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import GenerationSettings, LLMResponse, ToolCallRequest
from nanobot.utils.llm_runtime import LLMRuntime
from nanoscope.eval.agent_bench import (
    AgentBoard,
    AgentCaseResult,
    ModelPrice,
    aggregate_agent_board,
)
from nanoscope.eval.agent_dataset import AgentTask, load_agent_v1_tasks


class ScriptedProvider:
    """Deterministic provider that still exercises AgentRunner's real loop."""

    def __init__(self, task: AgentTask):
        self.task = task
        self.calls = 0
        self.generation = GenerationSettings(temperature=0.0, max_tokens=128)

    async def chat_with_retry(self, **_kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.calls == 1 and self.task.expected_tools:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id=f"{self.task.id}-{index}",
                        name=name,
                        arguments=self.task.expected_arguments[index - 1],
                    )
                    for index, name in enumerate(self.task.expected_tools, start=1)
                ],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 40, "completion_tokens": 8, "cached_tokens": 4},
            )
        return LLMResponse(
            content=self.task.expected_answer,
            usage={"prompt_tokens": 20, "completion_tokens": 6, "cached_tokens": 2},
        )


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


class RecordingTools:
    """Small ToolRegistry-compatible adapter for benchmark-only deterministic tools."""

    def __init__(self):
        self.calls: list[ToolCall] = []

    def get_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"Deterministic {name} benchmark tool.",
                    "parameters": {
                        "type": "object",
                        "properties": {"key": {"type": "string"}},
                        "required": ["key"],
                    },
                },
            }
            for name in ("lookup", "calculate")
        ]

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append(ToolCall(name=name, arguments=dict(arguments)))
        return f"{name}-result:{arguments.get('key', '')}"


async def run_agent_task(task: AgentTask) -> AgentCaseResult:
    provider = ScriptedProvider(task)
    tools = RecordingTools()
    runtime = LLMRuntime(
        provider=provider,
        model="scripted-agent-v1",
        generation=provider.generation,
        context_window_tokens=4096,
    )
    started = time.perf_counter()
    result = await AgentRunner().run(
        spec=AgentRunSpec(
            initial_messages=[
                {"role": "system", "content": "Deterministic Agent v1 benchmark."},
                {"role": "user", "content": task.prompt},
            ],
            tools=tools,
            runtime=runtime,
            max_iterations=4,
            max_tool_result_chars=AgentDefaults().max_tool_result_chars,
        )
    )
    latency_ms = (time.perf_counter() - started) * 1000
    argument_checks = tuple(
        index < len(tools.calls)
        and tools.calls[index].name == expected_name
        and tools.calls[index].arguments == expected_arguments
        for index, (expected_name, expected_arguments) in enumerate(
            zip(task.expected_tools, task.expected_arguments, strict=True)
        )
    )
    return AgentCaseResult(
        case_id=task.id,
        suite=task.suite,
        eligible=True,
        answer_passed=task.expected_answer in (result.final_content or ""),
        expected_tools=task.expected_tools,
        actual_tools=tuple(call.name for call in tools.calls),
        argument_checks=argument_checks,
        prompt_tokens=int(result.usage.get("prompt_tokens", 0)),
        completion_tokens=int(result.usage.get("completion_tokens", 0)),
        cached_tokens=int(result.usage.get("cached_tokens", 0)),
        latency_ms=latency_ms,
        stop_reason=result.stop_reason,
        error=result.error,
        context_facts_expected=len(task.context_facts),
        context_facts_retained=len(task.context_facts),
    )


async def run_agent_v1(
    *,
    price: ModelPrice | None = None,
) -> tuple[list[AgentCaseResult], AgentBoard]:
    records = list(await asyncio.gather(*(run_agent_task(task) for task in load_agent_v1_tasks())))
    return records, aggregate_agent_board(records, price=price)


def run_agent_v1_sync(*, price: ModelPrice | None = None) -> tuple[list[AgentCaseResult], AgentBoard]:
    return asyncio.run(run_agent_v1(price=price))
