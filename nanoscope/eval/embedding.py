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

import hashlib
import json
import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path
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


class PersistentCachingEmbedder:
    """按文本 SHA-256 持久化向量；缓存不保存原文与凭证，支持失败后续跑。"""

    def __init__(
        self,
        delegate: Embedder,
        path: str | Path,
        *,
        namespace: str | None = None,
        write_batch_size: int = 64,
    ):
        if write_batch_size <= 0:
            raise ValueError("write_batch_size must be positive")
        self.delegate = delegate
        self.name = getattr(delegate, "name", type(delegate).__name__)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace or self._default_namespace(delegate)
        self.write_batch_size = write_batch_size
        self._lock = RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS embeddings ("
            "namespace TEXT NOT NULL,"
            "text_sha256 TEXT NOT NULL,"
            "vector_json TEXT NOT NULL,"
            "PRIMARY KEY(namespace, text_sha256)"
            ")"
        )
        self._conn.commit()

    @staticmethod
    def _default_namespace(delegate: Embedder) -> str:
        identity = {
            "class": type(delegate).__name__,
            "model": getattr(delegate, "model", None),
            "dimensions": getattr(delegate, "dimensions", None),
            "base_url": getattr(delegate, "base_url", None),
        }
        return json.dumps(identity, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _load(self, hashes: Sequence[str]) -> dict[str, list[float]]:
        cached: dict[str, list[float]] = {}
        unique = list(dict.fromkeys(hashes))
        for offset in range(0, len(unique), 400):
            batch = unique[offset : offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(
                "SELECT text_sha256, vector_json FROM embeddings "
                f"WHERE namespace = ? AND text_sha256 IN ({placeholders})",
                (self.namespace, *batch),
            ).fetchall()
            for text_hash, vector_json in rows:
                vector = json.loads(vector_json)
                if not isinstance(vector, list) or not vector:
                    raise ValueError("Persistent embedding cache contains an invalid vector")
                cached[text_hash] = vector
        return cached

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        requested = list(texts)
        hashes = [self._hash(text) for text in requested]
        with self._lock:
            cached = self._load(hashes)
            missing_by_hash: dict[str, str] = {}
            for text, text_hash in zip(requested, hashes):
                if text_hash not in cached:
                    missing_by_hash.setdefault(text_hash, text)

            missing = list(missing_by_hash.items())
            for offset in range(0, len(missing), self.write_batch_size):
                batch = missing[offset : offset + self.write_batch_size]
                vectors = self.delegate.embed([text for _text_hash, text in batch])
                if len(vectors) != len(batch):
                    raise ValueError("Embedder returned a mismatched vector count")
                rows = []
                for (text_hash, _text), vector in zip(batch, vectors):
                    cached[text_hash] = list(vector)
                    rows.append(
                        (
                            self.namespace,
                            text_hash,
                            json.dumps(vector, separators=(",", ":")),
                        )
                    )
                self._conn.executemany(
                    "INSERT OR REPLACE INTO embeddings "
                    "(namespace, text_sha256, vector_json) VALUES (?,?,?)",
                    rows,
                )
                self._conn.commit()
            return [list(cached[text_hash]) for text_hash in hashes]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


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
        batch_size: int = _BATCH,
        timeout: float = 30.0,
        requests_per_second: float = 2.0,
        http_client: JsonHttpClient | None = None,
    ):
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.api_key = api_key or os.environ.get("GLM_API_KEY")
        if not self.api_key:
            raise RuntimeError("缺少 GLM_API_KEY（请设为环境变量，不要写入代码/配置库）")
        self.base_url = (base_url or os.environ.get("GLM_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("GLM_EMBED_MODEL") or _DEFAULT_MODEL
        self.dimensions = dimensions
        self.batch_size = batch_size
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
        for i in range(0, len(batch), self.batch_size):
            out.extend(self._post_batch(batch[i : i + self.batch_size]))
        return out
