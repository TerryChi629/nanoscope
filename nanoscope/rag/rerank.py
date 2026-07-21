"""M18 · Reranker (PRD_v4 §M18.2 第 5 点)。

cross-encoder 重排：对融合后的 top-N 候选，用"query×chunk 联合打分"重排。真实
cross-encoder（如 bge-reranker）留待接入；本模块给出可离线跑的确定性 stub，用于
在自建 gold 上证明"重排后 nDCG/MRR ≥ 重排前"（D5/M18.3）。

红线：rerank 只对**已隔离的可见候选**排序，绝不改变可见性。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class Reranker(Protocol):
    """给定 query 与候选文本，返回重排后的候选下标（相关性降序）。"""

    def rerank(self, query: str, candidates: Sequence[str]) -> list[int]:
        ...


class StubReranker:
    """确定性 stub cross-encoder：用"query 与 chunk 的字符级重合度"作联合分。

    重合度 = |q_chars ∩ c_chars| / |q_chars|（Jaccard 变体，对 CJK 友好、可复现）。
    这不是真 cross-encoder，但足以在合成 gold 上演示重排管线与增益度量；真实模型
    接入后此 stub 可整体替换（`Reranker` 契约不变）。
    """

    name = "stub_reranker"

    def rerank(self, query: str, candidates: Sequence[str]) -> list[int]:
        q = set(query.strip())
        if not q or not candidates:
            return list(range(len(candidates)))
        scored = [
            (idx, len(q & set(text)) / len(q))
            for idx, text in enumerate(candidates)
        ]
        # 稳定降序：分数高在前，同分按原下标（确定性 tie-break）。
        scored.sort(key=lambda kv: (-kv[1], kv[0]))
        return [idx for idx, _ in scored]
