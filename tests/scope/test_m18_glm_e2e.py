"""M18 · 真实 GLM embedding + 合成跨部门语料端到端隔离验证 (PRD_v4 §M18)。

用**真实 GLM `embedding-3` 向量**（而非离线假向量）跑权限感知文档 RAG，证明隔离墙在
真实语义向量下依然成立：A 部门成员用"语义命中 B 部门机密"的 query 检索，
`forbidden_doc_exposure==0`；关掉授权（全库向量近邻）时越权文档被泄露（>0，可证伪翻转）。

纪律：
- 凭证只走环境变量 `GLM_API_KEY`（绝不入库）；缺失时整模块 skip（CI 默认不联网）。
- 语料是**程序合成**的跨部门机密（非真实机密），仓库零机密数据。
- 三策略（pre/post/partitioned）+ 真 hnswlib（若可用）全部断言 0 越权。
"""

from __future__ import annotations

import os

import pytest

from nanoscope.identity import AUDIENCE_DM, SecurityContext
from nanoscope.rag import (
    PartitionedSearcher,
    PostFilterSearcher,
    PreFilterSearcher,
    doc_search_visible,
    forbidden_doc_exposure,
)
from nanoscope.rag.store import ChunkStore
from nanoscope.rag.sweep import seed_scaled_cross_dept

pytestmark = pytest.mark.skipif(
    not os.environ.get("GLM_API_KEY"),
    reason="需要真实 GLM_API_KEY 环境变量（不入库）；缺失时跳过联网端到端验证",
)


def _glm_embedder():
    from nanoscope.eval.embedding import GlmEmbedder

    # 降维到 256，省流量/加速；隔离结论与维度无关。
    return GlmEmbedder(dimensions=256)


def _alice() -> SecurityContext:
    return SecurityContext(
        tenant_id="orgX", principal_id="orgX:feishu:alice",
        session_key="feishu:alice", audience_type=AUDIENCE_DM, roles=("proj_a",),
    )


@pytest.fixture(scope="module")
def glm_store(tmp_path_factory):
    d = tmp_path_factory.mktemp("glm_rag")
    s = ChunkStore(d / "docs.db")
    gold, forbidden = seed_scaled_cross_dept(s, n_per_group=10, n_forbidden=10)
    yield s, set(gold), forbidden
    s.close()


def test_glm_e2e_zero_forbidden_exposure_all_strategies(glm_store):
    store, _gold, forbidden = glm_store
    emb = _glm_embedder()
    q = "B 部门量子通信纠缠态实验的机密进展"
    alice = _alice()
    for searcher in (
        PreFilterSearcher(store, emb),
        PostFilterSearcher(store, emb, use_ann=True),
        PartitionedSearcher(store, emb, use_ann=True),
    ):
        out = searcher.search(alice, q, top_k=10)
        assert forbidden_doc_exposure(out.results, forbidden) == 0


def test_glm_e2e_search_visible_zero_exposure(glm_store):
    store, _gold, forbidden = glm_store
    emb = _glm_embedder()
    results = doc_search_visible(
        store, _alice(), "量子加密密钥分发的机密方案", embedder=emb, top_k=10
    )
    assert forbidden_doc_exposure(results, forbidden) == 0


def test_glm_e2e_falsifiable_unfiltered_leaks(glm_store):
    """可证伪对照：绕过授权 WHERE 用真实 GLM 向量全库近邻 → 越权文档被泄露（>0）。"""
    from nanoscope.eval.retrieval import Doc, VectorRetriever

    store, _gold, forbidden = glm_store
    emb = _glm_embedder()
    all_chunks = store.all_chunks_unfiltered()
    docs = [Doc(id=c.id, content=c.content, created_at=c.created_at) for c in all_chunks]
    naive_ids = set(VectorRetriever(docs, emb).search("量子通信纠缠态实验记录", 10))
    naive_results = [c for c in all_chunks if c.id in naive_ids]
    assert forbidden_doc_exposure(naive_results, forbidden) > 0
