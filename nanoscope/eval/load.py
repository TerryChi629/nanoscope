"""M9 负载分析器 (PRD §9/§15.4)。

对压测采集到的一批 turn 事件，算调度层的可证伪指标：
- queue_wait 分布（mean/p50/p95/p99/max）：排队时间劣化程度（缺陷①）。
- Jain's Fairness Index，**双变量**：
    - Jain(completed_turns)：偏向短任务用户，会掩盖对长任务的不公。
    - Jain(admitted_service_time)：反映“gate 名额时间”的真实分配，更诚实。
  两个都出并说明差异（PRD §15.4 明确要求）。
- rejected_total：优雅拒绝数（缺陷③，有界背压是否触发）。
- throughput：单位时间完成 turn 数。

纯函数 + 纯数据类，不依赖事件循环，便于单测校验数学正确性。
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class TurnRecord:
    """一个 turn 的生命周期采样（由压测 harness 产出）。

    - principal_id：发起者。
    - enqueued_at / admitted_at / finished_at：单调时钟秒（time.perf_counter）。
    - rejected：是否被有界 admission 优雅拒绝（缺陷③）。被拒时后两个时间无意义。
    """

    principal_id: str
    enqueued_at: float
    admitted_at: float | None
    finished_at: float | None
    rejected: bool = False

    @property
    def queue_wait(self) -> float:
        """排队等待时间 = 入场时刻 - 入队时刻；被拒/未入场记 0。"""
        if self.rejected or self.admitted_at is None:
            return 0.0
        return max(0.0, self.admitted_at - self.enqueued_at)

    @property
    def service_time(self) -> float:
        """占用名额的服务时间 = 完成 - 入场；未完成记 0。"""
        if self.admitted_at is None or self.finished_at is None:
            return 0.0
        return max(0.0, self.finished_at - self.admitted_at)


@dataclass(frozen=True)
class Percentiles:
    mean: float
    p50: float
    p95: float
    p99: float
    maximum: float


@dataclass(frozen=True)
class LoadReport:
    """一次压测运行的聚合指标（改前/改后各一份，供 A/B 对比）。"""

    label: str
    n_total: int
    n_completed: int
    n_rejected: int
    queue_wait: Percentiles
    jain_completed_turns: float
    jain_service_time: float
    throughput: float  # 完成 turn / 墙钟秒
    max_principal_queue_wait: float


def percentile(values: Sequence[float], q: float) -> float:
    """线性插值分位数（q∈[0,1]）。空序列返回 0。"""
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    pos = q * (len(ordered) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(ordered[lo])
    frac = pos - lo
    return float(ordered[lo] * (1 - frac) + ordered[hi] * frac)


def summarize(values: Sequence[float]) -> Percentiles:
    if not values:
        return Percentiles(0.0, 0.0, 0.0, 0.0, 0.0)
    return Percentiles(
        mean=sum(values) / len(values),
        p50=percentile(values, 0.50),
        p95=percentile(values, 0.95),
        p99=percentile(values, 0.99),
        maximum=max(values),
    )


def jain_index(values: Sequence[float]) -> float:
    """Jain's Fairness Index：J = (Σxᵢ)² / (n·Σxᵢ²)，取值 (1/n, 1]。

    全相等 → 1（完全公平）；一人独占 → 1/n（极度不均）。
    全 0（无资源分配）约定返回 1.0（无从判定不公，不污染对比）。
    """
    if not values:
        return 1.0
    total = sum(values)
    sq = sum(v * v for v in values)
    if sq == 0:
        return 1.0
    return (total * total) / (len(values) * sq)


def _per_principal(records: Sequence[TurnRecord], attr: str) -> list[float]:
    """把每个 principal 的某项资源量聚合成列表（Jain 的 xᵢ）。"""
    agg: dict[str, float] = defaultdict(float)
    for r in records:
        if r.rejected:
            continue
        agg[r.principal_id] += getattr(r, attr)
    return list(agg.values())


def analyze(
    records: Sequence[TurnRecord],
    *,
    label: str,
    wall_clock_seconds: float,
) -> LoadReport:
    """把一批 TurnRecord 聚合成 LoadReport。

    - queue_wait 分布只统计成功入场的 turn。
    - Jain(completed_turns)：xᵢ = 该 principal 完成的 turn 数。
    - Jain(service_time)：xᵢ = 该 principal 占用的服务时间总和。
    - throughput = 完成数 / 墙钟秒。
    """
    completed = [r for r in records if not r.rejected and r.finished_at is not None]
    rejected = [r for r in records if r.rejected]
    waits = [r.queue_wait for r in completed]

    completed_counts: dict[str, float] = defaultdict(float)
    for r in completed:
        completed_counts[r.principal_id] += 1.0

    n_completed = len(completed)
    throughput = n_completed / wall_clock_seconds if wall_clock_seconds > 0 else 0.0

    return LoadReport(
        label=label,
        n_total=len(records),
        n_completed=n_completed,
        n_rejected=len(rejected),
        queue_wait=summarize(waits),
        jain_completed_turns=jain_index(list(completed_counts.values())),
        jain_service_time=jain_index(_per_principal(completed, "service_time")),
        throughput=throughput,
        max_principal_queue_wait=max(waits) if waits else 0.0,
    )


def format_ab(baseline: LoadReport, improved: LoadReport) -> str:
    """把改前/改后两份 LoadReport 渲染成对比文本表（供报告/终端）。"""
    rows = [
        ("指标", baseline.label, improved.label),
        ("总请求数", f"{baseline.n_total}", f"{improved.n_total}"),
        ("完成数", f"{baseline.n_completed}", f"{improved.n_completed}"),
        ("拒绝数(有界背压)", f"{baseline.n_rejected}", f"{improved.n_rejected}"),
        ("queue_wait p95(s)", f"{baseline.queue_wait.p95:.4f}", f"{improved.queue_wait.p95:.4f}"),
        ("queue_wait p99(s)", f"{baseline.queue_wait.p99:.4f}", f"{improved.queue_wait.p99:.4f}"),
        ("queue_wait max(s)", f"{baseline.queue_wait.maximum:.4f}",
         f"{improved.queue_wait.maximum:.4f}"),
        ("Jain(completed_turns)", f"{baseline.jain_completed_turns:.4f}",
         f"{improved.jain_completed_turns:.4f}"),
        ("Jain(service_time)", f"{baseline.jain_service_time:.4f}",
         f"{improved.jain_service_time:.4f}"),
        ("throughput(turn/s)", f"{baseline.throughput:.2f}", f"{improved.throughput:.2f}"),
    ]
    w0 = max(len(r[0]) for r in rows)
    w1 = max(len(r[1]) for r in rows)
    w2 = max(len(r[2]) for r in rows)
    lines = []
    for i, (a, b, c) in enumerate(rows):
        lines.append(f"| {a:<{w0}} | {b:>{w1}} | {c:>{w2}} |")
        if i == 0:
            lines.append(f"| {'-' * w0} | {'-' * w1} | {'-' * w2} |")
    return "\n".join(lines)
