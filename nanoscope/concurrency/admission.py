"""M9 有界公平准入控制器 (PRD §15.2/§15.3)。

改前基线（`BaselineGate`）= 全局单一 FIFO `Semaphore(gate)`，复刻 nanobot 的三个缺陷：
- ① 全局 FIFO 队头阻塞：慢 turn 占满名额后，快 turn 不管多快都排在后面。
- ② 无 per-principal 配额：一个刷屏用户可占满全部名额，其他人集体饿等。
- ③ 无全局有界 admission 背压：等待者无上限，洪峰即雪崩。

改后（`FairAdmissionController`，MVP-0 + P1-B.1/B.2）：
- 全局有界 admission 队列：等待者超过 `max_queue` → `AdmissionRejected`（优雅拒绝，缺陷③）。
- per-principal in-flight 上限：单人最多同时 `per_principal_limit` 个在飞，防霸占（缺陷②）。
- least-in-flight 公平出队：释放名额时优先唤醒“当前占用最少”的 principal（缺陷①②），
  同 principal 内按到达序 FIFO，从而慢会话不再长期拖垮快会话。

红线：本模块只做**调度**（谁先进、进不进得来），绝不触碰记忆可见性——
可见性由 M2 的 `Repository.search_visible` 授权 WHERE 强制，二者正交。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, runtime_checkable

from loguru import logger


class AdmissionRejectedError(Exception):
    """全局有界 admission 队列已满，优雅拒绝（缺陷③，可回 retry-after）。"""

    def __init__(self, message: str = "", *, retry_after: float | None = None) -> None:
        super().__init__(message)
        # 供渠道退避：秒级建议重试间隔（M13 改动点③/⑤）。None 表示未指定。
        self.retry_after = retry_after


@dataclass(frozen=True)
class Ticket:
    """一次准入许可。principal_id 用于释放时归还配额，seq 便于审计/调试。"""

    principal_id: str
    seq: int


@runtime_checkable
class AdmissionController(Protocol):
    """准入控制器统一接口，供 A/B 压测在改前/改后间切换。"""

    async def acquire(self, principal_id: str) -> Ticket:
        """申请一个名额；无名额时排队，队满则抛 AdmissionRejected。"""
        ...

    def release(self, ticket: Ticket) -> None:
        """归还名额，触发下一个等待者入场。"""
        ...


@dataclass
class _Waiter:
    principal_id: str
    seq: int
    future: asyncio.Future


class BaselineGate:
    """改前基线：全局单一 FIFO Semaphore，无 per-principal 配额、无有界背压。

    等待者 FIFO 唤醒、无上限，principal_id 被忽略——正是 §15.2 三缺陷的载体。
    """

    name = "baseline"

    def __init__(self, *, limit: int = 3) -> None:
        if limit <= 0:
            raise ValueError("limit 必须为正")
        self._sem = asyncio.Semaphore(limit)
        self._seq = 0

    async def acquire(self, principal_id: str) -> Ticket:
        await self._sem.acquire()  # FIFO；不区分 principal；永不拒绝
        self._seq += 1
        return Ticket(principal_id=principal_id, seq=self._seq)

    def release(self, ticket: Ticket) -> None:
        self._sem.release()

    @asynccontextmanager
    async def slot(self, principal_id: str) -> AsyncIterator[Ticket]:
        ticket = await self.acquire(principal_id)
        try:
            yield ticket
        finally:
            self.release(ticket)


@dataclass
class FairAdmissionController:
    """改后：全局有界 admission + per-principal in-flight 上限 + least-in-flight 公平出队。

    参数：
    - global_limit：全局同时在飞上限（对应基线 gate 的并发上限旋钮）。
    - per_principal_limit：单 principal 同时在飞上限，防单人霸占（缺陷②）。
    - max_queue：全局等待队列上限，超过即优雅拒绝（缺陷③）；<=0 表示无界（仅测试用）。
    """

    global_limit: int = 3
    per_principal_limit: int = 1
    max_queue: int = 64
    name: str = "nanoscope"

    _global_inflight: int = field(default=0, init=False)
    _principal_inflight: dict[str, int] = field(default_factory=dict, init=False)
    _waiters: list[_Waiter] = field(default_factory=list, init=False)
    _seq: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.global_limit <= 0:
            raise ValueError("global_limit 必须为正")
        if self.per_principal_limit <= 0:
            raise ValueError("per_principal_limit 必须为正")

    # -- 内部：判定与入场 ---------------------------------------------------

    def _can_admit(self, principal_id: str) -> bool:
        return (
            self._global_inflight < self.global_limit
            and self._principal_inflight.get(principal_id, 0) < self.per_principal_limit
        )

    def _admit(self, principal_id: str) -> Ticket:
        self._global_inflight += 1
        self._principal_inflight[principal_id] = (
            self._principal_inflight.get(principal_id, 0) + 1
        )
        self._seq += 1
        return Ticket(principal_id=principal_id, seq=self._seq)

    # -- 公开接口 ----------------------------------------------------------

    async def acquire(self, principal_id: str) -> Ticket:
        # 无人排队且有容量 → 立即入场（不破坏公平序）。
        if not self._waiters and self._can_admit(principal_id):
            return self._admit(principal_id)

        # 需要排队：先看全局有界 admission 背压（缺陷③的解法）。
        if self.max_queue > 0 and len(self._waiters) >= self.max_queue:
            raise AdmissionRejectedError(
                f"admission 队列已满（{len(self._waiters)}/{self.max_queue}），请稍后再试",
                retry_after=self.suggested_retry_after(),
            )

        self._seq += 1
        loop = asyncio.get_running_loop()
        waiter = _Waiter(principal_id=principal_id, seq=self._seq, future=loop.create_future())
        self._waiters.append(waiter)
        try:
            return await waiter.future
        except asyncio.CancelledError:
            # 等待被取消：从队列摘除，并让出的容量泵给后续等待者。
            if waiter in self._waiters:
                self._waiters.remove(waiter)
            self._pump()
            raise

    def try_acquire(self, principal_id: str) -> Ticket | None:
        """非阻塞预检（M13 改动点②）：有容量立即入场返回 Ticket，否则返回 None。

        不排队、不抛异常——入口处 run-loop 用它决定「建 task 还是直接优雅拒绝」，
        使 task/队列峰值收敛到 global_limit（不再随注入量线性增长，B2）。
        """
        if not self._waiters and self._can_admit(principal_id):
            return self._admit(principal_id)
        return None

    def would_reject(self, principal_id: str) -> bool:
        """入口非阻塞谓词（M13 改动点②）：若此刻 acquire 会因队满而拒绝则返回 True。

        语义与 acquire 一致：能立即入场或能排队 → False；仅当全局有界队列已满时 True。
        run-loop 在 create_task 前调用它，队满则直接优雅拒绝、不建 task（避免无界堆积）。
        """
        if not self._waiters and self._can_admit(principal_id):
            return False  # 可立即入场
        if self.max_queue > 0 and len(self._waiters) >= self.max_queue:
            return True  # 队满 → 拒绝
        return False  # 尚可排队

    def release(self, ticket: Ticket) -> None:
        pid = ticket.principal_id
        # M13 改动点⑤：校验非法/重复释放，避免账本被二次释放破坏（原静默 max(0)）。
        if self._global_inflight <= 0 or self._principal_inflight.get(pid, 0) <= 0:
            logger.error(
                "admission release 非法/重复 ticket: principal={} seq={} "
                "global_inflight={} principal_inflight={}",
                pid, ticket.seq, self._global_inflight,
                self._principal_inflight.get(pid, 0),
            )
            return
        self._global_inflight -= 1
        remaining = self._principal_inflight.get(pid, 0) - 1
        if remaining <= 0:
            self._principal_inflight.pop(pid, None)
        else:
            self._principal_inflight[pid] = remaining
        self._pump()

    def _pump(self) -> None:
        """释放/取消后，按 least-in-flight（同值 FIFO）唤醒可入场的等待者。"""
        while self._global_inflight < self.global_limit:
            candidate = self._pick_next_waiter()
            if candidate is None:
                break
            self._waiters.remove(candidate)
            if candidate.future.cancelled():
                continue  # 已被取消的等待者跳过，不占名额
            ticket = self._admit(candidate.principal_id)
            candidate.future.set_result(ticket)

    def _pick_next_waiter(self) -> _Waiter | None:
        """挑下一个入场者：当前 in-flight 最少的 principal 优先，同值按到达序。

        这就是公平核心——把基线的“全局 FIFO”换成“谁占得少谁先来”，
        使刷屏用户无法凭先到就长期霸占名额（缺陷①②）。
        """
        best: _Waiter | None = None
        best_key: tuple[int, int] | None = None
        for w in self._waiters:
            if self._principal_inflight.get(w.principal_id, 0) >= self.per_principal_limit:
                continue  # 该 principal 已达配额，本轮不入场
            key = (self._principal_inflight.get(w.principal_id, 0), w.seq)
            if best_key is None or key < best_key:
                best, best_key = w, key
        return best

    @asynccontextmanager
    async def slot(self, principal_id: str) -> AsyncIterator[Ticket]:
        ticket = await self.acquire(principal_id)
        try:
            yield ticket
        finally:
            self.release(ticket)

    # -- 只读观测 ----------------------------------------------------------

    def suggested_retry_after(self) -> float:
        """建议退避秒数（M13 改动点③）：队列越满建议等得越久，给渠道退避用。

        简单单调启发式：基准 1s，按当前队列占用比例线性放大到最多 +4s。
        纯观测，不影响调度决策。
        """
        if self.max_queue <= 0:
            return 1.0
        ratio = min(1.0, len(self._waiters) / self.max_queue)
        return round(1.0 + 4.0 * ratio, 3)

    @property
    def queue_len(self) -> int:
        return len(self._waiters)

    @property
    def global_inflight(self) -> int:
        return self._global_inflight
