"""检索器对照组 (PRD §14.2/§14.5)。

规模曲线要证明"检索 > 全量注入/grep"以及"该上 RAG 的拐点 M₀"，需要在
**同一份隔离后可见集合**上跑多个对照检索器：

- GrepRetriever   : 基线代理——大小写不敏感子串匹配，命中集按新近度排序
                    （对应基线的"grep/近邻窗口",无相关性 ranking）。
- Bm25Retriever   : SQLite FTS5 trigram + bm25() 相关性排序（P1-A.1）。
- VectorRetriever  : 向量召回（P1-A.2，GLM embedding），留待后段接入。
- RrfRetriever    : 多路倒数秩融合（P1-A.2）。

红线：所有检索器只在"已隔离的可见集合"内排序，绝不决定可见性。
本模块用于离线评测，语料是 (id, content) 对，隔离已在上游 Repository 完成。
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from nanoscope.eval.embedding import Embedder
from nanoscope.memory.repository import _build_trigram_match


@dataclass(frozen=True)
class Doc:
    """评测语料的一条记忆（隔离后可见项）。created_at 越大越新。"""

    id: str
    content: str
    created_at: int = 0


class Retriever(Protocol):
    """检索器契约：给定 query，返回相关性降序的 doc id 列表。"""

    name: str

    def search(self, query: str, top_k: int) -> list[str]:
        ...


class GrepRetriever:
    """基线代理：trigram 候选集 + 按 created_at 新近度排序（无相关性 ranking）。

    与 Bm25Retriever 用**同一份 trigram 候选集**（同样的召回口径），唯一差异是
    排序方式——grep 按新近度、bm25 按相关性。这样规模曲线隔离出的唯一变量就是
    "相关性排序 vs 近邻窗口"，直接对应 PRD §14.5 要证明的命题。

    随规模失效的机制：N 增大→与 query 共享 trigram 的干扰项变多→gold 被更新的
    干扰项挤出 top-K→grep 的 Recall@K 掉线；bm25 按相关性把 gold 顶在前面故守住。
    """

    name = "grep"

    def __init__(self, docs: Sequence[Doc]):
        self._docs = list(docs)

    @staticmethod
    def _trigrams(text: str) -> set[str]:
        # NanoScope (PRD_v4 §M14, 修 H5)：大小写不敏感——用 casefold 与文档声明一致。
        t = text.strip().casefold()
        return {t[i : i + 3] for i in range(len(t) - 2)} if len(t) >= 3 else set()

    def search(self, query: str, top_k: int) -> list[str]:
        q_grams = self._trigrams(query)
        if not q_grams:
            return []
        hits = [d for d in self._docs if self._trigrams(d.content) & q_grams]
        hits.sort(key=lambda d: d.created_at, reverse=True)
        return [d.id for d in hits[:top_k]]


class Bm25Retriever:
    """SQLite FTS5 trigram + bm25() 相关性排序（P1-A.1）。

    在内存库里建 trigram 影子倒排，与 Repository 用同一套 trigram 匹配式构造，
    保证评测里的 BM25 行为与线上 search_visible 一致。
    """

    name = "bm25"

    def __init__(self, docs: Sequence[Doc], db_path: str | Path | None = None):
        # NanoScope (PRD_v4 §M14, 修 H5)：默认用内存库，不再用不安全的 tempfile.mktemp
        # （避免临时 .db 文件泄漏 + 竞态）。显式传 db_path 时才落盘。
        self._path = str(db_path) if db_path else ":memory:"
        self._conn = sqlite3.connect(self._path)
        self._conn.executescript(
            "CREATE VIRTUAL TABLE docs USING fts5(content, id UNINDEXED, tokenize='trigram');"
        )
        self._conn.executemany(
            "INSERT INTO docs (id, content) VALUES (?, ?)",
            [(d.id, d.content) for d in docs],
        )
        self._conn.commit()

    def search(self, query: str, top_k: int) -> list[str]:
        match = _build_trigram_match(query)
        if match is None:
            return []
        rows = self._conn.execute(
            "SELECT id FROM docs WHERE docs MATCH ? ORDER BY bm25(docs) LIMIT ?",
            (match, top_k),
        ).fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        self._conn.close()


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度（纯 Python，不引入 numpy）。零向量返回 0。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class VectorRetriever:
    """向量召回（P1-A.2）：GLM embedding + 余弦相似度排序。

    补 trigram 的语义/词序盲区——如 query「橘猫豆豆」vs 记忆「豆豆的橘猫」无共同
    3-gram（BM25 漏召回），但语义相近、向量近邻能召回。
    构造时对全部 doc 预取 embedding（一次批量），search 时对 query 取 embedding
    后按余弦降序返回。所有向量只在"已隔离可见集合"内排序，绝不决定可见性。
    """

    name = "vector"

    def __init__(self, docs: Sequence[Doc], embedder: Embedder):
        self._docs = list(docs)
        self._embedder = embedder
        self._vecs = embedder.embed([d.content for d in self._docs]) if self._docs else []

    def search(self, query: str, top_k: int, min_score: float = 0.0) -> list[str]:
        # NanoScope (PRD_v4 §M14, 修 H5)：min_score 过滤零/负分——query 与全库无关时
        # 返回空（abstention），不再返回一批零分文档虚增召回。
        if not self._docs:
            return []
        q = self._embedder.embed([query])[0]
        scored = [(d.id, _cosine(q, v)) for d, v in zip(self._docs, self._vecs)]
        scored = [(doc_id, s) for doc_id, s in scored if s > min_score]
        scored.sort(key=lambda kv: kv[1], reverse=True)
        return [doc_id for doc_id, _ in scored[:top_k]]


def rrf_fuse(rankings: Sequence[Sequence[str]], k: int = 60, top_k: int = 20) -> list[str]:
    """倒数秩融合（PRD §14.3）：RRF_score(d) = Σ_r 1/(k + rank_r(d))。

    只依赖名次不依赖分数，天然免掉不同检索器分数量纲对齐问题；某路未召回该项
    贡献 0，自动降权。返回融合后相关性降序的 id 列表。

    NanoScope (PRD_v4 §M14, 修 H5)：确定性 tie-break——排序键为
    `(RRF_score desc, hit_count desc, best_rank asc, doc_id asc)`，消除对 dict
    插入顺序的依赖，保证多次运行 / 打乱子检索器顺序时输出稳定一致。空输入 abstain。
    """
    scores: dict[str, float] = {}
    hit_count: dict[str, int] = {}
    best_rank: dict[str, int] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
            hit_count[doc_id] = hit_count.get(doc_id, 0) + 1
            best_rank[doc_id] = min(best_rank.get(doc_id, rank), rank)
    ordered = sorted(
        scores.items(),
        key=lambda kv: (-kv[1], -hit_count[kv[0]], best_rank[kv[0]], kv[0]),
    )
    return [doc_id for doc_id, _ in ordered[:top_k]]


class RrfRetriever:
    """多路检索器的 RRF 融合（P1-A.2），本身也是一个 Retriever。

    对每路子检索器取 top-N 名次，再按 RRF_score 融合成一个排序。用于规模曲线里
    直接把 "BM25 + 向量" 融合结果和单路对照。fetch 时每路多取一些（fanout）以免
    融合前被 top_k 截断丢掉有价值的名次。
    """

    def __init__(
        self,
        retrievers: Sequence[Retriever],
        *,
        name: str = "rrf",
        k: int = 60,
        fanout: int = 50,
    ):
        self._retrievers = list(retrievers)
        self.name = name
        self._k = k
        self._fanout = fanout

    def search(self, query: str, top_k: int) -> list[str]:
        n = max(top_k, self._fanout)
        rankings = [r.search(query, n) for r in self._retrievers]
        return rrf_fuse(rankings, k=self._k, top_k=top_k)
