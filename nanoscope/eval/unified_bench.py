"""Agent、安全、RAG 三套正交看板的统一运行清单与证据输出。"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nanoscope.eval.agent_bench import AgentBoard
from nanoscope.eval.rag_bench import Board as RagBoard
from nanoscope.eval.security_bench import SecurityBoard

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    started_at: str
    git_commit: str
    dirty_worktree: bool
    dataset_version: str
    dataset_sha256: str
    metric_schema_version: str
    model: str
    provider: str
    price_table_version: str | None
    random_seed: int
    python_version: str
    platform: str
    credential_env_names: tuple[str, ...]


@dataclass
class UnifiedBoard:
    manifest: RunManifest
    agent: AgentBoard | None
    security: SecurityBoard
    rag: RagBoard | None
    agent_gate_pass: bool | None
    security_gate_pass: bool
    security_completeness_pass: bool
    rag_gate_pass: bool | None
    overall_gate_pass: bool

    def to_dict(self, *, include_records: bool = False) -> dict[str, Any]:
        def board_data(board):
            if board is None:
                return None
            data = asdict(board)
            if not include_records:
                data.pop("records", None)
            return data

        return {
            "schema_version": SCHEMA_VERSION,
            "manifest": asdict(self.manifest),
            "gates": {
                "agent": self.agent_gate_pass,
                "security": self.security_gate_pass,
                "security_completeness": self.security_completeness_pass,
                "rag": self.rag_gate_pass,
                "overall": self.overall_gate_pass,
            },
            "agent": board_data(self.agent),
            "security": board_data(self.security),
            "rag": board_data(self.rag),
        }


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_value(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip()


def build_manifest(
    *,
    repo: str | Path,
    dataset_path: str | Path,
    dataset_version: str,
    model: str,
    provider: str,
    random_seed: int,
    credential_env_names: tuple[str, ...] = (),
    price_table_version: str | None = None,
    now: datetime | None = None,
) -> RunManifest:
    """生成可复现实验清单，只记录凭证变量名，绝不记录凭证值。"""
    repo_path = Path(repo)
    timestamp = (now or datetime.now(UTC)).isoformat()
    commit = _git_value(repo_path, "rev-parse", "HEAD")
    dirty = bool(_git_value(repo_path, "status", "--porcelain"))
    run_material = f"{timestamp}|{commit}|{dataset_version}|{model}|{random_seed}"
    run_id = hashlib.sha256(run_material.encode()).hexdigest()[:16]
    return RunManifest(
        run_id=run_id,
        started_at=timestamp,
        git_commit=commit,
        dirty_worktree=dirty,
        dataset_version=dataset_version,
        dataset_sha256=sha256_file(dataset_path),
        metric_schema_version=SCHEMA_VERSION,
        model=model,
        provider=provider,
        price_table_version=price_table_version,
        random_seed=random_seed,
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        credential_env_names=tuple(sorted(credential_env_names)),
    )


def aggregate_unified_board(
    manifest: RunManifest,
    security: SecurityBoard,
    *,
    agent: AgentBoard | None = None,
    rag: RagBoard | None = None,
    minimum_agent_tsr: float = 0.0,
) -> UnifiedBoard:
    """三套看板保持独立；总门禁只做布尔合取，不计算掩盖红线的综合分。"""
    agent_gate = None if agent is None else (
        agent.n_errored == 0 and agent.strict_tsr >= minimum_agent_tsr
    )
    rag_gate = None if rag is None else (
        rag.security_gate_pass and rag.reliability_gate_pass
    )
    required = [
        security.security_gate_pass,
        security.completeness_gate_pass,
        *(value for value in (agent_gate, rag_gate) if value is not None),
    ]
    return UnifiedBoard(
        manifest=manifest,
        agent=agent,
        security=security,
        rag=rag,
        agent_gate_pass=agent_gate,
        security_gate_pass=security.security_gate_pass,
        security_completeness_pass=security.completeness_gate_pass,
        rag_gate_pass=rag_gate,
        overall_gate_pass=all(required),
    )


def write_json_report(
    report: UnifiedBoard,
    path: str | Path,
    *,
    include_records: bool = False,
) -> None:
    """原子写出 UTF-8 JSON；逐场景记录默认不进入基线摘要。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            report.to_dict(include_records=include_records),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
