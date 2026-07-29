"""Offline nightly baseline runner for Agent, Security, and RAG."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from nanoscope.eval.agent_runner_bench import run_agent_v1
from nanoscope.eval.rag_bench import build_benchmark, run_board
from nanoscope.eval.security_cases import collect_security_cases
from nanoscope.eval.trend import MetricStat, metric_stat
from nanoscope.rag import ChunkStore, StubReranker
from nanoscope.rag.sweep import HashingEmbedder


async def collect_offline_round(repo: str | Path) -> dict[str, float]:
    _records, agent = await run_agent_v1()
    with TemporaryDirectory(prefix="nanoscope_nightly_rag_") as temporary:
        store = ChunkStore(Path(temporary) / "rag.db")
        try:
            rag = run_board(
                store,
                build_benchmark(store),
                label="L0 nightly",
                embedder=HashingEmbedder(dim=128),
                reranker=StubReranker(),
            )
        finally:
            store.close()
    _security_records, security = await asyncio.to_thread(collect_security_cases, repo)
    return {
        "agent.strict_tsr": agent.strict_tsr,
        "agent.tokens_per_success": agent.tokens_per_success or 0.0,
        "agent.p95_ms": agent.latency_p95_ms,
        "security.attack_success_rate": security.attack_success_rate,
        "security.violations": float(security.n_violations),
        "rag.recall_at_k": rag.recall_at_k,
        "rag.mrr": rag.mrr,
        "rag.ndcg_at_k": rag.ndcg_at_k,
        "rag.p95_ms": rag.rag_p95_ms,
    }


async def run_offline_nightly(repo: str | Path, *, rounds: int = 3) -> dict[str, MetricStat]:
    if rounds < 2:
        raise ValueError("nightly confidence intervals require at least two rounds")
    results = [await collect_offline_round(repo) for _ in range(rounds)]
    return {
        metric: metric_stat([result[metric] for result in results])
        for metric in results[0]
    }


def serialize_stats(stats: dict[str, MetricStat]) -> dict[str, dict]:
    return {name: asdict(stat) | {"mean": stat.mean, "ci95": stat.ci95} for name, stat in stats.items()}
