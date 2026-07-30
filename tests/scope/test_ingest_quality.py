"""摄取质量集 ingest.v1 测试 (RAG 摄取专项)。

离线纪律：默认用确定性 `HashingEmbedder`，不依赖网络/密钥。真实 GLM 端到端
（合成语料）见 `test_ingest_glm_e2e`（需 `GLM_API_KEY`，缺失时跳过）。

核心可证伪命题：
- 安全零容忍——任何 chunking 配置下，proj_a 成员的检索结果都不得命中 proj_b 越权
  chunk（forbidden_exposure==0），否则专项 FAIL。
- 摄取质量有区分度——不同 size/overlap 对 Recall/nDCG 产生可观测差异，帕累托前沿非空。
"""

from __future__ import annotations

import os

import pytest

from nanoscope.eval.ingest_bench import (
    INGEST_GRID,
    pareto_front,
    run_ingest_board,
    run_ingest_config,
)
from nanoscope.eval.ingest_dataset import (
    INGEST_DATASET_VERSION,
    forbidden_documents,
    load_ingest_queries,
    visible_documents,
)
from nanoscope.rag.sweep import HashingEmbedder


def test_dataset_shapes():
    visible = visible_documents()
    forbidden = forbidden_documents()
    queries = load_ingest_queries()
    assert len(visible) == 6
    assert len(forbidden) == 4
    assert len(queries) == len(visible)
    # 每篇可见文档足够长以切出多个 chunk。
    assert all(len(doc.text) > 200 for doc in visible)
    # needle 关键词唯一且出现在对应文本中。
    for doc in visible:
        assert doc.needle in doc.text


def test_security_gate_zero_forbidden_exposure():
    """每种 chunking 配置下越权命中必须为 0（fail-closed 硬门禁）。"""
    embedder = HashingEmbedder(dim=128)
    queries = load_ingest_queries()
    for config in INGEST_GRID:
        result = run_ingest_config(config, queries, embedder=embedder)
        assert result.forbidden_exposure == 0, f"越权泄露于配置 {config}"


def test_quality_metrics_bounded_and_discriminative():
    board = run_ingest_board(embedder=HashingEmbedder(dim=128))
    assert board["dataset_version"] == INGEST_DATASET_VERSION
    assert board["security_gate_pass"] is True
    assert board["gate"]["overall"] is True
    recalls = [c["recall_at_k"] for c in board["configs"]]
    ndcgs = [c["ndcg_at_k"] for c in board["configs"]]
    # 指标落在 [0,1]。
    assert all(0.0 <= r <= 1.0 for r in recalls)
    assert all(0.0 <= n <= 1.0 for n in ndcgs)
    # 帕累托前沿非空且是全体子集。
    front = board["pareto_front"]
    assert 1 <= len(front) <= len(board["configs"])


def test_pareto_front_not_dominated():
    """帕累托前沿的每个点都不被任何其他点严格支配。"""
    queries = load_ingest_queries()
    embedder = HashingEmbedder(dim=128)
    full = [run_ingest_config(cfg, queries, embedder=embedder) for cfg in INGEST_GRID]
    front = pareto_front(full)
    assert front
    for p in front:
        for q in full:
            if q is p:
                continue
            strictly_better = (
                q.recall_at_k >= p.recall_at_k
                and q.ndcg_at_k >= p.ndcg_at_k
                and q.n_visible_chunks <= p.n_visible_chunks
                and (
                    q.recall_at_k > p.recall_at_k
                    or q.ndcg_at_k > p.ndcg_at_k
                    or q.n_visible_chunks < p.n_visible_chunks
                )
            )
            assert not strictly_better


def test_determinism():
    """同一 embedder 两次运行结果完全一致（可复现）。"""
    a = run_ingest_board(embedder=HashingEmbedder(dim=128))
    b = run_ingest_board(embedder=HashingEmbedder(dim=128))
    assert a["configs"] == b["configs"]


@pytest.mark.skipif(
    not os.environ.get("GLM_API_KEY"),
    reason="需要 GLM_API_KEY 环境变量做真实 embedding 端到端",
)
def test_ingest_glm_e2e():
    """真实 GLM embedding 下安全门禁仍零容忍、质量指标仍在 [0,1]。"""
    from nanoscope.eval.embedding import CachingEmbedder, GlmEmbedder

    embedder = CachingEmbedder(GlmEmbedder(dimensions=256))
    board = run_ingest_board(embedder=embedder)
    assert board["security_gate_pass"] is True
    assert all(0.0 <= c["recall_at_k"] <= 1.0 for c in board["configs"])
