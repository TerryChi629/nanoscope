"""Three-run open Agent benchmark with fingerprinted resume and human review."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from nanobot.providers.base import GenerationSettings
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanoscope.eval.agent_live_dataset import LiveAgentTask
from nanoscope.eval.agent_live_runner import run_live_agent_task
from nanoscope.eval.agent_open_bench import (
    HumanReview,
    OpenAgentBoard,
    OpenRunRecord,
    aggregate_open_agent_board,
)
from nanoscope.eval.agent_open_dataset import (
    AGENT_OPEN_DATASET_VERSION,
    OpenAgentTask,
    load_agent_open_v1,
)
from nanoscope.eval.artifacts import atomic_write_json, atomic_write_jsonl

OPEN_SYSTEM_PROMPT = (
    "你正在执行脱敏开放 Agent 评测。先理解用户目标，再决定是否调用工具；"
    "严格使用工具 schema，不补造事实；遇到瞬态错误只做必要恢复；"
    "最终答案应正确、完整、遵循指令，并解释关键推理或工具结果。"
)


def task_sha256(task: OpenAgentTask) -> str:
    payload = json.dumps(asdict(task), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def run_open_agent_task(
    task: OpenAgentTask,
    *,
    provider: Any,
    model: str,
    provider_name: str,
    run_index: int,
) -> OpenRunRecord:
    live_task = LiveAgentTask(
        id=task.id,
        suite=task.suite,
        prompt=task.prompt,
        expected_facts=task.expected_facts,
        expected_tools=task.expected_tools,
        expected_arguments=task.expected_arguments,
        context_facts=task.context_facts,
        recovery_expected=task.recovery_expected,
        expected_any=task.expected_any,
        allowed_tools=task.allowed_tools,
    )
    result = await run_live_agent_task(
        live_task,
        provider=provider,
        model=model,
        conversation_messages=[asdict(message) for message in task.messages],
        system_prompt=OPEN_SYSTEM_PROMPT,
    )
    return OpenRunRecord(
        dataset_version=AGENT_OPEN_DATASET_VERSION,
        task_sha256=task_sha256(task),
        model=model,
        provider=provider_name,
        run_index=run_index,
        result=result,
    )


async def run_agent_open_v1(
    *,
    provider: Any,
    model: str,
    provider_name: str,
    checkpoint_path: str | Path,
    repeats: int = 3,
    resume: bool = False,
    tasks: Sequence[OpenAgentTask] | None = None,
    reviews: Sequence[HumanReview] = (),
    max_workers: int = 2,
) -> tuple[list[OpenRunRecord], OpenAgentBoard]:
    if repeats < 3:
        raise ValueError("P1-B requires at least 3 repeated runs")
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    frozen_tasks = list(tasks or load_agent_open_v1())
    completed = (
        load_open_checkpoint(
            checkpoint_path,
            tasks=frozen_tasks,
            model=model,
            provider=provider_name,
            repeats=repeats,
        )
        if resume
        else {}
    )
    ordered_keys = [
        (task.id, run_index)
        for run_index in range(1, repeats + 1)
        for task in frozen_tasks
    ]
    task_by_id = {task.id: task for task in frozen_tasks}
    semaphore = asyncio.Semaphore(max_workers)
    checkpoint_lock = asyncio.Lock()

    async def run_missing(case_id: str, run_index: int) -> None:
        key = (case_id, run_index)
        if key in completed:
            return
        async with semaphore:
            record = await run_open_agent_task(
                task_by_id[case_id],
                provider=provider,
                model=model,
                provider_name=provider_name,
                run_index=run_index,
            )
        async with checkpoint_lock:
            completed[key] = record
            atomic_write_jsonl(
                checkpoint_path,
                [completed[item].to_dict() for item in ordered_keys if item in completed],
            )

    await asyncio.gather(
        *(run_missing(case_id, run_index) for case_id, run_index in ordered_keys)
    )
    records = [completed[key] for key in ordered_keys]
    return records, aggregate_open_agent_board(
        records,
        dataset_version=AGENT_OPEN_DATASET_VERSION,
        n_tasks=len(frozen_tasks),
        repeats=repeats,
        reviews=reviews,
    )


def load_open_checkpoint(
    path: str | Path,
    *,
    tasks: Sequence[OpenAgentTask],
    model: str,
    provider: str,
    repeats: int,
) -> dict[tuple[str, int], OpenRunRecord]:
    checkpoint = Path(path)
    if not checkpoint.exists():
        return {}
    task_by_id = {task.id: task for task in tasks}
    records: dict[tuple[str, int], OpenRunRecord] = {}
    for line_number, line in enumerate(
        checkpoint.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        raw = json.loads(line)
        case_id = raw.get("result", {}).get("case_id")
        run_index = raw.get("run_index")
        if case_id not in task_by_id or not isinstance(run_index, int):
            raise ValueError(f"Invalid open Agent checkpoint record at line {line_number}")
        expected = {
            "dataset_version": AGENT_OPEN_DATASET_VERSION,
            "task_sha256": task_sha256(task_by_id[case_id]),
            "model": model,
            "provider": provider,
        }
        mismatches = [
            name for name, value in expected.items() if raw.get(name) != value
        ]
        if mismatches or run_index < 1 or run_index > repeats:
            details = ", ".join(mismatches or ["run_index"])
            raise ValueError(
                f"Open Agent checkpoint fingerprint mismatch at line "
                f"{line_number}: {details}"
            )
        result_data = raw["result"]
        result_data["expected_tools"] = tuple(result_data.get("expected_tools", ()))
        result_data["allowed_tools"] = tuple(result_data.get("allowed_tools", ()))
        result_data["actual_tools"] = tuple(result_data.get("actual_tools", ()))
        result_data["argument_checks"] = tuple(result_data.get("argument_checks", ()))
        result_data["actual_arguments"] = tuple(result_data.get("actual_arguments", ()))
        result_data["tool_statuses"] = tuple(result_data.get("tool_statuses", ()))
        from nanoscope.eval.agent_bench import AgentCaseResult

        record = OpenRunRecord(
            dataset_version=raw["dataset_version"],
            task_sha256=raw["task_sha256"],
            model=raw["model"],
            provider=raw["provider"],
            run_index=run_index,
            result=AgentCaseResult(**result_data),
        )
        key = (case_id, run_index)
        if key in records:
            raise ValueError(f"Duplicate open Agent checkpoint record: {key}")
        records[key] = record
    return records


def load_human_reviews(path: str | Path) -> list[HumanReview]:
    review_path = Path(path)
    if not review_path.exists():
        return []
    reviews: list[HumanReview] = []
    for line_number, line in enumerate(
        review_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        raw = json.loads(line)
        if raw.get("status") != "completed":
            continue
        review = HumanReview(
            **{
                key: raw[key]
                for key in (
                    "case_id",
                    "run_index",
                    "correctness",
                    "completeness",
                    "instruction_following",
                    "tool_rationality",
                    "reviewer",
                    "notes",
                )
            }
        )
        scores = (
            review.correctness,
            review.completeness,
            review.instruction_following,
            review.tool_rationality,
        )
        if any(not isinstance(score, int) or not 0 <= score <= 4 for score in scores):
            raise ValueError(f"Invalid human score at line {line_number}")
        if not review.reviewer.strip():
            raise ValueError(f"Missing reviewer at line {line_number}")
        reviews.append(review)
    return reviews


def write_human_review_template(
    path: str | Path,
    records: Sequence[OpenRunRecord],
    tasks: Sequence[OpenAgentTask],
) -> None:
    task_by_id = {task.id: task for task in tasks}
    atomic_write_jsonl(
        path,
        [
            {
                "case_id": record.result.case_id,
                "run_index": record.run_index,
                "status": "pending",
                "rubric": list(task_by_id[record.result.case_id].rubric),
                "answer": record.result.answer,
                "correctness": None,
                "completeness": None,
                "instruction_following": None,
                "tool_rationality": None,
                "reviewer": "",
                "notes": "",
            }
            for record in records
        ],
    )


def write_agent_open_artifact(
    path: str | Path,
    *,
    board: OpenAgentBoard,
    model: str,
    provider: str,
    dataset_sha256: str,
    raw_trace_path: str | Path,
    human_review_path: str | Path,
    credential_env_names: tuple[str, ...],
) -> None:
    atomic_write_json(
        path,
        {
            "schema_version": "1.0",
            "artifact_type": "agent_open",
            "run": {
                "started_at": datetime.now(UTC).isoformat(),
                "model": model,
                "provider": provider,
                "dataset_version": board.dataset_version,
                "dataset_sha256": dataset_sha256,
                "repeats": board.repeats,
                "credential_env_names": sorted(credential_env_names),
                "raw_trace_path": str(raw_trace_path),
                "human_review_path": str(human_review_path),
            },
            "gate": {
                "automatic": board.automatic_gate_pass,
                "human_review_status": board.human_review_status,
                "human": board.human_gate_pass,
                "overall": board.overall_gate_pass,
            },
            "board": board.to_summary_dict(),
        },
    )


async def _async_main(args: argparse.Namespace) -> int:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("缺少 DEEPSEEK_API_KEY；P1-B 真实开放 Agent 测评未执行")
        return 2
    model = args.model or os.environ.get("DEEPSEEK_MODEL") or "deepseek-v4-flash"
    provider_name = "deepseek"
    provider = OpenAICompatProvider(
        api_key=api_key,
        api_base=os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
        default_model=model,
    )
    provider.generation = GenerationSettings(temperature=0.2, max_tokens=1024)
    tasks = load_agent_open_v1()
    reviews = load_human_reviews(args.human_review)
    records, board = await run_agent_open_v1(
        provider=provider,
        model=model,
        provider_name=provider_name,
        checkpoint_path=args.raw_trace,
        repeats=args.repeats,
        resume=args.resume,
        tasks=tasks,
        reviews=reviews,
        max_workers=args.max_workers,
    )
    if not reviews:
        write_human_review_template(args.human_review, records, tasks)
    dataset_path = Path(__file__).with_name("agent_open_dataset.py")
    write_agent_open_artifact(
        args.out,
        board=board,
        model=model,
        provider=provider_name,
        dataset_sha256=hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        raw_trace_path=args.raw_trace,
        human_review_path=args.human_review,
        credential_env_names=("DEEPSEEK_API_KEY",),
    )
    print(
        f"Agent open: {board.n_runs}/{board.n_tasks * board.repeats} runs, "
        f"strict TSR={board.strict_tsr.mean:.3f} "
        f"[{board.strict_tsr.ci95_low:.3f}, {board.strict_tsr.ci95_high:.3f}], "
        f"human={board.human_review_status}, overall={board.overall_gate_pass}"
    )
    return 0 if board.automatic_gate_pass else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="reports/baselines/AGENT_OPEN_V1.json")
    parser.add_argument(
        "--raw-trace",
        default="reports/checkpoints/agent_open/agent-open30.v1.jsonl",
    )
    parser.add_argument(
        "--human-review",
        default="reports/reviews/AGENT_OPEN_V1_HUMAN.jsonl",
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--max-workers", type=int, default=2)
    return asyncio.run(_async_main(parser.parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
