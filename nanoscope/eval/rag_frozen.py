"""Frozen RAG v1 dataset metadata and deterministic query paraphrases."""

from __future__ import annotations

RAG_DATASET_VERSION = "rag96.v1"
RAG_QUERY_VARIANTS = (
    "{query}",
    "请根据文档回答：{query}",
    "换一种说法，我想确认：{query}",
    "只依据可见资料说明：{query}",
)

RAG_GROUP_COUNTS = {
    "recall": 24,
    "rerank": 20,
    "abstain": 12,
    "isolation": 20,
    "generation": 20,
}


def query_variants(query: str) -> tuple[str, ...]:
    return tuple(template.format(query=query) for template in RAG_QUERY_VARIANTS)
