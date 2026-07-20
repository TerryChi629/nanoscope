"""M14 检索质量硬化测试 (PRD_v4 §M14，修 H5)。

验收点：
- Q1 零分过滤：完全无关 query，VectorRetriever 返回空（而非一批零分）。
- Q2 tie-break 确定性：并列分场景，打乱子检索器顺序 RRF 输出稳定一致。
- Q3 abstention：全库无关 query，融合结果为空。
- Q4 大小写：英文大小写混合命中符合修正后的声明（casefold 不敏感）。
- Q5 无临时文件泄漏：评测跑完无遗留 .db 临时文件。
- Q6 隔离红线不破：DM 门 / 跨 principal 双证仍绿（复用 M7 断言）。
"""

from __future__ import annotations

import glob
import os
import tempfile
from pathlib import Path

import pytest

from nanoscope.eval.retrieval import (
    Bm25Retriever,
    Doc,
    GrepRetriever,
    RrfRetriever,
    VectorRetriever,
    rrf_fuse,
)
from nanoscope.identity import AUDIENCE_DM, AUDIENCE_GROUP, SecurityContext
from nanoscope.memory import SCOPE_USER, Repository


class _KeywordEmbedder:
    """确定性假向量：文本含词则该维置 1，无共同词则余弦为 0。"""

    def __init__(self, vocab: list[str]):
        self._vocab = vocab

    def embed(self, texts):
        return [[1.0 if w in t else 0.0 for w in self._vocab] for t in texts]


def _ctx(tenant: str, principal: str, audience: str) -> SecurityContext:
    return SecurityContext(
        tenant_id=tenant,
        principal_id=principal,
        session_key=f"feishu:{principal}",
        audience_type=audience,
    )


# ---------- Q1 零分过滤 ----------

def test_q1_vector_filters_zero_score():
    """完全无关 query（无共同关键词，余弦全 0）→ VectorRetriever 返回空。"""
    docs = [
        Doc(id="a", content="我家养了一只橘猫", created_at=0),
        Doc(id="b", content="每个月还房贷", created_at=1),
    ]
    emb = _KeywordEmbedder(["橘猫", "房贷", "天气"])
    vec = VectorRetriever(docs, emb)
    # query 只含 vocab 外词 → 全部 doc 余弦 0 → 过滤后为空。
    assert vec.search("完全无关的内容xyz", 5) == []


def test_q1_vector_min_score_threshold():
    """min_score 抬高时，弱相关项被过滤。"""
    docs = [
        Doc(id="strong", content="橘猫 房贷", created_at=0),
        Doc(id="weak", content="橘猫", created_at=1),
    ]
    emb = _KeywordEmbedder(["橘猫", "房贷"])
    vec = VectorRetriever(docs, emb)
    # 默认（min_score=0）两条都召回。
    assert set(vec.search("橘猫 房贷", 5)) == {"strong", "weak"}
    # 抬高阈值只保留满分强相关项。
    assert vec.search("橘猫 房贷", 5, min_score=0.9) == ["strong"]


# ---------- Q2 tie-break 确定性 ----------

def test_q2_rrf_tie_break_deterministic_across_order():
    """并列 RRF 分场景：打乱子检索器顺序，输出稳定一致（不依赖插入顺序）。"""
    # a、b 在两路里对称并列（rank 互换），c 只在一路。
    r1 = ["a", "b", "c"]
    r2 = ["b", "a"]
    fused_ab = rrf_fuse([r1, r2], k=60, top_k=3)
    fused_ba = rrf_fuse([r2, r1], k=60, top_k=3)
    assert fused_ab == fused_ba
    # a、b 同 RRF 分、同 hit_count(2)、同 best_rank(1) → doc_id 升序 a 在 b 前。
    assert fused_ab[:2] == ["a", "b"]


