"""GLM embedding 客户端 (PRD §14 / P1-A.2)。

向量召回后段：用 GLM `embedding-3` 把文本映射到稠密向量，补 trigram 的语义/词序
盲区（如 query「橘猫豆豆」vs 记忆「豆豆的橘猫」无共同 3-gram，但语义相近）。

设计（自包含、可离线测试）：
- `Embedder` Protocol：`embed(texts) -> list[list[float]]`，评测/检索只依赖此契约。
- `GlmEmbedder`：标准库 urllib 直连 GLM，凭证从**环境变量**注入（绝不入库）：
    GLM_API_KEY   （必需）
    GLM_BASE_URL  （默认 https://open.bigmodel.cn/api/paas/v4）
    GLM_EMBED_MODEL（默认 embedding-3）
- 测试可注入 `FakeEmbedder`（确定性假向量），不依赖网络/密钥。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from threading import RLock
from typing import Protocol

from nanoscope.eval.http_client import JsonHttpClient

_DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
_DEFAULT_MODEL = "embedding-3"
_BATCH = 64  # 单次请求的文本条数上限（保守分批）。


class Embedder(Protocol):
    """把一批文本编码成等长稠密向量。"""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...


class CachingEmbedder:
    """Cache exact text embeddings while preserving the wrapped provider contract."""

    def __init__(self, delegate: Embedder):
        self.delegate = delegate
        self.name = getattr(delegate, "name", type(delegate).__name__)
        self._cache: dict[str, list[float]] = {}
        self._lock = RLock()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        requested = list(texts)
        with self._lock:
            missing = list(dict.fromkeys(text for text in requested if text not in self._cache))
            if missing:
                vectors = self.delegate.embed(missing)
                if len(vectors) != len(missing):
                    raise ValueError("Embedder returned a mismatched vector count")
                self._cache.update(zip(missing, vectors))
            return [list(self._cache[text]) for text in requested]


class GlmEmbedder:
    """GLM embedding-3 客户端（urllib 直连，凭证来自环境变量）。

    dimensions 可选（GLM 支持降维，省存储/加速）；None 用模型默认（2048）。
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        dimensions: int | None = None,
        timeout: float = 30.0,
        requests_per_second: float = 2.0,
        http_client: JsonHttpClient | None = None,
    ):
        self.api_key = api_key or os.environ.get("GLM_API_KEY")
        if not self.api_key:
            raise RuntimeError("缺少 GLM_API_KEY（请设为环境变量，不要写入代码/配置库）")
        self.base_url = (base_url or os.environ.get("GLM_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("GLM_EMBED_MODEL") or _DEFAULT_MODEL
        self.dimensions = dimensions
        self.timeout = timeout
        self.http_client = http_client or JsonHttpClient(
            timeout=timeout,
            requests_per_second=requests_per_second,
        )

    def _post_batch(self, texts: list[str]) -> list[list[float]]:
        payload: dict = {"model": self.model, "input": texts}
        if self.dimensions is not None:
            payload["dimensions"] = self.dimensions
        body = self.http_client.post(
            f"{self.base_url}/embeddings",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            payload=payload,
        )
        # 按 index 复位顺序，避免服务端乱序。
        items = body.get("data")
        if not isinstance(items, list) or len(items) != len(texts):
            raise ValueError("GLM response data count does not match the request")
        try:
            ordered = sorted(items, key=lambda item: item["index"])
            vectors = [item["embedding"] for item in ordered]
        except (KeyError, TypeError):
            raise ValueError("GLM response has an invalid embedding item") from None
        if any(not isinstance(vector, list) or not vector for vector in vectors):
            raise ValueError("GLM response contains an invalid embedding vector")
        return vectors

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        batch = list(texts)
        for i in range(0, len(batch), _BATCH):
            out.extend(self._post_batch(batch[i : i + _BATCH]))
        return out
