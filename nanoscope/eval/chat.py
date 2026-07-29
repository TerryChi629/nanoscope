"""轻量 chat LLM 客户端（评测 L3 生成用）。

RAG 链路最后一步「读证据写答案」需要一个 chat 模型。nanoscope 评测子包此前只有
embedding（`GlmEmbedder`）与 rerank（`SiliconFlowReranker`）两个 urllib 直连客户端，
没有 chat 客户端——本模块补齐这一档，风格与凭证纪律完全对齐：

- `ChatModel` Protocol：`complete(system, user) -> str`，评测只依赖此契约。
- `DeepSeekChat`：标准库 urllib 直连 DeepSeek（OpenAI 兼容 /chat/completions），
  凭证从**环境变量**注入（绝不入库）：
    DEEPSEEK_API_KEY   （必需）
    DEEPSEEK_BASE_URL  （默认 https://api.deepseek.com）
    DEEPSEEK_MODEL     （默认 deepseek-chat）
- 测试可注入 `FakeChat`（确定性假回答），不依赖网络/密钥。
"""

from __future__ import annotations

import os
from typing import Protocol

from nanoscope.eval.http_client import JsonHttpClient

_DEFAULT_BASE_URL = "https://api.deepseek.com"
_DEFAULT_MODEL = "deepseek-chat"


class ChatModel(Protocol):
    """给定 system + user 提示，返回一段回答文本。"""

    def complete(self, system: str, user: str) -> str:
        ...


class DeepSeekChat:
    """DeepSeek chat 客户端（urllib 直连，凭证来自环境变量）。

    temperature 默认 0.0：评测要求可复现，不引入采样随机性。
    """

    name = "deepseek_chat"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 512,
        timeout: float = 60.0,
        requests_per_second: float = 2.0,
        http_client: JsonHttpClient | None = None,
    ):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "缺少 DEEPSEEK_API_KEY（请设为环境变量，不要写入代码/配置库）"
            )
        self.base_url = (
            base_url or os.environ.get("DEEPSEEK_BASE_URL") or _DEFAULT_BASE_URL
        ).rstrip("/")
        self.model = model or os.environ.get("DEEPSEEK_MODEL") or _DEFAULT_MODEL
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.http_client = http_client or JsonHttpClient(
            timeout=timeout,
            requests_per_second=requests_per_second,
        )
        # 评测可观测：累计 token 用量（供 CPS 成本估算）。
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        body = self.http_client.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            payload=payload,
        )
        usage = body.get("usage") or {}
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("DeepSeek response has no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise ValueError("DeepSeek response content must be a string")
        if not isinstance(usage, dict):
            raise ValueError("DeepSeek response usage must be an object")
        self.prompt_tokens += int(usage.get("prompt_tokens", 0))
        self.completion_tokens += int(usage.get("completion_tokens", 0))
        return content
