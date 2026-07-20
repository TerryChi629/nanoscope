"""M9 压测 harness：Mock LLM 负载 + U1-U5 用例 + A/B runner (PRD §15.5)。

方法论红线（PRD §15.5 前提）：**Provider 用 Mock LLM（可控固定/可配延迟）隔离网络抖动**——
否则测的是 OpenAI 的延迟，不是调度层。只有 mock 掉 LLM，queue_wait/Jain 才纯粹
反映**调度算法**，改前/改后收益才可归因。

每个用例在 BaselineGate（改前）与 FairAdmissionController（改后）下各跑一遍，
同 workload、同 mock service time、同事件循环，由 LoadAnalyzer 出指标。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass

from nanoscope.concurrency.admission import (
    AdmissionController,
    AdmissionRejectedError,
    BaselineGate,
    FairAdmissionController,
)
from nanoscope.eval.load import LoadReport, TurnRecord, analyze


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
