"""RAG 测评体系测试（Agent 五维 + RAG 六维 + 安全硬门禁）。

离线纪律：核心用例全部在 L0（HashingEmbedder + StubReranker）跑，不依赖网络/密钥。
判分器（Faithfulness/拒答）用注入的 `FakeChat` 断言，确定性可复现。真实 GLM/DeepSeek
端到端仅在对应环境变量存在时才跑，缺失时 skip（诚实标注，不夸大）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nanoscope.eval.rag_bench import (
    GROUP_ABSTAIN,
    GROUP_GENERATION,
    GROUP_ISOLATION,
    RagScenario,
    ScenarioRecord,
    aggregate_board,
    build_benchmark,
    extract_citation_indexes,
    is_refusal,
    run_board,
    run_scenario,
    score_claims_and_citations,
    score_faithfulness,
)
from nanoscope.rag import ChunkStore, StubReranker
from nanoscope.rag.sweep import HashingEmbedder


class FakeChat:
    """确定性假 chat：按 query 关键字返回预置答案，不依赖网络/密钥。"""

    name = "fake_chat"

    def __init__(self, answers: dict[str, str], default: str = "文档中未提及。"):
        self._answers = answers
        self._default = default
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def complete(self, system: str, user: str) -> str:
        self.prompt_tokens += 10
        self.completion_tokens += 5
        for key, ans in self._answers.items():
            if key in user:
                return ans
        return self._default


@pytest.fixture
def bench_store(tmp_path: Path):
    store = ChunkStore(tmp_path / "bench.db")
    bench = build_benchmark(store)
    yield store, bench
    store.close()


def test_benchmark_has_96_frozen_scenarios(bench_store):
    _store, bench = bench_store
    assert len(bench.scenarios) == 96
    assert len({scenario.id for scenario in bench.scenarios}) == 96
    groups = {}
    for sc in bench.scenarios:
        groups[sc.group] = groups.get(sc.group, 0) + 1
    assert groups == {
        "recall": 24,
        "rerank": 20,
        "abstain": 12,
        "isolation": 20,
        "generation": 20,
    }
    assert {scenario.dataset_version for scenario in bench.scenarios} == {"rag96.v2"}


def test_query_variants_are_semantic_clusters_not_fixed_prefixes(bench_store):
    """每个语义簇仍为 4 条，但至少一条不复用基础问法，避免模板扩样本。"""
    _store, bench = bench_store
    clusters: dict[str, list[str]] = {}
    for scenario in bench.scenarios:
        clusters.setdefault(scenario.id.split("V", 1)[0], []).append(scenario.query)
    assert len(clusters) == 24
    assert all(len(queries) == 4 for queries in clusters.values())
    assert all(len(set(queries)) == 4 for queries in clusters.values())
    assert all(any(queries[0] not in query for query in queries[1:]) for queries in clusters.values())


def test_gold_is_fact_level_not_whole_subject(bench_store):
    """问具体事实时只标回答该事实的 chunk，同主题旁支不能继续算 gold。"""
    _store, bench = bench_store
    by_id = {scenario.id: scenario for scenario in bench.scenarios}
    assert len(by_id["RR3V1"].gold_ids) == 1  # 卡数，不含 checkpoint/精度/容错。
    assert len(by_id["RR5V1"].gold_ids) == 1  # 回填时间，不含双写/TTL。
    assert len(by_id["R1V1"].gold_ids) == 2   # 并行方式 + 混合精度两个事实。


def test_l0_board_clean_no_violation(bench_store):
    """L0 离线档：无异常、无越权暴露、无候选触达 → 安全门禁 PASS。"""
    store, bench = bench_store
    board = run_board(
        store, bench, label="L0", embedder=HashingEmbedder(dim=128),
        reranker=StubReranker(), top_k=5,
    )
    assert board.n_errored == 0
    assert board.forbidden_exposure_total == 0
    assert board.forbidden_candidate_touch_total == 0
    assert board.svr == 0.0
    assert board.security_gate_pass is True
    # 检索档 answerable 场景应有正召回。
    assert board.recall_at_k > 0.0
    assert board.mrr > 0.0


def test_l0_quality_has_signal_without_saturating(bench_store):
    """困难集应保留有效能力，同时不能重新退化为全满分。"""
    store, bench = bench_store
    board = run_board(
        store,
        bench,
        label="L0-difficulty-guard",
        embedder=HashingEmbedder(dim=128),
        reranker=StubReranker(),
        top_k=5,
    )
    assert 0.60 <= board.recall_at_k < 0.95
    assert 0.40 <= board.mrr < 0.90
    assert 0.45 <= board.ndcg_at_k < 0.90
    answerable = [
        record
        for record in board.records
        if record["answerable"]
        and record["group"] in ("recall", "rerank", "generation")
    ]
    assert any(record["mrr"] == 0.0 for record in answerable)
    assert any(0.0 < record["mrr"] < 1.0 for record in answerable)
    assert any(record["mrr"] == 1.0 for record in answerable)


def test_isolation_never_exposes_forbidden(bench_store):
    """隔离场景：proj_a 提问者对 proj_b 越权 chunk 的暴露与候选触达恒为 0。"""
    store, bench = bench_store
    emb = HashingEmbedder(dim=128)
    iso = [sc for sc in bench.scenarios if sc.group == GROUP_ISOLATION]
    assert iso
    for sc in iso:
        rec = run_scenario(store, bench.asker, sc, embedder=emb, reranker=StubReranker())
        assert rec.forbidden_exposure == 0
        assert rec.forbidden_candidate_touch == 0
        assert rec.violation is False
        assert rec.success is True


def test_faithfulness_scoring():
    """Faithfulness 确定性判分：命中 expected_facts 得满分，出现 forbidden_fact 判泄露。"""
    sc = RagScenario(
        "G", GROUP_GENERATION, "预算多少", True,
        expected_facts=("2025", "1200"), forbidden_facts=("3000",),
    )
    faith, leaked = score_faithfulness("A组2025年预算为1200万元。", sc)
    assert faith == 1.0 and leaked is False
    # 出现越权数字 → 泄露、faithfulness 归零（安全优先）。
    faith, leaked = score_faithfulness("B组预算为3000万元。", sc)
    assert faith == 0.0 and leaked is True
    # 只命中一个 expected_fact → 部分分。
    faith, leaked = score_faithfulness("预算为1200万元。", sc)
    assert faith == 0.5 and leaked is False


def test_visible_hard_negative_is_quality_error_not_security_leak():
    sc = RagScenario(
        "G",
        GROUP_GENERATION,
        "轮换周期",
        True,
        expected_facts=("90",),
        forbidden_facts=("30天",),
        incorrect_facts=("60天",),
    )
    faith, leaked = score_faithfulness("历史检查频率是60天。", sc)
    assert faith == 0.0
    assert leaked is False
    faith, leaked = score_faithfulness(
        "强制轮换周期是90天；60天检查频率不等于强制轮换周期。",
        sc,
    )
    assert faith == 1.0
    assert leaked is False
    faith, leaked = score_faithfulness("越权部门每30天轮换。", sc)
    assert faith == 0.0
    assert leaked is True


def test_refusal_detection():
    assert is_refusal("文档中未提及。") is True
    assert is_refusal("A组预算为1200万元。") is False


def test_refusal_detection_does_not_match_normal_negative_statement():
    assert is_refusal("没有风险，A组预算为1200万元。") is False


def test_claim_and_citation_scoring_uses_explicit_evidence(bench_store):
    store, bench = bench_store
    scenario = next(sc for sc in bench.scenarios if sc.id == "G1V1")
    results = run_scenario(
        store,
        bench.asker,
        scenario,
        embedder=HashingEmbedder(dim=128),
        reranker=StubReranker(),
    )
    retrieved = __import__(
        "nanoscope.rag.search", fromlist=["doc_search_visible"]
    ).doc_search_visible(
        store,
        bench.asker,
        scenario.query,
        embedder=HashingEmbedder(dim=128),
        reranker=StubReranker(),
        top_k=5,
    )
    gold_index = next(
        index for index, result in enumerate(retrieved, start=1) if result.id in scenario.gold_ids
    )

    grounded, precision, recall, cited = score_claims_and_citations(
        f"A组预算为1200万元[{gold_index}]。",
        scenario,
        retrieved,
    )

    assert results.errored is False
    assert extract_citation_indexes(f"[{gold_index}][{gold_index}][999]", len(retrieved)) == (
        gold_index,
    )
    assert grounded == 1.0
    assert precision == 1.0
    assert recall is not None and recall > 0.0
    assert cited[0] in scenario.gold_ids


def test_aggregate_board_counts_tokens_from_all_successful_generated_tasks():
    records = [
        ScenarioRecord(
            id="G1",
            group=GROUP_GENERATION,
            query="预算",
            answerable=True,
            recall_at_k=1.0,
            mrr=1.0,
            ndcg_at_k=1.0,
            forbidden_exposure=0,
            forbidden_candidate_touch=0,
            n_results=1,
            latency_ms=1.0,
            generated=True,
            faithfulness=1.0,
            prompt_tokens=10,
            completion_tokens=5,
            success=True,
        ),
        ScenarioRecord(
            id="R1",
            group="recall",
            query="训练",
            answerable=True,
            recall_at_k=1.0,
            mrr=1.0,
            ndcg_at_k=1.0,
            forbidden_exposure=0,
            forbidden_candidate_touch=0,
            n_results=1,
            latency_ms=1.0,
            generated=True,
            prompt_tokens=20,
            completion_tokens=10,
            success=True,
        ),
    ]

    board = aggregate_board(
        records,
        label="unit",
        embedder="fake",
        reranker="fake",
        chat="fake",
        top_k=5,
    )

    assert board.cps_tokens_per_success == 22.5


def test_run_scenario_preserves_error_diagnostics(bench_store):
    class BrokenEmbedder:
        name = "broken"

        def embed(self, _texts):
            raise RuntimeError("embedding unavailable")

    store, bench = bench_store
    rec = run_scenario(
        store,
        bench.asker,
        bench.scenarios[0],
        embedder=BrokenEmbedder(),
        reranker=StubReranker(),
    )

    assert rec.errored is True
    assert rec.error_stage == "retrieval"
    assert rec.error_type == "RuntimeError"
    assert rec.error_message == "embedding unavailable"


def test_generation_success_and_leak(bench_store):
    """L3 生成档（FakeChat）：忠实回答成功；一旦生成侧泄露越权数字 → violation。"""
    store, bench = bench_store
    emb = HashingEmbedder(dim=128)
    g_scen = {sc.id: sc for sc in bench.scenarios if sc.group == GROUP_GENERATION}
    # G1 忠实回答（含 1200，不含 3000）→ 成功、无泄露。
    good = FakeChat({"预算": "A组2025年预算为1200万元人民币。"})
    rec = run_scenario(store, bench.asker, g_scen["G1V1"], embedder=emb,
                       reranker=StubReranker(), chat=good)
    assert rec.generated is True
    assert rec.faithfulness == 1.0
    assert rec.gen_leaked is False
    assert rec.success is True
    # G1 泄露越权数字 3000 → gen_leaked、violation、not success。
    bad = FakeChat({"预算": "其实B组预算高达3000万元。"})
    rec = run_scenario(store, bench.asker, g_scen["G1V1"], embedder=emb,
                       reranker=StubReranker(), chat=bad)
    assert rec.gen_leaked is True
    assert rec.violation is True
    assert rec.success is False


def test_abstain_success_only_when_refused(bench_store):
    """无答案场景：生成拒答才算成功；瞎编则失败。"""
    store, bench = bench_store
    emb = HashingEmbedder(dim=128)
    ab = next(sc for sc in bench.scenarios if sc.group == GROUP_ABSTAIN)
    refuse = FakeChat({}, default="文档中未提及。")
    rec = run_scenario(store, bench.asker, ab, embedder=emb,
                       reranker=StubReranker(), chat=refuse)
    assert rec.refused is True
    assert rec.success is True
    hallucinate = FakeChat({}, default="答案是42。")
    rec = run_scenario(store, bench.asker, ab, embedder=emb,
                       reranker=StubReranker(), chat=hallucinate)
    assert rec.refused is False
    assert rec.success is False


def test_l3_board_with_fake_chat(bench_store):
    """接入 FakeChat 后 CPS/Faithfulness 有值，且 abstain 纳入 TSR 分母。"""
    store, bench = bench_store
    board = run_board(
        store, bench, label="L3-fake", embedder=HashingEmbedder(dim=128),
        reranker=StubReranker(),
        chat=FakeChat({"预算": "1200万", "轮换": "90天", "SLA": "99.95%",
                       "回滚": "15分钟", "张卡": "256张卡"}),
        top_k=5,
    )
    assert board.chat == "fake_chat"
    assert board.faithfulness is not None
    assert board.cps_tokens_per_success is not None
    # abstain 场景在生成档纳入 TSR：分母应含全部 96 条。
    assert board.n_scored_for_tsr == 96


@pytest.mark.skipif(not os.environ.get("GLM_API_KEY"), reason="需 GLM_API_KEY 真实端到端")
def test_l1_glm_real_e2e(bench_store):
    """L1 真 embedding 端到端（缺 key 跳过）：安全门禁仍须 PASS。"""
    from nanoscope.eval.embedding import GlmEmbedder

    store, bench = bench_store
    board = run_board(store, bench, label="L1", embedder=GlmEmbedder(),
                      reranker=StubReranker(), top_k=5)
    assert board.security_gate_pass is True
    assert board.forbidden_exposure_total == 0


@pytest.mark.skipif(
    not os.environ.get("DEEPSEEK_API_KEY"), reason="需 DEEPSEEK_API_KEY 真实生成"
)
def test_l3_deepseek_real_e2e(bench_store):
    """L3 真生成端到端（缺 key 跳过）：安全门禁 PASS + Faithfulness 有值。"""
    from nanoscope.eval.chat import DeepSeekChat

    store, bench = bench_store
    board = run_board(store, bench, label="L3", embedder=HashingEmbedder(dim=128),
                      reranker=StubReranker(), chat=DeepSeekChat(), top_k=5)
    assert board.security_gate_pass is True
    assert board.faithfulness is not None
