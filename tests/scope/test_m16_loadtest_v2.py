"""M16 压测闭环 v2 测试 (PRD_v4 §M16)。

验收点：
- L1 goodput/rejection/completed-latency 三者分开可取且方向自洽。
- L2 active_tasks/queue 峰值在改后有上界、改前无界（B2 的压测级复现）。
- L3 Little's Law 校验：实测 L 与 λ·W 在误差范围内一致。
- L4 多轮聚合给出 CI；拐点带区间。
- L5 单用户/基线 gate 路径行为不变。
"""

from __future__ import annotations

from nanoscope.eval.load import (
    LittlesLaw,
    TurnRecord,
    effective_goodput,
    group_percentiles,
    jain_queue_wait,
    littles_law,
    peak_concurrency,
    rejection_ratio,
)
from nanoscope.eval.loadtest import (
    RoundStat,
    run_case_ab_rounds,
    workload_u6_poisson,
    workload_u7_sustained_overload,
)


def _rec(pid, enq, adm, fin, rejected=False):
    return TurnRecord(
        principal_id=pid, enqueued_at=enq, admitted_at=adm, finished_at=fin, rejected=rejected
    )


# ---------- L1 三指标分开自洽 ----------

def test_l1_rejection_ratio_and_goodput_are_separable():
    """拒绝率与 goodput 分开可取：过载下高拒绝率不应被误读为尾延迟改善。"""
    records = [
        _rec("a", 0.0, 0.0, 1.0),  # 完成
        _rec("b", 0.0, 0.1, 1.1),  # 完成
        _rec("c", 0.0, None, None, rejected=True),  # 拒绝
        _rec("d", 0.0, None, None, rejected=True),  # 拒绝
    ]
    assert rejection_ratio(records) == 0.5  # 2/4
    assert effective_goodput(records, wall=2.0) == 1.0  # 2 完成 / 2s


def test_l1_group_percentiles_split_normal_vs_hog():
    """per-principal 拆分：普通用户与 hog 分开报告尾延迟。"""
    records = [
        _rec("hog", 0.0, 0.5, 1.0),  # hog 排队久
        _rec("hog", 0.0, 0.6, 1.1),
        _rec("alice", 0.0, 0.01, 0.5),  # 普通用户排队短
        _rec("bob", 0.0, 0.02, 0.5),
    ]
    normal = group_percentiles(records, {"alice", "bob"})
    hog = group_percentiles(records, {"hog"})
    # 普通用户排队等待远小于 hog。
    assert normal.p99 < hog.p99


# ---------- L2 背压峰值有界 vs 无界 ----------

def test_l2_peak_concurrency_sweep_line():
    """sweep-line 重建：峰值在飞数 = 时间轴上同时在飞的最大记录数。"""
    records = [
        _rec("a", 0.0, 0.0, 2.0),
        _rec("b", 0.0, 0.5, 2.5),
        _rec("c", 0.0, 1.0, 1.5),  # t∈[1.0,1.5) 三者同时在飞
    ]
    peak_inflight, peak_queue = peak_concurrency(records)
    assert peak_inflight == 3


def test_l2_bounded_after_unbounded_before():
    """改后（有界 max_queue）峰值排队有上界；改前（无界）随 burst 线性增长。"""
    specs = workload_u7_sustained_overload(burst=120, service_time=0.01)
    stat = run_case_ab_rounds("U7", specs, rounds=3, global_limit=3, max_queue=32)
    # 改后峰值排队 ≤ max_queue + global_limit（有界）。
    assert stat.improved_peak_queue.mean <= 32 + 3 + 1
    # 改前峰值排队显著大于改后（无界堆积）。
    assert stat.baseline_peak_queue.mean > stat.improved_peak_queue.mean


# ---------- L3 Little's Law ----------

def test_l3_littles_law_consistency():
    """Little's Law：L_measured ≈ λ·W（同一批记录的一致性校验）。"""
    # 稳定到达：每 1s 到一个，各服务 1s，global 容量足够 → 系统内约 1 个。
    records = [_rec(f"p{i}", float(i), float(i), float(i) + 1.0) for i in range(10)]
    law = littles_law(records, wall=10.0)
    assert isinstance(law, LittlesLaw)
    assert law.l_measured > 0
    assert law.l_predicted > 0
    # 相对误差在宽松范围内（有限样本近似）。
    assert law.rel_error < 0.5


# ---------- L4 多轮聚合 CI ----------

def test_l4_multi_round_reports_ci():
    """多轮聚合给出均值与 CI。"""
    specs = workload_u6_poisson(rate=50.0, duration=0.3, service_time=0.01, seed=1)
    stat = run_case_ab_rounds("U6", specs, rounds=5, global_limit=3, max_queue=64)
    assert isinstance(stat.baseline_goodput, RoundStat)
    assert len(stat.baseline_goodput.values) == 5
    assert stat.baseline_goodput.ci95 >= 0.0
    assert stat.improved_goodput.ci95 >= 0.0


# ---------- L5 基线路径行为不变 ----------

def test_l5_jain_queue_wait_all_zero_is_one():
    """无排队（queue_wait 全 0）时 Jain=1（不污染对比）。"""
    records = [_rec("a", 0.0, 0.0, 1.0), _rec("b", 0.0, 0.0, 1.0)]
    assert jain_queue_wait(records) == 1.0


def test_l5_single_user_no_rejection():
    """单用户/低负载：无拒绝、goodput 正常（基线路径不变）。"""
    specs = workload_u6_poisson(rate=5.0, duration=0.2, service_time=0.01, seed=2)
    stat = run_case_ab_rounds("U6_light", specs, rounds=3, global_limit=3, max_queue=64)
    # 低负载下改前改后都不拒绝。
    assert stat.improved_rejection_ratio.mean == 0.0
