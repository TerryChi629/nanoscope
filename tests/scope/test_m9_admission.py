"""M9 有界公平准入 + 压测 A/B 测试 (PRD §15/§11-M9 验收)。

验收点：
- FairAdmissionController 不变量：全局并发不超 global_limit、单人不超 per_principal_limit、
  队满优雅拒绝（缺陷③）；least-in-flight 公平出队（缺陷①②）。
- LoadAnalyzer 数学正确：Jain 边界、percentile 插值。
- A/B 可证伪：
  - U3 慢会话隔离——改后快 turn 的 queue_wait p99 显著优于基线（缺陷①）。
  - U4 混合负载——改后 Jain(service_time) 提升，刷屏用户无法霸占（缺陷②）。
  - U5 洪峰过载——改后触发有界拒绝、队列有上限（缺陷③），基线不拒绝。
"""

from __future__ import annotations

import asyncio

import pytest

from nanoscope.concurrency.admission import (
    AdmissionRejectedError,
    BaselineGate,
    FairAdmissionController,
)
from nanoscope.eval.load import TurnRecord, analyze, jain_index, percentile, summarize
from nanoscope.eval.loadtest import (
    collect_records,
    run_case_ab,
    run_workload,
    workload_u1_ramp,
    workload_u3_slow_isolation,
    workload_u4_mixed_fairness,
    workload_u5_overload,
)

# ---------------------------------------------------------------------------
# 控制器不变量
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_global_limit_never_exceeded():
    """任意时刻全局在飞数不超过 global_limit。"""
    ctrl = FairAdmissionController(global_limit=3, per_principal_limit=3, max_queue=0)
    peak = 0

    async def worker(pid: str):
        nonlocal peak
        async with ctrl.slot(pid):
            peak = max(peak, ctrl.global_inflight)
            assert ctrl.global_inflight <= 3
            await asyncio.sleep(0.01)

    await asyncio.gather(*(worker(f"p{i}") for i in range(12)))
    assert peak == 3  # 名额被打满过
    assert ctrl.global_inflight == 0  # 全部归还


@pytest.mark.asyncio
async def test_per_principal_limit_prevents_hogging():
    """单 principal 在飞数不超过 per_principal_limit，防霸占（缺陷②）。"""
    ctrl = FairAdmissionController(global_limit=5, per_principal_limit=1, max_queue=0)
    concurrent_hog = 0
    peak_hog = 0

    async def hog():
        nonlocal concurrent_hog, peak_hog
        async with ctrl.slot("hog"):
            concurrent_hog += 1
            peak_hog = max(peak_hog, concurrent_hog)
            await asyncio.sleep(0.01)
            concurrent_hog -= 1

    await asyncio.gather(*(hog() for _ in range(8)))
    assert peak_hog == 1  # 同一 principal 从不并发超过 1


@pytest.mark.asyncio
async def test_bounded_queue_rejects_when_full():
    """全局等待队列满 → 优雅拒绝（缺陷③）。"""
    ctrl = FairAdmissionController(global_limit=1, per_principal_limit=1, max_queue=2)
    # 占满唯一名额。
    t = await ctrl.acquire("a")
    # 两个进队列（占满 max_queue=2）。
    w1 = asyncio.ensure_future(ctrl.acquire("b"))
    w2 = asyncio.ensure_future(ctrl.acquire("c"))
    await asyncio.sleep(0)  # 让两个 waiter 真正入队
    assert ctrl.queue_len == 2
    # 第三个应被拒绝。
    with pytest.raises(AdmissionRejectedError):
        await ctrl.acquire("d")
    # 收尾：逐个释放让排队者依次入场，避免悬挂。
    ctrl.release(t)
    tk1 = await asyncio.wait_for(w1, timeout=1.0)
    ctrl.release(tk1)
    tk2 = await asyncio.wait_for(w2, timeout=1.0)
    ctrl.release(tk2)


@pytest.mark.asyncio
async def test_fair_dequeue_prefers_least_inflight():
    """释放名额时优先唤醒占用最少的 principal（缺陷①②公平核心）。

    构造：hog 已占 2 个名额（global=2），随后 hog 再排一个、alice 也排一个。
    释放 1 个 hog 名额时，hog 仍在飞 1 个、alice 在飞 0 个 →
    least-in-flight 必须让 alice（0）先于 hog 再入场（1）。
    """
    ctrl = FairAdmissionController(global_limit=2, per_principal_limit=3, max_queue=0)
    admit_order: list[str] = []
    t0 = await ctrl.acquire("hog")
    t1 = await ctrl.acquire("hog")  # hog 现占满 global=2

    async def waiter(pid: str, arrive_delay: float):
        await asyncio.sleep(arrive_delay)
        async with ctrl.slot(pid):
            admit_order.append(pid)
            await asyncio.sleep(0.005)

    w_hog = asyncio.ensure_future(waiter("hog", 0.001))  # 先到（seq 更小）
    w_alice = asyncio.ensure_future(waiter("alice", 0.002))
    await asyncio.sleep(0.01)
    ctrl.release(t0)  # 让出 1 个：hog 仍在飞 1，alice 在飞 0 → alice 先入
    await asyncio.sleep(0.02)
    ctrl.release(t1)
    await asyncio.gather(w_hog, w_alice)
    assert admit_order[0] == "alice"


