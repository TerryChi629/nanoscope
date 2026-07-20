"""M7 检索层测试 (PRD §14 / §11-M7 验收)。

验收点：
- metrics：Recall@K / MRR / nDCG@K 纯函数在已知排序上取值正确，空 gold 约定。
- retrieval：grep（新近序）与 bm25（相关性序）在同一候选集上排序不同；RRF 融合。
- evaluator：多 query 等权平均聚合。
- repository BM25：给定 query 在可见集合内按相关性排序（连续子串重合命中在前）。
- 红线：BM25 只排序，绝不决定可见性——RAG 不破坏隔离（DM 门 / 跨 principal）。
- 规模曲线：grep 随 N 失效、bm25 守住，拐点 M₀ 可复现。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoscope.eval.evaluator import EvalQuery, evaluate
from nanoscope.eval.metrics import mrr, ndcg_at_k, recall_at_k
from nanoscope.eval.retrieval import (
    Bm25Retriever,
    Doc,
    GrepRetriever,
    RrfRetriever,
    VectorRetriever,
    rrf_fuse,
)
from nanoscope.eval.scale_curve import find_crossover, run_curve
from nanoscope.eval.scale_dataset import build_dataset
from nanoscope.identity import AUDIENCE_DM, AUDIENCE_GROUP, SecurityContext
from nanoscope.memory import SCOPE_USER, Repository
from nanoscope.memory.repository import _build_trigram_match


def _ctx(tenant: str, principal: str, audience: str) -> SecurityContext:
    return SecurityContext(
        tenant_id=tenant,
        principal_id=principal,
        session_key=f"feishu:{principal}",
        audience_type=audience,
    )


# ---------- metrics ----------

def test_recall_at_k_counts_hits_in_prefix():
    assert recall_at_k(["a", "b", "c"], {"a", "c"}, 3) == 1.0
    assert recall_at_k(["a", "b", "c"], {"a", "c"}, 1) == 0.5
    assert recall_at_k(["x", "y"], {"a"}, 5) == 0.0


def test_recall_empty_gold_is_one():
    assert recall_at_k([], set(), 5) == 1.0


def test_mrr_first_hit_rank():
    assert mrr(["x", "a", "b"], {"a"}) == pytest.approx(0.5)
    assert mrr(["a"], {"a"}) == 1.0
    assert mrr(["x", "y"], {"a"}) == 0.0


def test_ndcg_perfect_and_partial():
    assert ndcg_at_k(["a", "b"], {"a", "b"}, 2) == pytest.approx(1.0)
    # gold 排在第 2 位，nDCG < 1。
    assert 0.0 < ndcg_at_k(["x", "a"], {"a"}, 2) < 1.0


# ---------- retrieval 对照组 ----------

def test_grep_orders_by_recency_bm25_by_relevance():
    """同一候选集：grep 把最新的排前，bm25 把子串最重合的排前。"""
    docs = [
        Doc(id="gold", content="我下个月离职的安排已经和领导确认", created_at=0),
        Doc(id="noise", content="同事下个月也要出差顺便团建", created_at=100),
    ]
    query = "下个月离职的安排"
    grep = GrepRetriever(docs)
    bm25 = Bm25Retriever(docs)
    try:
        # grep：noise 更新 → 排第一。
        assert grep.search(query, 2)[0] == "noise"
        # bm25：gold 与 query 连续子串重合更多 → 排第一。
        assert bm25.search(query, 2)[0] == "gold"
    finally:
        bm25.close()


def test_bm25_short_query_no_trigram_returns_empty():
    docs = [Doc(id="a", content="离职", created_at=0)]
    bm25 = Bm25Retriever(docs)
    try:
        assert bm25.search("离职", 5) == []  # 2 字无 trigram
    finally:
        bm25.close()


def test_rrf_fuse_rewards_agreement():
    """两路都靠前的 doc 融合后应排第一。"""
    r1 = ["a", "b", "c"]
    r2 = ["b", "a", "d"]
    fused = rrf_fuse([r1, r2], k=60, top_k=4)
    assert set(fused[:2]) == {"a", "b"}


def test_build_trigram_match_dedups_and_guards_short():
    assert _build_trigram_match("ab") is None
    m = _build_trigram_match("离职离职")
    assert m is not None and "OR" in m


# ---------- evaluator ----------

def test_evaluate_averages_over_queries():
    docs = [
        Doc(id="g1", content="每个月还房贷一万二千元", created_at=0),
        Doc(id="g2", content="报名了注册会计师考试", created_at=0),
    ]
    bm25 = Bm25Retriever(docs)
    try:
        queries = [
            EvalQuery(query="每个月还房贷多少", gold_ids=frozenset({"g1"})),
            EvalQuery(query="注册会计师考试报名", gold_ids=frozenset({"g2"})),
        ]
        report = evaluate(bm25, queries, k=5)
        assert report.n_queries == 2
        assert report.recall_at_k == pytest.approx(1.0)
    finally:
        bm25.close()


# ---------- repository BM25 排序 ----------

@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    r = Repository(tmp_path / "mem.db")
    yield r
    r.close()


def test_repository_bm25_ranks_relevant_first(repo: Repository):
    """给 query 时，可见集合内子串重合度高的记忆排在前。"""
    ctx = _ctx("orgA", "orgA:feishu:alice", AUDIENCE_DM)
    repo.add(ctx, content="我下个月要离职并办理交接", scope=SCOPE_USER, source_type="tool")
    repo.add(ctx, content="今天中午吃了牛肉面很好吃", scope=SCOPE_USER, source_type="tool")
    got = repo.search_visible(ctx, query="下个月离职的安排", top_k=5)
    assert got[0].content == "我下个月要离职并办理交接"


def test_repository_bm25_does_not_break_dm_gate(repo: Repository):
    """红线：带 query 走 BM25 时，DM 门仍拦住群聊里的个人记忆。"""
    principal = "orgA:feishu:alice"
    dm = _ctx("orgA", principal, AUDIENCE_DM)
    repo.add(dm, content="我下个月要离职并办理交接", scope=SCOPE_USER, source_type="tool")
    group = _ctx("orgA", principal, AUDIENCE_GROUP)
    got = repo.search_visible(group, query="下个月离职的安排", top_k=5)
    assert all("离职" not in r.content for r in got)


def test_repository_bm25_does_not_leak_across_principal(repo: Repository):
    """红线：BM25 命中他人条目也被主表授权 WHERE 过滤。"""
    alice = _ctx("orgA", "orgA:feishu:alice", AUDIENCE_DM)
    bob = _ctx("orgA", "orgA:feishu:bob", AUDIENCE_DM)
    repo.add(alice, content="我下个月要离职并办理交接", scope=SCOPE_USER, source_type="tool")
    got = repo.search_visible(bob, query="下个月离职的安排", top_k=5)
    assert got == []


def test_repository_no_query_falls_back_to_time_order(repo: Repository):
    """无 query 时回退时间序（MVP-0 行为），返回全部可见项。"""
    ctx = _ctx("orgA", "orgA:feishu:alice", AUDIENCE_DM)
    repo.add(ctx, content="第一条", scope=SCOPE_USER, source_type="tool")
    repo.add(ctx, content="第二条", scope=SCOPE_USER, source_type="tool")
    got = repo.search_visible(ctx, top_k=5)
    assert {r.content for r in got} == {"第一条", "第二条"}


# ---------- 规模曲线拐点 ----------

def test_scale_curve_grep_degrades_bm25_holds():
    """grep 随 N 阶梯下滑并跌破 SLA；bm25 全程守住高位。"""
    data = build_dataset(scales=[20, 200, 5000])
    points = run_curve(k=5, frozen=data)
    grep_recalls = [p.grep.recall_at_k for p in points]
    bm25_recalls = [p.bm25.recall_at_k for p in points]
    # grep 单调不增且末端明显低于首端。
    assert grep_recalls[-1] < grep_recalls[0]
    # bm25 全程稳定高位。
    assert all(r >= 0.8 for r in bm25_recalls)
    # 存在拐点。
    assert find_crossover(points, sla=0.8) is not None


# ---------- 向量召回 + RRF（离线 FakeEmbedder，不依赖网络/密钥）----------

class _KeywordEmbedder:
    """确定性假向量：每个维度对应一个关键词，文本含该词则该维置 1。

    模拟"语义相近"——即便无共同 trigram，含同一关键词也向量近邻。用于离线证明
    向量能补 BM25 的词序/语义盲区，且完全可复现。
    """

    def __init__(self, vocab: list[str]):
        self._vocab = vocab

    def embed(self, texts):
        return [[1.0 if w in t else 0.0 for w in self._vocab] for t in texts]


def test_vector_recovers_bm25_word_order_blind_spot():
    """BM25 因词序漏召回，向量按关键词近邻召回。"""
    docs = [
        Doc(id="gold", content="我家养了一只叫豆豆的橘猫很粘人", created_at=0),
        Doc(id="noise", content="今天天气不错出门散步", created_at=1),
    ]
    query = "我家那只橘猫豆豆"  # 与 gold 无共同 3-gram（词序颠倒）
    bm25 = Bm25Retriever(docs)
    try:
        assert bm25.search(query, 5) == []  # BM25 盲区
    finally:
        bm25.close()
    emb = _KeywordEmbedder(["橘猫", "豆豆", "天气"])
    vec = VectorRetriever(docs, emb)
    assert vec.search(query, 5)[0] == "gold"  # 向量召回


def test_rrf_retriever_fuses_bm25_and_vector():
    """RRF 融合：BM25 命中的与向量命中的都进 top-K。"""
    docs = [
        Doc(id="d_bm25", content="每个月还房贷一万二千元", created_at=0),
        Doc(id="d_vec", content="我家养了一只叫豆豆的橘猫", created_at=1),
    ]
    bm25 = Bm25Retriever(docs)
    try:
        emb = _KeywordEmbedder(["房贷", "橘猫"])
        vec = VectorRetriever(docs, emb)
        rrf = RrfRetriever([bm25, vec])
        # 房贷查询 BM25 命中 d_bm25；橘猫查询向量命中 d_vec。
        assert rrf.search("每个月还房贷多少", 5)[0] == "d_bm25"
        assert rrf.search("我家那只橘猫豆豆", 5)[0] == "d_vec"
    finally:
        bm25.close()


def test_scale_curve_with_embedder_adds_vector_and_rrf():
    """传 embedder 时曲线含 vector/rrf，且向量补齐 BM25 漏召回。"""
    data = build_dataset(scales=[50])
    emb = _KeywordEmbedder(["橘猫", "豆豆", "离职", "房贷", "过敏", "会计", "搬家", "蛋糕", "车牌"])
    points = run_curve(k=5, frozen=data, embedder=emb)
    p = points[0]
    assert p.vector is not None and p.rrf is not None
    # 向量命中率 ≥ BM25（补齐词序盲区那条）。
    assert p.vector.recall_at_k >= p.bm25.recall_at_k
