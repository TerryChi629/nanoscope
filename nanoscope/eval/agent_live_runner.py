"""Real-model AgentRunner benchmark with deterministic tools and resumable evidence."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import GenerationSettings
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.utils.llm_runtime import LLMRuntime
from nanoscope.eval.agent_bench import AgentBoard, AgentCaseResult, aggregate_agent_board
from nanoscope.eval.agent_live_dataset import (
    AGENT_LIVE_DATASET_VERSION,
    LiveAgentTask,
    load_agent_live_v1,
)
from nanoscope.eval.artifacts import atomic_write_json, atomic_write_jsonl

_LOOKUP_VALUES: dict[str, object] = {
    "region": "华东",
    "owner": "林舟",
    "timeout": 30,
    "mode": "灰度",
    "base": 12,
    "replicas": 3,
    "budget": 120,
    "latency": 45,
}
_UNSTABLE_VALUES: dict[str, object] = {
    "quota": 128,
    "index": "search-v2",
    "rotation": 90,
    "batch": 64,
}


@dataclass(frozen=True)
class ToolAttempt:
    name: str
    arguments: dict[str, Any]
    status: str
    latency_ms: float = 0.0


class TimedProvider:
    """Measure model wait time without changing the provider contract."""

    def __init__(self, delegate: Any):
        self._delegate = delegate
        self.model_latency_ms = 0.0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def chat_with_retry(self, **kwargs: Any):
        started = time.perf_counter()
        try:
            return await self._delegate.chat_with_retry(**kwargs)
        finally:
            self.model_latency_ms += (time.perf_counter() - started) * 1000


class BenchmarkTool(Tool):
    def __init__(self, name: str, description: str, parameters: dict[str, Any], execute):
        self._name = name
        self._description = description
        self._parameters = parameters
        self._execute = execute

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> Any:
        return self._execute(**kwargs)


class LiveToolHarness:
    def __init__(self):
        self.attempts: list[ToolAttempt] = []
        self.tool_latency_ms = 0.0
        self._unstable_counts: Counter[str] = Counter()
        self.registry = ToolRegistry()
        key_schema = {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "enum": list(_LOOKUP_VALUES),
                    "description": "配置键：region/owner/timeout/mode/base/replicas/budget/latency",
                }
            },
            "required": ["key"],
            "additionalProperties": False,
        }
        self.registry.register(
            BenchmarkTool(
                "lookup",
                "查询冻结评测配置。参数 key 必须来自用户问题。",
                key_schema,
                self._lookup,
            )
        )
        self.registry.register(
            BenchmarkTool(
                "calculate",
                "执行确定性算术。operation 只能是 add/subtract/multiply/divide。",
                {
                    "type": "object",
                    "properties": {
                        "left": {"type": "number"},
                        "right": {"type": "number"},
                        "operation": {
                            "type": "string",
                            "enum": ["add", "subtract", "multiply", "divide"],
                        },
                    },
                    "required": ["left", "right", "operation"],
                    "additionalProperties": False,
                },
                self._calculate,
            )
        )
        self.registry.register(
            BenchmarkTool(
                "unstable_lookup",
                "查询可能瞬态失败的配置。若明确返回 transient error，原参数重试一次。",
                {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "enum": list(_UNSTABLE_VALUES),
                            "description": "不稳定配置键：quota/index/rotation/batch",
                        }
                    },
                    "required": ["key"],
                    "additionalProperties": False,
                },
                self._unstable_lookup,
            )
        )

    def _lookup(self, key: str) -> Any:
        started = time.perf_counter()
        if key not in _LOOKUP_VALUES:
            result = ToolResult.error(f"unknown key: {key}")
            self._record_attempt("lookup", {"key": key}, "error", started)
            return result
        value = _LOOKUP_VALUES[key]
        self._record_attempt("lookup", {"key": key}, "ok", started)
        return f"{key}={value}"

    def _calculate(self, left: float, right: float, operation: str) -> Any:
        started = time.perf_counter()
        arguments = {"left": left, "right": right, "operation": operation}
        try:
            result = {
                "add": left + right,
                "subtract": left - right,
                "multiply": left * right,
                "divide": left / right,
            }[operation]
        except (KeyError, ZeroDivisionError) as exc:
            self._record_attempt("calculate", arguments, "error", started)
            return ToolResult.error(str(exc))
        self._record_attempt("calculate", arguments, "ok", started)
        return f"result={result:g}"

    def _unstable_lookup(self, key: str) -> Any:
        started = time.perf_counter()
        arguments = {"key": key}
        self._unstable_counts[key] += 1
        if self._unstable_counts[key] == 1:
            self._record_attempt("unstable_lookup", arguments, "error", started)
            return ToolResult.error("transient error: retry the same call once")
        if key not in _UNSTABLE_VALUES:
            self._record_attempt("unstable_lookup", arguments, "error", started)
            return ToolResult.error(f"unknown key: {key}")
        self._record_attempt("unstable_lookup", arguments, "ok", started)
        return f"{key}={_UNSTABLE_VALUES[key]}"

    def _record_attempt(
        self,
        name: str,
        arguments: dict[str, Any],
        status: str,
        started: float,
    ) -> None:
        latency_ms = (time.perf_counter() - started) * 1000
        self.tool_latency_ms += latency_ms
        self.attempts.append(ToolAttempt(name, arguments, status, latency_ms))


async def run_live_agent_task(
    task: LiveAgentTask,
    *,
    provider,
    model: str,
    conversation_messages: list[dict[str, Any]] | None = None,
    system_prompt: str | None = None,
) -> AgentCaseResult:
    """Execute one constrained task through the real AgentRunner and ToolRegistry."""
    tools = LiveToolHarness()
    generation = getattr(
        provider,
        "generation",
        GenerationSettings(temperature=0.0, max_tokens=1024),
    )
    timed_provider = TimedProvider(provider)
    runtime = LLMRuntime(
        provider=timed_provider,
        model=model,
        generation=generation,
        context_window_tokens=8192,
    )
    started = time.perf_counter()
    messages = conversation_messages or [{"role": "user", "content": task.prompt}]
    result = await AgentRunner().run(
        spec=AgentRunSpec(
            initial_messages=[
                {
                    "role": "system",
                    "content": system_prompt or (
                        "你正在执行可审计 Agent 评测。只在任务需要时调用工具；"
                        "严格使用工具 schema；工具瞬态失败时根据提示恢复；"
                        "最终答案简洁并包含问题要求的事实。"
                    ),
                },
                *messages,
            ],
            tools=tools.registry,
            runtime=runtime,
            max_iterations=6,
            max_tool_result_chars=AgentDefaults().max_tool_result_chars,
            provider_retry_mode="standard",
        )
    )
    latency_ms = (time.perf_counter() - started) * 1000
    answer = result.final_content or ""
    actual_tools = tuple(attempt.name for attempt in tools.attempts)
    actual_arguments = tuple(attempt.arguments for attempt in tools.attempts)
    argument_checks = tuple(
        index < len(actual_arguments) and actual_arguments[index] == expected
        for index, expected in enumerate(task.expected_arguments)
    )
    expected_counts = Counter(task.expected_tools)
    allowed_counts = Counter(task.allowed_tools)
    actual_counts = Counter(actual_tools)
    matched_tools = sum(
        min(count, actual_counts.get(name, 0))
        for name, count in expected_counts.items()
    )
    tool_recall = matched_tools / len(task.expected_tools) if task.expected_tools else 1.0
    argument_score = (
        sum(argument_checks) / len(argument_checks) if argument_checks else 1.0
    )
    normalized_answer = answer.casefold()
    answer_passed = all(
        fact.casefold() in normalized_answer for fact in task.expected_facts
    ) and (
        not task.expected_any
        or any(marker.casefold() in normalized_answer for marker in task.expected_any)
    )
    expected_index = 0
    for name in actual_tools:
        if expected_index < len(task.expected_tools) and name == task.expected_tools[expected_index]:
            expected_index += 1
    errors = [attempt for attempt in tools.attempts if attempt.status == "error"]
    recovered = bool(
        task.recovery_expected
        and errors
        and any(attempt.status == "ok" for attempt in tools.attempts)
        and answer_passed
    )
    unhandled_errors = 0 if not errors or recovered else len(errors)
    unexpected_tools = sum(
        max(
            0,
            count - expected_counts.get(name, 0) - allowed_counts.get(name, 0),
        )
        for name, count in actual_counts.items()
    )
    sequence_passed = (
        expected_index == len(task.expected_tools) and unexpected_tools == 0
    )
    completed = result.stop_reason == "completed" and result.error is None
    partial_score = (
        float(answer_passed)
        + tool_recall
        + argument_score
        + float(completed)
    ) / 4
    expected_context = task.context_facts
    retained_context = sum(
        1 for fact in expected_context if fact.casefold() in answer.casefold()
    )
    return AgentCaseResult(
        case_id=task.id,
        suite=task.suite,
        eligible=True,
        answer_passed=answer_passed,
        expected_tools=task.expected_tools,
        allowed_tools=task.allowed_tools,
        actual_tools=actual_tools,
        argument_checks=argument_checks,
        forbidden_tool_calls=unexpected_tools,
        unhandled_tool_errors=unhandled_errors,
        prompt_tokens=int(result.usage.get("prompt_tokens", 0)),
        completion_tokens=int(result.usage.get("completion_tokens", 0)),
        cached_tokens=int(result.usage.get("cached_tokens", 0)),
        usage_estimated=bool(
            result.usage.get("estimated_tokens")
            and not result.usage.get("provider_tokens")
        ),
        latency_ms=latency_ms,
        model_latency_ms=timed_provider.model_latency_ms,
        tool_latency_ms=tools.tool_latency_ms,
        stop_reason=result.stop_reason,
        context_facts_expected=len(expected_context),
        context_facts_retained=retained_context,
        error=result.error,
        partial_score=partial_score,
        tool_sequence_passed=sequence_passed,
        recovery_expected=task.recovery_expected,
        recovered=recovered,
        max_iteration_terminated=result.stop_reason == "max_iterations",
        answer=answer,
        actual_arguments=actual_arguments,
        tool_statuses=tuple(attempt.status for attempt in tools.attempts),
    )


def write_agent_live_artifact(
    path: str | Path,
    *,
    board: AgentBoard,
    model: str,
    provider: str,
    dataset_version: str,
    credential_env_names: tuple[str, ...],
    secret_values: tuple[str, ...] = (),
    dataset_sha256: str | None = None,
) -> None:
    """Write a sanitized summary; secret_values exists only for negative testing."""
    del secret_values
    atomic_write_json(
        path,
        {
            "schema_version": "1.0",
            "artifact_type": "agent_live",
            "run": {
                "started_at": datetime.now(UTC).isoformat(),
                "model": model,
                "provider": provider,
                "dataset_version": dataset_version,
                "dataset_sha256": dataset_sha256,
                "credential_env_names": list(sorted(credential_env_names)),
            },
            "gate": {
                "complete": board.n_skipped == 0 and board.n_errored == 0,
                "minimum_strict_tsr": 0.8,
                "strict_tsr_pass": board.strict_tsr >= 0.8,
                "overall": (
                    board.n_skipped == 0
                    and board.n_errored == 0
                    and board.strict_tsr >= 0.8
                ),
            },
            "board": board.to_summary_dict(),
        },
    )


async def run_agent_live_v1(
    *,
    provider,
    model: str,
    checkpoint_path: str | Path,
    resume: bool,
) -> tuple[list[AgentCaseResult], AgentBoard]:
    tasks = load_agent_live_v1()
    completed = _load_agent_checkpoint(checkpoint_path) if resume else {}
    for task in tasks:
        if task.id in completed:
            continue
        record = await run_live_agent_task(task, provider=provider, model=model)
        completed[task.id] = asdict(record)
        atomic_write_jsonl(
            checkpoint_path,
            [completed[item.id] for item in tasks if item.id in completed],
        )
    records = [AgentCaseResult(**completed[task.id]) for task in tasks]
    return records, aggregate_agent_board(records)


def _load_agent_checkpoint(path: str | Path) -> dict[str, dict[str, Any]]:
    checkpoint = Path(path)
    if not checkpoint.exists():
        return {}
    records: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(
        checkpoint.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        record = json.loads(line)
        case_id = record.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"Invalid Agent checkpoint record at line {line_number}")
        records[case_id] = record
    return records


async def _async_main(args: argparse.Namespace) -> int:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("缺少 DEEPSEEK_API_KEY；真实 Agent 测评未执行")
        return 2
    model = args.model or os.environ.get("DEEPSEEK_MODEL") or "deepseek-v4-flash"
    base_url = os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    provider = OpenAICompatProvider(
        api_key=api_key,
        api_base=base_url,
        default_model=model,
    )
    provider.generation = GenerationSettings(temperature=0.0, max_tokens=1024)
    _records, board = await run_agent_live_v1(
        provider=provider,
        model=model,
        checkpoint_path=args.checkpoint,
        resume=args.resume,
    )
    write_agent_live_artifact(
        args.out,
        board=board,
        model=model,
        provider="deepseek",
        dataset_version=AGENT_LIVE_DATASET_VERSION,
        dataset_sha256=hashlib.sha256(
            (
                Path(__file__).with_name("agent_live_dataset.py")
            ).read_bytes()
        ).hexdigest(),
        credential_env_names=("DEEPSEEK_API_KEY",),
    )
    print(
        f"Agent live: {board.n_success}/{board.n_eligible}, "
        f"strict TSR={board.strict_tsr:.3f}, partial TSR={board.partial_tsr:.3f}, "
        f"tool P/R={board.tool_selection_precision:.3f}/"
        f"{board.tool_selection_recall:.3f}, P95={board.latency_p95_ms:.2f}ms"
    )
    print(f"summary: {args.out}")
    print(f"checkpoint: {args.checkpoint}")
    return 0 if board.n_errored == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="reports/baselines/AGENT_LIVE_V1.json")
    parser.add_argument(
        "--checkpoint",
        default="reports/checkpoints/agent_live/agent-live24.v2.jsonl",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--model")
    return asyncio.run(_async_main(parser.parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
