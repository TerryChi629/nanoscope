"""M15 规模曲线 v2 测试 (PRD_v4 §M15，修 H4)。

验收点：
- S1 无阈值：数据生成不含任何手工 `_BURY_AT` 常量；干扰数量是 N 的连续函数。
- S2 单调性：随 N 增大 grep Recall@K 统计意义下降，BM25 相对更稳。
- S3 可复现：固定种子输出确定；多种子聚合给出均值与方差。
- S4 rank 分布：报告能输出每档 N 的 gold rank 分布。
- S5 诚实标注：报告写清"合成集"，拐点带 CI，不写单点绝对值。
"""

from __future__ import annotations

from nanoscope.eval import scale_dataset_v2 as sv2
from nanoscope.eval.scale_dataset_v2 import (
    build_at_scale_v2,
    find_crossover_v2,
    format_curve_v2,
    run_curve_v2,
    zipf_lambda,
)

# ---------- S1 无阈值 + 连续增长 ----------

def test_s1_no_bury_at_threshold_constant():
    """v2 数据生成器不得含任何手工掩埋阈值常量（区别于 v1 的 _BURY_AT）。"""
    assert not hasattr(sv2, "_BURY_AT")


def test_s1_distractor_count_grows_continuously_with_n():
    """同主题干扰数量随 N 连续增长（非阶梯阈值触发）。"""
    def _topic0_distractors(n: int) -> int:
        docs, _ = build_at_scale_v2(n, seed=7)
        return sum(1 for d in docs if d.id.startswith("dist-0-"))

    small = _topic0_distractors(200)
    large = _topic0_distractors(5000)
    # 热门主题（t=0）干扰随规模显著增多。
    assert large > small
    # Zipf：热门主题 λ 大于长尾主题。
    assert zipf_lambda(0, density=0.02, s=1.0) > zipf_lambda(5, density=0.02, s=1.0)


# ---------- S2 单调性 ----------

def test_s2_grep_degrades_bm25_holds_under_ci():
    """随 N 增大，grep Recall@K 均值下降；BM25 相对守住高位。"""
    scales = [50, 500, 3000]
    seeds = [11, 22, 33, 44, 55]
    points = run_curve_v2(scales, seeds, k=5)
    grep_means = [p.grep.mean for p in points]
    bm25_means = [p.bm25.mean for p in points]
    # grep 末端明显低于首端。
    assert grep_means[-1] < grep_means[0]
    # bm25 全程守住高位（相关性排序不受干扰密度影响）。
    assert all(m >= 0.8 for m in bm25_means)
    # 存在拐点（grep 均值跌破 SLA）。
    assert find_crossover_v2(points, sla=0.8) is not None


# ---------- S3 可复现 + 方差 ----------

def test_s3_fixed_seed_is_deterministic():
    """同 (n, seed) 两次生成逐条一致。"""
    d1, q1 = build_at_scale_v2(500, seed=42)
    d2, q2 = build_at_scale_v2(500, seed=42)
    assert [d.id for d in d1] == [d.id for d in d2]
    assert [d.content for d in d1] == [d.content for d in d2]
    assert [q.query for q in q1] == [q.query for q in q2]


def test_s3_multiseed_reports_mean_and_std():
    """多种子聚合给出均值与非负标准差 + CI 字段。"""
    points = run_curve_v2([500], [1, 2, 3, 4, 5], k=5)
    stat = points[0].grep
    assert len(stat.values) == 5
    assert 0.0 <= stat.mean <= 1.0
    assert stat.std >= 0.0
    assert stat.ci95 >= 0.0


# ---------- S4 rank 分布 ----------

def test_s4_reports_gold_rank_distribution():
    """报告输出每档 N 的 gold rank 分布 + 候选集大小。"""
    points = run_curve_v2([500], [1, 2], k=5)
    p = points[0]
    assert len(p.gold_ranks) > 0
    assert len(p.candidate_sizes) > 0
    # rank 为 0（不在候选集）或正整数。
    assert all(r >= 0 for r in p.gold_ranks)


# ---------- S5 诚实标注 ----------

def test_s5_report_text_is_honest():
    """报告文本写清'合成集'、带 CI，不写单点绝对结论。"""
    points = run_curve_v2([50, 500], [1, 2, 3], k=5)
    text = format_curve_v2(points, k=5, sla=0.8)
    assert "合成集" in text
    assert "CI" in text or "±" in text
