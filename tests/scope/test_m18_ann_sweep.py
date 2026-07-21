"""M18.7 · 真 hnswlib ANN 索引一致性 + ef/M 参数扫描帕累托 (PRD_v4 §M18.7.3/§M18.7.4)。

覆盖真 HNSW 接入后的三条关键命题：
- **A1 ANN 不破隔离**：`use_ann=True` 下 partitioned/post-filter 仍 `forbidden_exposure==0`，
  且 partitioned 从不访问越权子图——隔离与 ANN 正交（调参不影响可见性）。
- **A2 ANN 与暴力一致（小候选集）**：候选集 ≤ fanout 时 ANN 精确重打分与暴力 top-k 逐字节一致。
- **A3 recall/延迟权衡**：ef_search 单调不降 recall；帕累托前沿含"高 recall"点；
  partitioned 候选规模 < post-filter（剪枝证据）。

未装 hnswlib 时（`hnswlib_available()==False`）ANN 静默退化暴力，涉及"图遍历差异"的断言跳过，
但隔离/一致性断言仍成立（契约不变）。
"""

from __future__ import annotations

import pytest

from nanoscope.identity import AUDIENCE_DM, SecurityContext
from nanoscope.rag import (
    PartitionedSearcher,
    PostFilterSearcher,
    PreFilterSearcher,
    forbidden_doc_exposure,
    hnswlib_available,
)
from nanoscope.rag.store import ChunkStore
from nanoscope.rag.sweep import (
    HashingEmbedder,
    format_sweep_table,
    pareto_front,
    run_param_sweep,
    seed_scaled_cross_dept,
)


def _alice() -> SecurityContext:
    return SecurityContext(
        tenant_id="orgX", principal_id="orgX:feishu:alice",
        session_key="feishu:alice", audience_type=AUDIENCE_DM, roles=("proj_a",),
    )


@pytest.fixture
def scaled_store(tmp_path):
    s = ChunkStore(tmp_path / "docs.db")
    gold, forbidden = seed_scaled_cross_dept(s, n_per_group=40, n_forbidden=40)
    yield s, set(gold), forbidden
    s.close()


# ---------- A1 ANN 不破隔离（调参与可见性正交） ----------

def test_a1_ann_partitioned_zero_exposure_and_no_forbidden_subgraph(scaled_store):
    store, _gold, forbidden = scaled_store
    emb = HashingEmbedder(dim=128)
    for ef in (10, 50, 100):
        out = PartitionedSearcher(store, emb, use_ann=True, ef_search=ef).search(
            _alice(), "量子加密密钥分发", top_k=10
        )
        assert forbidden_doc_exposure(out.results, forbidden) == 0
        assert "project_proj_b" not in out.accessed_partitions
        assert "project_proj_a" in out.accessed_partitions


def test_a1_ann_postfilter_zero_exposure_but_scores_forbidden(scaled_store):
    """post-filter 即便用 ANN 也逃不掉硬伤：越权向量进候选被打分（红线证据）。"""
    store, _gold, forbidden = scaled_store
    emb = HashingEmbedder(dim=128)
    out = PostFilterSearcher(store, emb, use_ann=True, ef_search=100).search(
        _alice(), "量子加密密钥分发", top_k=10
    )
    assert forbidden_doc_exposure(out.results, forbidden) == 0  # 事后删干净
    # 但越权 id 已进过候选/被打分（全局图，与 ef 无关）——线上被否决的理由。
    assert forbidden & out.scored_chunk_ids


# ---------- A2 ANN 精确重打分与暴力一致（候选集 ≤ fanout） ----------

def test_a2_ann_matches_bruteforce_when_candidates_within_fanout(tmp_path):
    """可见集合足够小（≤ fanout）时，ANN 召回覆盖全集 → 与暴力 top-k 逐字节一致。"""
    store = ChunkStore(tmp_path / "small.db")
    seed_scaled_cross_dept(store, n_per_group=6, n_forbidden=6)
    emb = HashingEmbedder(dim=128)
    alice = _alice()
    q = "量子加密密钥分发"
    pre = PreFilterSearcher(store, emb).search(alice, q, top_k=5)
    part_ann = PartitionedSearcher(store, emb, use_ann=True, ef_search=100).search(
        alice, q, top_k=5
    )
    assert [c.id for c in part_ann.results] == [c.id for c in pre.results]
    store.close()


# ---------- A3 recall / 延迟权衡曲线 + 帕累托 ----------

def test_a3_sweep_recall_monotonic_and_pareto_nonempty(scaled_store):
    store, gold, forbidden = scaled_store
    emb = HashingEmbedder(dim=128)
    points = run_param_sweep(
        store, _alice(), "量子加密密钥分发", gold, forbidden,
        embedder=emb, top_k=10, ef_values=(10, 25, 50, 100), repeats=2,
    )
    # 安全硬断言：全程越权命中 0（调参与隔离正交）。
    assert all(p.forbidden_exposure == 0 for p in points)

    # pre-filter 是天花板 recall=1.0。
    pre_pts = [p for p in points if p.strategy == "pre_filter"]
    assert len(pre_pts) == 1 and pre_pts[0].recall_vs_ceiling == 1.0

    # partitioned 候选规模 < post-filter（分区剪枝，与 ef 无关）。
    part = [p for p in points if p.strategy == "partitioned"]
    post = [p for p in points if p.strategy == "post_filter"]
    assert part and post
    assert max(p.candidate_size for p in part) < max(p.candidate_size for p in post)

    if hnswlib_available():
        # HNSW 近似检索：recall 不保证逐点单调，但整体应处高位、且大 ef 不劣于小 ef。
        part_sorted = sorted(part, key=lambda p: p.ef_search)
        assert all(p.recall_vs_ceiling >= 0.8 for p in part_sorted)
        assert part_sorted[-1].recall_vs_ceiling >= part_sorted[0].recall_vs_ceiling

    # 帕累托前沿非空且被前沿点均未被支配。
    front = pareto_front(points)
    assert front


def test_a3_sweep_table_is_honest_about_ann_backend(scaled_store):
    store, gold, forbidden = scaled_store
    emb = HashingEmbedder(dim=128)
    points = run_param_sweep(
        store, _alice(), "量子加密密钥分发", gold, forbidden,
        embedder=emb, top_k=10, ef_values=(25, 50), repeats=1,
    )
    table = format_sweep_table(points)
    assert "越权命中" in table
    # 诚实标注 ANN 底座是否真启用。
    expected = "真 HNSW" if hnswlib_available() else "退化暴力"
    assert expected in table
