"""Generate the four complete offline evaluation baseline summaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from dataclasses import asdict
from pathlib import Path

from nanoscope.eval.agent_dataset import AGENT_DATASET_VERSION
from nanoscope.eval.agent_live_dataset import AGENT_LIVE_DATASET_VERSION
from nanoscope.eval.agent_open_dataset import AGENT_OPEN_DATASET_VERSION
from nanoscope.eval.agent_runner_bench import run_agent_v1_sync
from nanoscope.eval.artifacts import atomic_write_json
from nanoscope.eval.ingest_dataset import INGEST_DATASET_VERSION
from nanoscope.eval.injection_asr_bench import INJECTION_ASR_VERSION
from nanoscope.eval.rag_bench import build_benchmark, run_board
from nanoscope.eval.rag_frozen import RAG_DATASET_VERSION
from nanoscope.eval.refusal_bench import REFUSAL_MATRIX_VERSION
from nanoscope.eval.security_cases import collect_security_cases
from nanoscope.eval.summary_writers import write_evaluation_artifacts
from nanoscope.eval.unified_bench import (
    aggregate_unified_board,
    build_manifest,
    sha256_file,
)
from nanoscope.rag import ChunkStore, StubReranker
from nanoscope.rag.sweep import HashingEmbedder


def generate_offline_baselines(
    repo: str | Path,
    output_dir: str | Path,
    *,
    rag_live_path: str | Path | None = None,
    agent_live_path: str | Path | None = None,
    agent_open_path: str | Path | None = None,
    injection_asr_path: str | Path | None = None,
) -> dict[str, Path]:
    repo_path = Path(repo).resolve()
    output_path = Path(output_dir)
    agent_source = repo_path / "nanoscope/eval/agent_dataset.py"
    agent_live_source = repo_path / "nanoscope/eval/agent_live_dataset.py"
    agent_open_source = repo_path / "nanoscope/eval/agent_open_dataset.py"
    rag_source = repo_path / "nanoscope/eval/rag_frozen.py"
    security_source = repo_path / "nanoscope/eval/security_cases.py"
    ingest_source = repo_path / "nanoscope/eval/ingest_dataset.py"
    refusal_source = repo_path / "nanoscope/eval/refusal_bench.py"
    injection_asr_source = repo_path / "nanoscope/eval/injection_asr_bench.py"
    catalog_path = output_path / "EVAL_DATASETS_V1.json"
    atomic_write_json(
        catalog_path,
        {
            "schema_version": "1.0",
            "datasets": {
                "agent": {
                    "version": AGENT_DATASET_VERSION,
                    "source": str(agent_source.relative_to(repo_path)),
                    "sha256": sha256_file(agent_source),
                },
                "agent_live": {
                    "version": AGENT_LIVE_DATASET_VERSION,
                    "source": str(agent_live_source.relative_to(repo_path)),
                    "sha256": sha256_file(agent_live_source),
                },
                "agent_open": {
                    "version": AGENT_OPEN_DATASET_VERSION,
                    "source": str(agent_open_source.relative_to(repo_path)),
                    "sha256": sha256_file(agent_open_source),
                },
                "rag": {
                    "version": RAG_DATASET_VERSION,
                    "source": str(rag_source.relative_to(repo_path)),
                    "sha256": sha256_file(rag_source),
                },
                "security": {
                    "version": "security8.v1",
                    "source": str(security_source.relative_to(repo_path)),
                    "sha256": sha256_file(security_source),
                },
                "ingest": {
                    "version": INGEST_DATASET_VERSION,
                    "source": str(ingest_source.relative_to(repo_path)),
                    "sha256": sha256_file(ingest_source),
                },
                "refusal": {
                    "version": REFUSAL_MATRIX_VERSION,
                    "source": str(refusal_source.relative_to(repo_path)),
                    "sha256": sha256_file(refusal_source),
                },
                "injection_asr": {
                    "version": INJECTION_ASR_VERSION,
                    "source": str(injection_asr_source.relative_to(repo_path)),
                    "sha256": sha256_file(injection_asr_source),
                },
            },
        },
    )

    _agent_records, agent = run_agent_v1_sync()
    _security_records, security = collect_security_cases(
        repo_path,
        injection_asr_path=injection_asr_path,
    )
    with tempfile.TemporaryDirectory(prefix="rag_baseline_") as directory:
        store = ChunkStore(Path(directory) / "bench.db")
        try:
            benchmark = build_benchmark(store)
            rag = run_board(
                store,
                benchmark,
                label="L0 offline",
                embedder=HashingEmbedder(dim=128),
                reranker=StubReranker(),
                top_k=5,
            )
        finally:
            store.close()

    manifest = build_manifest(
        repo=repo_path,
        dataset_path=catalog_path,
        dataset_version=(
            f"{AGENT_DATASET_VERSION}+{AGENT_LIVE_DATASET_VERSION}+"
            f"{AGENT_OPEN_DATASET_VERSION}+"
            f"{RAG_DATASET_VERSION}+security8.v1+{INGEST_DATASET_VERSION}+"
            f"{REFUSAL_MATRIX_VERSION}+{INJECTION_ASR_VERSION}"
        ),
        model="scripted+hashing",
        provider="offline",
        random_seed=0,
        credential_env_names=(
            "DEEPSEEK_API_KEY",
            "GLM_API_KEY",
            "SILICONFLOW_API_KEY",
        ),
    )
    unified = aggregate_unified_board(
        manifest,
        security,
        agent=agent,
        rag=rag,
        minimum_agent_tsr=1.0,
    )
    rag_summary: dict = {"offline": asdict(rag), "live": [], "level_statuses": []}
    unified_summary = unified.to_dict()
    agent_live_data: dict | None = None
    agent_open_data: dict | None = None
    if agent_live_path is not None:
        agent_live_data = json.loads(
            Path(agent_live_path).read_text(encoding="utf-8")
        )
        if agent_live_data.get("run", {}).get("dataset_version") != "agent-live24.v2":
            raise ValueError("Agent live artifact must use agent-live24.v2")
        live_gate = bool(agent_live_data.get("gate", {}).get("overall"))
        unified_summary["agent_live"] = agent_live_data
        unified_summary["gates"]["agent_scripted"] = unified_summary["gates"]["agent"]
        unified_summary["gates"]["agent_live"] = live_gate
        unified_summary["gates"]["overall"] = bool(
            unified_summary["gates"]["overall"] and live_gate
        )
    if agent_open_path is not None:
        agent_open_data = json.loads(
            Path(agent_open_path).read_text(encoding="utf-8")
        )
        if (
            agent_open_data.get("artifact_type") != "agent_open"
            or agent_open_data.get("run", {}).get("dataset_version")
            != AGENT_OPEN_DATASET_VERSION
        ):
            raise ValueError("Agent open artifact must use agent-open30.v1")
        open_gate = bool(agent_open_data.get("gate", {}).get("overall"))
        unified_summary["agent_open"] = agent_open_data
        unified_summary["gates"]["agent_open"] = open_gate
        unified_summary["gates"]["overall"] = bool(
            unified_summary["gates"]["overall"] and open_gate
        )
    if rag_live_path is not None:
        for line in Path(rag_live_path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("_type") == "board_summary":
                rag_summary["live"].append(record)
            elif record.get("_type") == "level_status":
                rag_summary["level_statuses"].append(record)
        completed = {
            status["level"]
            for status in rag_summary["level_statuses"]
            if status.get("status") == "completed"
        }
        if completed != {"L1", "L2", "L3"}:
            raise ValueError("Live RAG artifact must contain completed L1, L2, and L3")
        if any(
            status.get("dataset_version") != RAG_DATASET_VERSION
            or status.get("checkpoint_schema") != "p0v4"
            for status in rag_summary["level_statuses"]
        ):
            raise ValueError(
                f"Live RAG artifact must use {RAG_DATASET_VERSION} with p0v4 checkpoints"
            )
        live_gate = all(
            board.get("security_gate_pass")
            and board.get("reliability_gate_pass")
            for board in rag_summary["live"]
        )
        unified_summary["rag_live"] = rag_summary
        unified_summary["gates"]["rag"] = live_gate
        unified_summary["gates"]["overall"] = bool(
            unified_summary["gates"]["overall"] and live_gate
        )
    outputs = write_evaluation_artifacts(
        output_dir=output_path,
        agent=agent,
        security=security,
        rag=rag_summary,
        unified=unified_summary,
        manifest=manifest,
    )
    if agent_live_path is not None and agent_live_data is not None:
        live_path = Path(agent_live_path)
        manifest_data = json.loads(outputs["manifest"].read_text(encoding="utf-8"))
        manifest_data["artifacts"]["agent_live"] = {
            "path": str(live_path.relative_to(output_path))
            if live_path.is_relative_to(output_path)
            else str(live_path),
            "sha256": hashlib.sha256(live_path.read_bytes()).hexdigest(),
            "schema_version": agent_live_data.get("schema_version"),
            "artifact_type": "agent_live",
            "gate": bool(agent_live_data.get("gate", {}).get("overall")),
            "records_included": False,
        }
        atomic_write_json(outputs["manifest"], manifest_data)
        outputs["agent_live"] = live_path
    if agent_open_path is not None and agent_open_data is not None:
        open_path = Path(agent_open_path)
        manifest_data = json.loads(outputs["manifest"].read_text(encoding="utf-8"))
        manifest_data["artifacts"]["agent_open"] = {
            "path": str(open_path.relative_to(output_path))
            if open_path.is_relative_to(output_path)
            else str(open_path),
            "sha256": hashlib.sha256(open_path.read_bytes()).hexdigest(),
            "schema_version": agent_open_data.get("schema_version"),
            "artifact_type": "agent_open",
            "gate": bool(agent_open_data.get("gate", {}).get("overall")),
            "records_included": False,
        }
        atomic_write_json(outputs["manifest"], manifest_data)
        outputs["agent_open"] = open_path
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--out-dir", default="reports/baselines")
    parser.add_argument("--rag-live")
    parser.add_argument("--agent-live")
    parser.add_argument("--agent-open")
    parser.add_argument("--injection-asr")
    args = parser.parse_args()
    outputs = generate_offline_baselines(
        args.repo,
        args.out_dir,
        rag_live_path=args.rag_live,
        agent_live_path=args.agent_live,
        agent_open_path=args.agent_open,
        injection_asr_path=args.injection_asr,
    )
    for name, path in outputs.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