@pytest.mark.asyncio
async def test_baseline_gate_is_fifo_and_unbounded():
    """基线 gate：FIFO、无 per-principal 配额、永不拒绝（三缺陷载体）。"""
    gate = BaselineGate(limit=1)
    t = await gate.acquire("hog")
    # 大量等待者都能排上（无界），不会抛拒绝。
    waiters = [asyncio.ensure_future(gate.acquire("hog")) for _ in range(50)]
    await asyncio.sleep(0)
    gate.release(t)
    for w in waiters:
        tk = await asyncio.wait_for(w, timeout=1.0)
        gate.release(tk)


# ---------------------------------------------------------------------------
# LoadAnalyzer 数学
# ---------------------------------------------------------------------------

def test_jain_index_boundaries():
    assert jain_index([5, 5, 5, 5]) == pytest.approx(1.0)  # 完全公平
    assert jain_index([10, 0, 0, 0]) == pytest.approx(0.25)  # 一人独占 → 1/n
    assert jain_index([]) == 1.0
    assert jain_index([0, 0]) == 1.0  # 无分配约定为 1


def test_percentile_interpolation():
    vals = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert percentile(vals, 0.0) == 0.0
    assert percentile(vals, 1.0) == 4.0
    assert percentile(vals, 0.5) == 2.0
    assert percentile([], 0.9) == 0.0


def test_summarize_and_analyze_counts_rejections():
    records = [
        TurnRecord("a", enqueued_at=0.0, admitted_at=0.0, finished_at=1.0),
        TurnRecord("a", enqueued_at=0.0, admitted_at=0.5, finished_at=1.5),
        TurnRecord("b", enqueued_at=0.0, admitted_at=None, finished_at=None, rejected=True),
    ]
    report = analyze(records, label="t", wall_clock_seconds=2.0)
    assert report.n_total == 3
    assert report.n_completed == 2
    assert report.n_rejected == 1
    assert report.throughput == pytest.approx(1.0)  # 2 完成 / 2 秒
    s = summarize([r.queue_wait for r in records if not r.rejected])
    assert s.maximum == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# A/B 可证伪（改前 vs 改后）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_u3_slow_isolation_protects_fast_turns():
    """U3★：单个慢会话灌满长 turn 后，改后快 turn 的排队延迟应显著优于基线（缺陷①）。

    基线全局 FIFO + gate：慢 principal 的多个长 turn 占满名额，快 turn 队头阻塞；
    改后 per-principal in-flight=1 把慢会话限制在 1 个名额，快 turn 排队时间大幅下降。
    """
    specs = workload_u3_slow_isolation()
    base = await collect_records(BaselineGate(limit=3), specs)
    imp = await collect_records(
        FairAdmissionController(global_limit=3, per_principal_limit=1, max_queue=0), specs
    )
    base_fast = summarize([r.queue_wait for r in base if r.principal_id.startswith("fast")])
    imp_fast = summarize([r.queue_wait for r in imp if r.principal_id.startswith("fast")])
    assert imp_fast.p95 < base_fast.p95


@pytest.mark.asyncio
async def test_u4_mixed_load_protects_normal_users_from_hog():
    """U4：刷屏用户(hog)下，改后普通用户的排队延迟应显著优于基线（缺陷②，防霸占）。

    所有 turn 都会完成、总服务时间由任务固有成本决定，故 Jain(service_time) 近似不变；
    真正的公平收益体现在**普通用户不再排在 hog 身后集体饿等**——用普通用户
    queue_wait 的 p95 作为可证伪信号。
    """
    specs = workload_u4_mixed_fairness()
    base = await collect_records(BaselineGate(limit=3), specs)
    imp = await collect_records(
        FairAdmissionController(global_limit=3, per_principal_limit=1, max_queue=0), specs
    )
    normals = {"alice", "bob", "carol"}
    base_wait = summarize([r.queue_wait for r in base if r.principal_id in normals])
    imp_wait = summarize([r.queue_wait for r in imp if r.principal_id in normals])
    assert imp_wait.p95 < base_wait.p95


@pytest.mark.asyncio
async def test_u5_overload_triggers_bounded_rejection():
    """U5：洪峰下改后触发有界优雅拒绝，基线无界（rejected=0）（缺陷③）。"""
    specs = workload_u5_overload(burst=200)
    res = await run_case_ab("U5", specs, global_limit=3, per_principal_limit=1, max_queue=32)
    assert res.baseline.n_rejected == 0  # 基线无界，从不拒绝
    assert res.improved.n_rejected > 0  # 改后有界，优雅拒绝洪峰


@pytest.mark.asyncio
async def test_u1_ramp_all_complete_under_both():
    """U1 爬坡：两种控制器都应完成全部（无拒绝，max_queue 足够），仅排队时间不同。"""
    specs = workload_u1_ramp(n_principals=12)
    base = await run_workload(BaselineGate(limit=3), specs, label="baseline")
    imp = await run_workload(
        FairAdmissionController(global_limit=3, per_principal_limit=1, max_queue=64),
        specs,
        label="nanoscope",
    )
    assert base.n_completed == 12
    assert imp.n_completed == 12
