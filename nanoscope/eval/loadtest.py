"""M9 压测 harness：Mock LLM 负载 + U1-U5 用例 + A/B runner (PRD §15.5)。

方法论红线（PRD §15.5 前提）：**Provider 用 Mock LLM（可控固定/可配延迟）隔离网络抖动**——
否则测的是 OpenAI 的延迟，不是调度层。只有 mock 掉 LLM，queue_wait/Jain 才纯粹
反映**调度算法**，改前/改后收益才可归因。

每个用例在 BaselineGate（改前）与 FairAdmissionController（改后）下各跑一遍，
同 workload、同 mock service time、同事件循环，由 LoadAnalyzer 出指标。
"""

from __future__ import annotations

import asyncio
import math
import random
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass

from nanoscope.concurrency.admission import (
    AdmissionController,
    AdmissionRejectedError,
    BaselineGate,
    FairAdmissionController,
)
from nanoscope.eval.load import (
    LoadReport,
    TurnRecord,
    analyze,
    effective_goodput,
    peak_concurrency,
    rejection_ratio,
)


@dataclass(frozen=True)
class TurnSpec:
    """一个待发起的 turn：谁发、什么时候到、mock LLM 要跑多久。"""

    principal_id: str
    arrival_at: float  # 相对 t0 的到达时刻（秒）
    service_time: float  # mock LLM + 工具链耗时（秒）


async def _run_one(
    controller: AdmissionController,
    spec: TurnSpec,
    t0: float,
) -> TurnRecord:
    """单个 turn 的生命周期：等到到达 → 申请名额 → mock 服务 → 释放。"""
    delay = spec.arrival_at - (time.perf_counter() - t0)
    if delay > 0:
        await asyncio.sleep(delay)

    enqueued_at = time.perf_counter()
    try:
        ticket = await controller.acquire(spec.principal_id)
    except AdmissionRejectedError:
        return TurnRecord(
            principal_id=spec.principal_id,
            enqueued_at=enqueued_at,
            admitted_at=None,
            finished_at=None,
            rejected=True,
        )
    admitted_at = time.perf_counter()
    try:
        # Mock LLM：确定性 sleep 代表 turn 的 IO 密集耗时，隔离真实网络抖动。
        await asyncio.sleep(spec.service_time)
    finally:
        finished_at = time.perf_counter()
        controller.release(ticket)
    return TurnRecord(
        principal_id=spec.principal_id,
        enqueued_at=enqueued_at,
        admitted_at=admitted_at,
        finished_at=finished_at,
        rejected=False,
    )


async def collect_records(
    controller: AdmissionController,
    specs: Sequence[TurnSpec],
) -> list[TurnRecord]:
    """跑完 workload 并返回原始 TurnRecord（供细粒度 per-principal 分析）。

    为让 FIFO 基线的到达顺序确定，按 arrival_at 升序创建任务，
    相同 arrival 用列表序作稳定 tie-break。
    """
    ordered = sorted(enumerate(specs), key=lambda kv: (kv[1].arrival_at, kv[0]))
    t0 = time.perf_counter()
    tasks = [asyncio.create_task(_run_one(controller, spec, t0)) for _, spec in ordered]
    records = list(await asyncio.gather(*tasks))
    return records


async def run_workload(
    controller: AdmissionController,
    specs: Sequence[TurnSpec],
    *,
    label: str,
) -> LoadReport:
    """在给定 controller 下跑完整 workload，返回聚合 LoadReport。"""
    t0 = time.perf_counter()
    records = await collect_records(controller, specs)
    wall = time.perf_counter() - t0
    return analyze(records, label=label, wall_clock_seconds=wall)


# ---------------------------------------------------------------------------
# 五个压测用例的 workload 构造（PRD §15.5 表）
# ---------------------------------------------------------------------------

def workload_u1_ramp(*, n_principals: int = 12, service_time: float = 0.05) -> list[TurnSpec]:
    """U1 跨 session 并发爬坡：n 个不同 principal 几乎同时各发 1 turn。

    用于观察 gate 饱和点与 queue_wait 随 N 的劣化（找拐点 M）。
    """
    return [
        TurnSpec(principal_id=f"p{i}", arrival_at=i * 0.001, service_time=service_time)
        for i in range(n_principals)
    ]


def workload_u2_same_session_flood(*, n_msgs: int = 30, service_time: float = 0.02) -> list[TurnSpec]:
    """U2 同 session 洪水：单 principal 连发大量 turn（per-principal 配额下的表现）。"""
    return [
        TurnSpec(principal_id="solo", arrival_at=i * 0.001, service_time=service_time)
        for i in range(n_msgs)
    ]


