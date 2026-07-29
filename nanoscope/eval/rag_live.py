"""Resumable scheduling primitives for long-running RAG live evaluation."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from nanoscope.eval.artifacts import atomic_write_jsonl

T = TypeVar("T")


@dataclass(frozen=True)
class LevelSpec:
    name: str
    required_env: tuple[str, ...]

    @property
    def available(self) -> bool:
        return all(os.environ.get(name) for name in self.required_env)


_LEVELS = {
    "L0": LevelSpec("L0", ()),
    "L1": LevelSpec("L1", ("GLM_API_KEY",)),
    "L2": LevelSpec("L2", ("GLM_API_KEY", "SILICONFLOW_API_KEY")),
    "L3": LevelSpec(
        "L3",
        ("GLM_API_KEY", "SILICONFLOW_API_KEY", "DEEPSEEK_API_KEY"),
    ),
}


def build_level_plan(levels: Sequence[str]) -> tuple[LevelSpec, ...]:
    """Validate and preserve an explicit execution order without adding levels."""
    normalized = tuple(level.upper() for level in levels)
    unknown = [level for level in normalized if level not in _LEVELS]
    if unknown:
        raise ValueError(f"Unknown RAG levels: {', '.join(unknown)}")
    if len(set(normalized)) != len(normalized):
        raise ValueError("RAG levels must not contain duplicates")
    return tuple(_LEVELS[level] for level in normalized)


def load_checkpoint(path: str | Path) -> dict[str, dict[str, Any]]:
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
        scenario_id = record.get("id")
        if not isinstance(scenario_id, str) or not scenario_id:
            raise ValueError(f"Invalid checkpoint record at line {line_number}")
        records[scenario_id] = record
    return records


def run_scenarios_resumable(
    *,
    level: str,
    scenarios: Iterable[T],
    run_one: Callable[[T], Mapping[str, Any]],
    checkpoint_path: str | Path,
    resume: bool,
    max_workers: int = 1,
) -> list[dict[str, Any]]:
    """Run bounded work and atomically checkpoint after every completed scenario."""
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    ordered = list(scenarios)
    scenario_ids = [str(getattr(scenario, "id")) for scenario in ordered]
    if len(set(scenario_ids)) != len(scenario_ids):
        raise ValueError(f"{level} contains duplicate scenario ids")

    completed = load_checkpoint(checkpoint_path) if resume else {}
    pending = [
        scenario for scenario in ordered
        if str(getattr(scenario, "id")) not in completed
    ]

    def persist() -> None:
        records = [completed[item_id] for item_id in scenario_ids if item_id in completed]
        atomic_write_jsonl(checkpoint_path, records)

    if max_workers == 1:
        for scenario in pending:
            record = dict(run_one(scenario))
            record.setdefault("id", str(getattr(scenario, "id")))
            completed[record["id"]] = record
            persist()
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(run_one, scenario): scenario for scenario in pending}
            for future in as_completed(futures):
                scenario = futures[future]
                record = dict(future.result())
                record.setdefault("id", str(getattr(scenario, "id")))
                completed[record["id"]] = record
                persist()

    return [completed[item_id] for item_id in scenario_ids if item_id in completed]
