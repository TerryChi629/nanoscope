"""M18 · Reranker (PRD_v4 §M18.2 第 5 点)。

cross-encoder 重排：对融合后的 top-N 候选，用"query×chunk 联合打分"重排。本模块给出
两档实现，共用 `Reranker` 契约：
- `StubReranker`：可离线跑的确定性 stub（字符级重合度），用于在自建 gold 上证明
  "重排后 nDCG/MRR ≥ 重排前"（D5/M18.3），无需网络/密钥。
- `SiliconFlowReranker`：真实 cross-encoder（SiliconFlow 托管 `bge-reranker-v2-m3`），
  urllib 直连，凭证从**环境变量**注入（绝不入库），风格对齐 `eval.embedding.GlmEmbedder`。

红线：rerank 只对**已隔离的可见候选**排序，绝不改变可见性。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Protocol

from nanoscope.eval.http_client import JsonHttpClient


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


_DEFAULT_SF_BASE_URL = "https://api.siliconflow.cn/v1"
_DEFAULT_SF_MODEL = "BAAI/bge-reranker-v2-m3"


class SiliconFlowReranker:
    """真实 cross-encoder 重排（SiliconFlow 托管 `bge-reranker-v2-m3`）。

    与 `StubReranker` 同实现 `Reranker` 契约，因此 `doc_search_visible` 主链路一行不改
    即可注入；stub 保留为离线/缺密钥时的 fallback。凭证走**环境变量**（绝不入库）：
        SILICONFLOW_API_KEY   （必需）
        SILICONFLOW_BASE_URL  （默认 https://api.siliconflow.cn/v1）
        SILICONFLOW_RERANK_MODEL（默认 BAAI/bge-reranker-v2-m3）

    行为红线：只对已隔离的可见候选**重排下标**，不增删候选、不改变可见性。返回的下标
    是输入 candidates 的一个全排列——服务端只返回 top_n 时，未覆盖的下标按原序补齐，
    保证「重排只调序、不丢候选」的契约不破。
    """

    name = "siliconflow_bge_reranker_v2_m3"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
        requests_per_second: float = 2.0,
        http_client: JsonHttpClient | None = None,
    ):
        self.api_key = api_key or os.environ.get("SILICONFLOW_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "缺少 SILICONFLOW_API_KEY（请设为环境变量，不要写入代码/配置库）"
            )
        self.base_url = (
            base_url or os.environ.get("SILICONFLOW_BASE_URL") or _DEFAULT_SF_BASE_URL
        ).rstrip("/")
        self.model = model or os.environ.get("SILICONFLOW_RERANK_MODEL") or _DEFAULT_SF_MODEL
        self.timeout = timeout
        self.http_client = http_client or JsonHttpClient(
            timeout=timeout,
            requests_per_second=requests_per_second,
        )

    def rerank(self, query: str, candidates: Sequence[str]) -> list[int]:
        docs = list(candidates)
        if not query.strip() or not docs:
            return list(range(len(docs)))
        payload = {
            "model": self.model,
            "query": query,
            "documents": docs,
            "top_n": len(docs),
            "return_documents": False,
        }
        body = self.http_client.post(
            f"{self.base_url}/rerank",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            payload=payload,
        )
        # 服务端按相关性降序返回 {index, relevance_score}；只取 index 即重排序。
        results = body.get("results")
        if not isinstance(results, list):
            raise ValueError("SiliconFlow response results must be a list")
        try:
            order = [int(item["index"]) for item in results]
        except (KeyError, TypeError, ValueError):
            raise ValueError("SiliconFlow response has an invalid result index") from None
        valid = [index for index in order if 0 <= index < len(docs)]
        if len(set(valid)) != len(valid):
            raise ValueError("SiliconFlow response contains duplicate result indexes")
        # fail-closed 兜底：服务端漏返/越界的下标按原序补齐，绝不丢候选、不改可见性。
        seen = set(valid)
        valid.extend(i for i in range(len(docs)) if i not in seen)
        return valid