def test_q2_rrf_hit_count_breaks_tie_before_doc_id():
    """RRF 分严格并列但命中路数不同：命中多的排前（hit_count 先于 doc_id）。

    构造：k=60。单路 rank1 → 1/61。两路 rank62 → 2/122 = 1/61（严格相等）。
    则 'a'（一路 rank1，hit_count=1）与 'b'（两路 rank62，hit_count=2）RRF 分相等，
    但 'b' 命中两路，应排前——即使 'b' > 'a' 字典序更大。
    """
    # r1: b 占前 61 个占位后 rank62 命中；为构造 rank62 用填充 id。
    filler = [f"f{i}" for i in range(61)]
    r1 = ["a"]  # a 一路 rank1
    r2 = filler + ["b"]  # b rank62
    r3 = filler + ["b"]  # b 另一路 rank62 → hit_count=2
    fused = rrf_fuse([r1, r2, r3], k=60, top_k=100)
    # a 与 b RRF 分相等（1/61），但 b hit_count=2 > a hit_count=1 → b 在 a 前。
    assert fused.index("b") < fused.index("a")


# ---------- Q3 abstention ----------

def test_q3_rrf_abstains_when_all_empty():
    """所有路有效召回为空 → 融合结果为空。"""
    assert rrf_fuse([[], []], k=60, top_k=5) == []


def test_q3_rrf_retriever_abstains_end_to_end():
    """RrfRetriever 在全库无关 query 上 abstain（各路都空 → 空）。"""
    docs = [Doc(id="a", content="橘猫豆豆", created_at=0)]
    emb = _KeywordEmbedder(["橘猫"])
    vec = VectorRetriever(docs, emb)
    bm25 = Bm25Retriever(docs)
    try:
        rrf = RrfRetriever([bm25, vec])
        # query 无 trigram 命中（bm25 空）且无共同关键词（vector 零分过滤）。
        assert rrf.search("xyz无关内容", 5) == []
    finally:
        bm25.close()


# ---------- Q4 大小写 ----------

def test_q4_grep_case_insensitive():
    """GrepRetriever 声明大小写不敏感：大小写混合的英文 query 命中。"""
    docs = [Doc(id="g", content="Deploy the SERVER tonight", created_at=0)]
    grep = GrepRetriever(docs)
    assert grep.search("deploy the server", 5) == ["g"]


# ---------- Q5 无临时文件泄漏 ----------

def test_q5_bm25_no_temp_db_leak():
    """Bm25Retriever close 后不遗留临时 .db 文件。"""
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "*.db")))
    docs = [Doc(id="a", content="每个月还房贷一万二千元", created_at=0)]
    bm25 = Bm25Retriever(docs)
    path = bm25._path
    bm25.search("房贷", 5)
    bm25.close()
    after = set(glob.glob(os.path.join(tempfile.gettempdir(), "*.db")))
    assert after <= before  # 无新增遗留
    if path not in (":memory:",):
        assert not os.path.exists(path)


# ---------- Q6 隔离红线不破 ----------

@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    r = Repository(tmp_path / "mem.db")
    yield r
    r.close()


def test_q6_dm_gate_still_holds(repo: Repository):
    principal = "orgA:feishu:alice"
    dm = _ctx("orgA", principal, AUDIENCE_DM)
    repo.add(dm, content="我下个月要离职并办理交接", scope=SCOPE_USER, source_type="tool")
    group = _ctx("orgA", principal, AUDIENCE_GROUP)
    got = repo.search_visible(group, query="下个月离职的安排", top_k=5)
    assert all("离职" not in r.content for r in got)


def test_q6_cross_principal_still_isolated(repo: Repository):
    alice = _ctx("orgA", "orgA:feishu:alice", AUDIENCE_DM)
    bob = _ctx("orgA", "orgA:feishu:bob", AUDIENCE_DM)
    repo.add(alice, content="我下个月要离职并办理交接", scope=SCOPE_USER, source_type="tool")
    got = repo.search_visible(bob, query="下个月离职的安排", top_k=5)
    assert got == []