def workload_u3_slow_isolation(
    *,
    n_slow_turns: int = 6,
    slow_time: float = 0.20,
    n_fast: int = 8,
    fast_time: float = 0.02,
) -> list[TurnSpec]:
    """U3 慢会话隔离（★核心，缺陷①）：单个慢 principal 连发多个长 turn 先到，

    随后多个不同的快 principal 各发一个短 turn。
    - 基线 FIFO + 全局 gate：慢 principal 的多个长 turn 占满全部名额，
      快 turn 不管多快都排在长 turn 之后 → 快 turn 被队头阻塞、被拖垮。
    - 改后 per-principal in-flight=1：慢 principal 最多只占 1 个名额，
      剩余名额留给快 principal → 快 turn 不再被单个慢会话长期拖垮。
    """
    specs: list[TurnSpec] = []
    # 单个慢 principal 连发多个长 turn，先到抢占。
    for i in range(n_slow_turns):
        specs.append(TurnSpec(principal_id="slow", arrival_at=i * 0.001, service_time=slow_time))
    # 快会话稍后到，各是不同 principal。
    for j in range(n_fast):
        specs.append(
            TurnSpec(principal_id=f"fast{j}", arrival_at=0.01 + j * 0.001, service_time=fast_time)
        )
    return specs


def workload_u4_mixed_fairness(*, rounds: int = 6) -> list[TurnSpec]:
    """U4 混合负载 + 公平：一个刷屏用户 heavy 连发，多个普通用户各发几条。

    验证 per-principal 资源分配是否公平（缺陷②）：基线下 heavy 可霸占，
    改后应把名额更均匀地分给各 principal。
    """
    specs: list[TurnSpec] = []
    t = 0.0
    for r in range(rounds):
        # 刷屏用户每轮灌 3 条长任务。
        for _ in range(3):
            specs.append(TurnSpec(principal_id="hog", arrival_at=t, service_time=0.06))
            t += 0.001
        # 三个普通用户各 1 条短任务。
        for u in ("alice", "bob", "carol"):
            specs.append(TurnSpec(principal_id=u, arrival_at=t, service_time=0.02))
            t += 0.001
    return specs


def workload_u5_overload(*, burst: int = 200, service_time: float = 0.05) -> list[TurnSpec]:
    """U5 洪峰过载：瞬时灌入远超容量的请求，验证有界 admission 是否稳住（缺陷③）。

    基线无界排队（rejected=0，但队列无上限）；改后应触发优雅拒绝（rejected>0，队列有界）。
    """
    return [
        TurnSpec(principal_id=f"u{i % 20}", arrival_at=0.0, service_time=service_time)
        for i in range(burst)
    ]


@dataclass(frozen=True)
class AbResult:
    case: str
    baseline: LoadReport
    improved: LoadReport


async def run_case_ab(
    case: str,
    specs: Sequence[TurnSpec],
    *,
    global_limit: int = 3,
    per_principal_limit: int = 1,
    max_queue: int = 64,
) -> AbResult:
    """同一 workload 在基线 gate 与公平 controller 下各跑一遍。"""
    baseline = await run_workload(
        BaselineGate(limit=global_limit), specs, label="baseline"
    )
    improved = await run_workload(
        FairAdmissionController(
            global_limit=global_limit,
            per_principal_limit=per_principal_limit,
            max_queue=max_queue,
        ),
        specs,
        label="nanoscope",
    )
    return AbResult(case=case, baseline=baseline, improved=improved)


# 用例注册表：名称 -> workload 工厂（供 report 脚本统一驱动）。
CASES = {
    "U1_ramp": workload_u1_ramp,
    "U2_same_session_flood": workload_u2_same_session_flood,
    "U3_slow_isolation": workload_u3_slow_isolation,
    "U4_mixed_fairness": workload_u4_mixed_fairness,
    "U5_overload": workload_u5_overload,
}


# ---------------------------------------------------------------------------
# M16 压测闭环 v2 (PRD_v4 §M16)：变到达率 / 持续过载 workload + 多轮聚合 CI。
# ---------------------------------------------------------------------------

def workload_u6_poisson(
    *,
    rate: float,
    duration: float,
    service_time: float = 0.01,
    seed: int = 0,
    n_principals: int = 20,
) -> list[TurnSpec]:
    """U6 泊松变到达率：到达间隔 ~ Exp(rate)，在 [0, duration) 上生成到达序列。

    真实 IM 流量是突发的泊松过程而非均匀节拍；用它检验有界准入在随机洪峰下的
    goodput/拒绝率稳定性（M16.3）。给定 seed 可复现，供多轮聚合时逐轮换 seed。
    """
    if rate <= 0 or duration <= 0:
        return []
    rng = random.Random(seed)
    specs: list[TurnSpec] = []
    t = 0.0
    i = 0
    while True:
        # 指数分布到达间隔（泊松过程的等价刻画）。
        gap = rng.expovariate(rate)
        t += gap
        if t >= duration:
            break
        specs.append(
            TurnSpec(
                principal_id=f"u{i % n_principals}",
                arrival_at=t,
                service_time=service_time,
            )
        )
        i += 1
    return specs


