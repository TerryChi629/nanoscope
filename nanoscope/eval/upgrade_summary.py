"""Build a machine-recomputable summary for the algorithm upgrade."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from nanoscope.eval.artifacts import atomic_write_json


def _json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _delta(before: float, after: float) -> dict[str, float]:
    return {
        "before": before,
        "after": after,
        "absolute": after - before,
        "relative": (after - before) / before if before else 0.0,
    }


def build_summary(
    *,
    agent_baseline: str | Path,
    agent_routed: str | Path,
    fusion: str | Path,
) -> dict[str, Any]:
    baseline = _json(agent_baseline)
    routed = _json(agent_routed)
    fusion_data = _json(fusion)
    before = baseline["board"]
    after = routed["board"]
    fusion_test = fusion_data["test"]
    return {
        "schema_version": "1.0",
        "artifact_type": "algo_agent_upgrade_summary",
        "agent": {
            "dataset_version": routed["run"]["dataset_version"],
            "routing": routed["run"]["tool_routing"],
            "gate": routed["gate"],
            "metrics": {
                name: _delta(before[name], after[name])
                for name in (
                    "strict_tsr",
                    "tool_selection_precision",
                    "tool_selection_recall",
                    "redundant_tool_rate",
                    "prompt_tokens",
                    "tokens_per_success",
                    "latency_p95_ms",
                )
            },
        },
        "rag_fusion": {
            "dataset_version": fusion_data["dataset_version"],
            "split": fusion_data["split"],
            "metrics": {
                name: _delta(
                    fusion_test["baseline"][name],
                    fusion_test["learned"][name],
                )
                for name in ("recall_at_k", "mrr", "ndcg_at_k")
            },
            "candidate_brier": fusion_test["candidate_brier"],
            "candidate_ece": fusion_test["candidate_ece"],
            "security": fusion_data["security"],
        },
        "limitations": [
            "Agent A/B is one real-model run per configuration, not a confidence interval.",
            "Fusion test contains 12 queries from whole semantic clusters.",
            "HNSW showed no stable latency gain on the current small corpus.",
        ],
        "sources": {
            "agent_baseline": str(agent_baseline),
            "agent_routed": str(agent_routed),
            "fusion": str(fusion),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-baseline",
        default="reports/runs/AGENT_LIVE_UPGRADE_BASELINE.json",
    )
    parser.add_argument(
        "--agent-routed",
        default="reports/runs/AGENT_LIVE_UPGRADE_ROUTED_ABSTAIN.json",
    )
    parser.add_argument(
        "--fusion",
        default="reports/runs/RAG_FUSION_CALIBRATION_V2.json",
    )
    parser.add_argument(
        "--out",
        default="reports/runs/ALGO_AGENT_UPGRADE_SUMMARY.json",
    )
    args = parser.parse_args(argv)
    summary = build_summary(
        agent_baseline=args.agent_baseline,
        agent_routed=args.agent_routed,
        fusion=args.fusion,
    )
    atomic_write_json(args.out, summary)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
