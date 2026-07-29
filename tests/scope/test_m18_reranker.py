"""M18 · SiliconFlowReranker 契约与端到端测试 (PRD_v4 §M18.2 第 5 点)。

两档验证：
- 离线契约（默认跑）：注入 fake JSON HTTP client 伪造 SiliconFlow 响应，
  断言真 cross-encoder 只**调序不丢候选**、fail-closed 补齐漏返下标、缺密钥即拒。
- 真实端到端（需 `SILICONFLOW_API_KEY`，缺失时 skip）：真调 bge-reranker-v2-m3，
  证明重排后 nDCG/MRR ≥ 重排前（D5），与 StubReranker 契约完全一致。
"""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest

from nanoscope.eval.metrics import mrr, ndcg_at_k
from nanoscope.rag.rerank import SiliconFlowReranker, StubReranker


class _FakeHttp:
    """Minimal injectable transport that keeps assertions at the JSON contract."""

    def __init__(self, responder: Callable[[dict], dict]):
        self._responder = responder
        self.calls = 0

    def post(self, _url, payload, *, headers):
        self.calls += 1
        assert headers["Authorization"].startswith("Bearer ")
        return self._responder(payload)


def test_missing_api_key_fail_closed(monkeypatch):
    """缺 SILICONFLOW_API_KEY → 构造即抛，绝不静默降级或读入库凭证。"""
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        SiliconFlowReranker()


def test_reorders_by_relevance():
    """服务端按相关性降序返回 index → rerank 返回对应下标顺序。"""

    def respond(body):
        # 断言请求体形状（不外泄密钥断言，只校验契约字段）。
        assert body["query"] and len(body["documents"]) == 3
        # gold 在输入第 2 位（idx=2），服务端把它顶到最前。
        return {
            "results": [
                {"index": 2, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.3},
                {"index": 1, "relevance_score": 0.1},
            ]
        }

    rr = SiliconFlowReranker(api_key="sk-test", http_client=_FakeHttp(respond))
    order = rr.rerank("量子加密", ["噪声A", "噪声B", "量子加密密钥分发方案"])
    assert order == [2, 0, 1]


def test_partial_results_backfilled_no_candidate_dropped():
    """服务端只返回 top_n 子集时，未覆盖下标按原序补齐——绝不丢候选（契约红线）。"""

    http = _FakeHttp(
        lambda _body: {"results": [{"index": 1, "relevance_score": 0.9}]}
    )
    rr = SiliconFlowReranker(api_key="sk-test", http_client=http)
    order = rr.rerank("q", ["a", "b", "c"])
    assert sorted(order) == [0, 1, 2]  # 全排列，一个不少
    assert order[0] == 1  # 命中项在前，其余按原序补齐


def test_empty_inputs_short_circuit():
    """空 query 或空候选：不发请求，返回原序下标。"""

    def boom(_body):
        raise AssertionError("空输入不应发起网络请求")

    http = _FakeHttp(boom)
    rr = SiliconFlowReranker(api_key="sk-test", http_client=http)
    assert rr.rerank("", ["a", "b"]) == [0, 1]
    assert rr.rerank("q", []) == []
    assert http.calls == 0


# ---------- 真实端到端（需密钥，缺失时 skip） ----------

@pytest.mark.skipif(
    not os.environ.get("SILICONFLOW_API_KEY"),
    reason="需要真实 SILICONFLOW_API_KEY 环境变量（不入库）；缺失时跳过联网重排验证",
)
def test_live_rerank_improves_ndcg_and_mrr_like_stub():
    """真 bge-reranker-v2-m3 与 StubReranker 同契约：把 gold 顶前，重排后 ≥ 重排前。"""
    query = "量子加密密钥分发方案"
    cand_texts = ["今天天气不错适合散步", "量子加密密钥分发的详细方案"]  # idx1 = gold
    gold = {"1"}
    before = [str(i) for i in range(len(cand_texts))]
    order = SiliconFlowReranker().rerank(query, cand_texts)
    after = [str(i) for i in order]
    assert sorted(after) == sorted(before)  # 契约：只调序不丢候选
    assert after[0] == "1"  # gold 顶到第 1
    assert ndcg_at_k(after, gold, 2) >= ndcg_at_k(before, gold, 2)
    assert mrr(after, gold) >= mrr(before, gold)


def test_stub_and_siliconflow_share_reranker_contract():
    """两档实现同 Reranker 契约：返回值都是候选下标的全排列。"""
    http = _FakeHttp(
        lambda _body: {"results": [{"index": 0}, {"index": 1}]},
    )
    cands = ["量子加密", "无关文本"]
    stub_order = StubReranker().rerank("量子加密", cands)
    sf_order = SiliconFlowReranker(
        api_key="sk-test",
        http_client=http,
    ).rerank("量子加密", cands)
    assert sorted(stub_order) == [0, 1]
    assert sorted(sf_order) == [0, 1]
