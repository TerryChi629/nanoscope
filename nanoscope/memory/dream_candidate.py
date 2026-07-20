"""M8 owner-aware Dream candidate (PRD §7 P1 / §11-M8)。

闭合 Dream 后门后（M5：multi_user 下 Dream 禁写三全局文件），本模块**恢复蒸馏能力**
但把 owner 边界做成确定性的：

设计（确定性、fail-closed，不让 LLM 判 owner）：
1. history 已在写入时持久化 `principal_id`（M1）。
2. **进 LLM 前就按 principal 分批**：每个 principal 的历史拼成**独立** Dream prompt，
   绝不把多人历史混进同一个 prompt（对比基线后门：所有人历史进同一 prompt）。
3. distiller（LLM 边界）只看**单个 principal** 的历史，只输出候选事实文本；
   **owner 由批次确定性继承**（= 该批的 principal_id），即便 LLM 试图指定别的 owner
   也被忽略——归属不是概率性判断。
4. 候选经 `Repository.add`（scope='user', owner=批次 principal）事务入库，仍受 DB CHECK
   + search_visible DM 门约束。

安全红线：无 `principal_id` 的历史条目 **fail-closed 丢弃**（宁可不蒸馏，绝不归错 owner）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from nanoscope.identity import AUDIENCE_DM, SecurityContext
from nanoscope.memory.repository import SCOPE_USER, MemoryRecord, Repository

_SOURCE_TYPE = "dream"


@dataclass(frozen=True)
class PrincipalBatch:
    """单个 principal 的历史批次——喂给 distiller 的最小、不混人单元。"""

    principal_id: str
    entries: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class MemoryCandidate:
    """一条待入库的蒸馏候选。owner 恒等于所属批次的 principal_id（确定性继承）。"""

    principal_id: str
    content: str


class Distiller(Protocol):
    """蒸馏器契约（LLM 边界）：只看单 principal 历史，产出候选事实文本列表。

    实现（真实 LLM 或测试 Fake）**拿不到、也无权决定 owner**——owner 在本模块外层
    按批次确定性赋予。返回的字符串就是候选 content。
    """

    def distill(self, principal_id: str, entries: Sequence[dict[str, Any]]) -> list[str]:
        ...


def group_by_principal(entries: Sequence[dict[str, Any]]) -> list[PrincipalBatch]:
    """把历史按 `principal_id` 分批。无 principal_id 的条目 fail-closed 丢弃。

    保持 principal 首次出现的顺序（可复现）；每批内保持原历史顺序。
    """
    order: list[str] = []
    buckets: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        pid = e.get("principal_id")
        if not pid:
            continue  # fail-closed：无 owner 的历史绝不蒸馏。
        if pid not in buckets:
            buckets[pid] = []
            order.append(pid)
        buckets[pid].append(e)
    return [PrincipalBatch(principal_id=pid, entries=tuple(buckets[pid])) for pid in order]


def distill_candidates(
    entries: Sequence[dict[str, Any]],
    distiller: Distiller,
) -> list[MemoryCandidate]:
    """按 principal 分批蒸馏，返回候选列表。owner 由批次确定性继承。

    每个 principal 单独调一次 distiller（独立 prompt，不混人）；distiller 返回的
    每条文本包一层 MemoryCandidate，owner = 该批 principal_id（LLM 无权更改）。
    """
    candidates: list[MemoryCandidate] = []
    for batch in group_by_principal(entries):
        for content in distiller.distill(batch.principal_id, batch.entries):
            text = (content or "").strip()
            if not text:
                continue
            # owner 确定性继承批次 principal——不读 distiller 输出里的任何 owner 字段。
            candidates.append(MemoryCandidate(principal_id=batch.principal_id, content=text))
    return candidates


def commit_candidates(
    candidates: Sequence[MemoryCandidate],
    repository: Repository,
    *,
    tenant_id: str,
) -> list[MemoryRecord]:
    """把候选写入 SQLite（scope='user', owner=候选 principal）。

    用 DM 语境 SecurityContext 走 Repository.add——owner/tenant 运行时注入，
    仍受 DB CHECK 与 search_visible DM 门约束。返回写入的记录。
    """
    written: list[MemoryRecord] = []
    for cand in candidates:
        ctx = SecurityContext(
            tenant_id=tenant_id,
            principal_id=cand.principal_id,
            session_key=f"dream:{cand.principal_id}",
            audience_type=AUDIENCE_DM,
        )
        rec = repository.add(
            ctx,
            content=cand.content,
            scope=SCOPE_USER,
            source_type=_SOURCE_TYPE,
            source_ref=f"dream:{cand.principal_id}",
        )
        written.append(rec)
    return written


def run_dream_candidates(
    entries: Sequence[dict[str, Any]],
    distiller: Distiller,
    repository: Repository,
    *,
    tenant_id: str,
) -> list[MemoryRecord]:
    """端到端：分批蒸馏 → 确定性归属 → 入库。返回写入的记录。"""
    candidates = distill_candidates(entries, distiller)
    return commit_candidates(candidates, repository, tenant_id=tenant_id)