def workload_u7_sustained_overload(
    *,
    burst: int = 120,
    service_time: float = 0.01,
) -> list[TurnSpec]:
    """U7 持续过载：瞬时灌入远超容量的请求（全部 t=0 到达），验证背压峰值有界性。

    与 U5 同构但显式面向 M16 的 peak_queue 度量：改前无界排队 → peak_queue 随
    burst 线性增长；改后有界 admission → peak_queue 收敛到 max_queue 上界（L2）。
    """
    return [
        TurnSpec(principal_id=f"u{i % 20}", arrival_at=0.0, service_time=service_time)
        for i in range(burst)
    ]


@dataclass(frozen=True)
class RoundStat:
    """多轮压测某一指标的聚合：均值 ± 95% CI（复用 M15 SeedStat 的 CI 公式）。"""

    values: list[float]

    @property
    def mean(self) -> float:
        return statistics.fmean(self.values) if self.values else 0.0

    @property
    def std(self) -> float:
        return statistics.stdev(self.values) if len(self.values) > 1 else 0.0

    @property
    def ci95(self) -> float:
        """95% 置信区间半宽 = 1.96 · std / sqrt(n)。"""
        n = len(self.values)
        if n <= 1:
            return 0.0
        return 1.96 * self.std / math.sqrt(n)


@dataclass(frozen=True)
class RoundsAbResult:
    """多轮 A/B 聚合结果：每个指标改前/改后各一份 RoundStat。"""

    case: str
    rounds: int
    baseline_peak_queue: RoundStat
    improved_peak_queue: RoundStat
    baseline_goodput: RoundStat
    improved_goodput: RoundStat
    baseline_rejection_ratio: RoundStat
    improved_rejection_ratio: RoundStat


async def _one_round(
    controller: AdmissionController,
    specs: Sequence[TurnSpec],
) -> tuple[float, float, float]:
    """跑一轮 workload，返回 (peak_queue, goodput, rejection_ratio)。"""
    t0 = time.perf_counter()
    records = await collect_records(controller, specs)
    wall = time.perf_counter() - t0
    _peak_inflight, peak_queue = peak_concurrency(records)
    return (
        float(peak_queue),
        effective_goodput(records, wall=wall),
        rejection_ratio(records),
    )


async def _run_case_ab_rounds(
    case: str,
    specs: Sequence[TurnSpec],
    *,
    rounds: int,
    global_limit: int,
    per_principal_limit: int,
    max_queue: int,
) -> RoundsAbResult:
    b_peak: list[float] = []
    i_peak: list[float] = []
    b_good: list[float] = []
    i_good: list[float] = []
    b_rej: list[float] = []
    i_rej: list[float] = []
    for _r in range(rounds):
        # 改前：全局 FIFO Semaphore，无 max_queue（无界排队）。
        bp, bg, br = await _one_round(BaselineGate(limit=global_limit), specs)
        b_peak.append(bp)
        b_good.append(bg)
        b_rej.append(br)
        # 改后：有界 admission + per-principal 配额。
        ip, ig, ir = await _one_round(
            FairAdmissionController(
                global_limit=global_limit,
                per_principal_limit=per_principal_limit,
                max_queue=max_queue,
            ),
            specs,
        )
        i_peak.append(ip)
        i_good.append(ig)
        i_rej.append(ir)
    return RoundsAbResult(
        case=case,
        rounds=rounds,
        baseline_peak_queue=RoundStat(b_peak),
        improved_peak_queue=RoundStat(i_peak),
        baseline_goodput=RoundStat(b_good),
        improved_goodput=RoundStat(i_good),
        baseline_rejection_ratio=RoundStat(b_rej),
        improved_rejection_ratio=RoundStat(i_rej),
    )


def run_case_ab_rounds(
    case: str,
    specs: Sequence[TurnSpec],
    *,
    rounds: int = 5,
    global_limit: int = 3,
    per_principal_limit: int = 1,
    max_queue: int = 64,
) -> RoundsAbResult:
    """同一 workload 在基线 gate（无界）与公平 controller（有界）下各跑 R 轮并聚合。

    每轮各自新建 controller（清零账本），采集三项指标；R 轮汇成 RoundStat 出 CI，
    使"改后峰值有界 / goodput 稳定"这类结论带区间可证伪（L4）。

    同步入口（内部自建事件循环），供纯函数式测试/报告脚本直接调用。
    """
    return asyncio.run(
        _run_case_ab_rounds(
            case,
            specs,
            rounds=rounds,
            global_limit=global_limit,
            per_principal_limit=per_principal_limit,
            max_queue=max_queue,
        )
    )
