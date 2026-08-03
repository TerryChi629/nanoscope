"""Explainable relevance, freshness, and diversity ranking for visible memories."""

from __future__ import annotations

import math
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from nanoscope.memory.repository import MemoryRecord


def _ngrams(text: str) -> Counter[str]:
    normalized = "".join(text.casefold().split())
    if not normalized:
        return Counter()
    width = 2 if len(normalized) >= 2 else 1
    return Counter(
        normalized[index : index + width]
        for index in range(len(normalized) - width + 1)
    )


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(key, 0) for key, value in left.items())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


@dataclass(frozen=True)
class MemoryRanker:
    """Greedy MMR ranker operating only on Repository-authorized candidates."""

    relevance_weight: float = 0.7
    freshness_weight: float = 0.3
    redundancy_weight: float = 0.2
    half_life_days: float = 30.0

    def __post_init__(self) -> None:
        if self.half_life_days <= 0:
            raise ValueError("half_life_days must be positive")
        if min(
            self.relevance_weight,
            self.freshness_weight,
            self.redundancy_weight,
        ) < 0:
            raise ValueError("memory ranking weights must be non-negative")

    def rank(
        self,
        query: str,
        records: Sequence[MemoryRecord],
        *,
        top_k: int,
        now: float | None = None,
    ) -> list[MemoryRecord]:
        if top_k <= 0 or not records:
            return []
        timestamp = time.time() if now is None else now
        query_vector = _ngrams(query)
        vectors = {record.id: _ngrams(record.content) for record in records}
        half_life_seconds = self.half_life_days * 86_400.0
        base_scores: dict[str, float] = {}
        for record in records:
            relevance = _cosine(query_vector, vectors[record.id])
            age = max(0.0, timestamp - record.created_at)
            freshness = math.exp(-math.log(2.0) * age / half_life_seconds)
            base_scores[record.id] = (
                self.relevance_weight * relevance
                + self.freshness_weight * freshness
            )

        remaining = {record.id: record for record in records}
        selected: list[MemoryRecord] = []
        while remaining and len(selected) < top_k:
            best = max(
                remaining.values(),
                key=lambda record: (
                    base_scores[record.id]
                    - self.redundancy_weight
                    * max(
                        (
                            _cosine(vectors[record.id], vectors[item.id])
                            for item in selected
                        ),
                        default=0.0,
                    ),
                    record.created_at,
                    record.id,
                ),
            )
            selected.append(best)
            remaining.pop(best.id)
        return selected
