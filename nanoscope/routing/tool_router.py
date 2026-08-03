"""Deterministic retrieval-based routing over tool JSON schemas."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

RoutingStrategy = Literal["all_tools", "bm25_topk", "hybrid_topk"]

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")
_DEFAULT_ALIASES: dict[str, str] = {
    "calculate": (
        "计算 数学 算术 加减乘除 平均 增加 扣除 剩余 均摊 每段 "
        "核算 总数 总量 合计 实例总数 副本总数 多少"
    ),
    "document_search": "文档 知识库 内部资料 检索 查找",
    "web_search": "联网 网络 搜索 新闻 最新 官方",
    "web_fetch": "网页 网址 URL 正文 读取",
    "read_file": "读取 打开 查看 本地 文件 配置",
    "write_file": "写入 保存 创建 文件 报告",
    "list_dir": "目录 文件夹 列表 有哪些文件",
    "memory_remember": "记住 记忆 偏好 长期保存",
    "create_task": "创建 新建 任务 待办 指派",
    "cron": "定时 周期 每天 每周 调度 计划",
    "lookup": (
        "查询 查找 获取 确认 当前 配置 区域 地区 负责人 值班 超时 时间 "
        "发布 模式 基础 容量 副本 预算 延迟"
    ),
    "unstable_lookup": (
        "查询 获取 确认 当前 配置 临时 异常 偶发 失败 恢复 瞬态 "
        "配额 API 索引 版本 轮换 周期 批处理 大小"
    ),
}
_NO_TOOL_MARKERS = (
    "只依据上下文",
    "仅依据上下文",
    "根据给定上下文",
    "心算",
    "手机号",
    "身份证号",
    "私人联系方式",
)


def _schema_parts(schema: dict[str, Any]) -> tuple[str, str]:
    function = schema.get("function")
    body = function if isinstance(function, dict) else schema
    name = str(body.get("name") or "")
    description = str(body.get("description") or "")
    parameters = body.get("parameters")
    properties = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
    parameter_text = " ".join(
        f"{key} {value.get('description', '') if isinstance(value, dict) else ''}"
        for key, value in properties.items()
    )
    return name, f"{name.replace('_', ' ')} {description} {parameter_text}".strip()


def _tokens(text: str) -> list[str]:
    normalized = text.casefold().replace("_", " ")
    base = [
        token
        for token in _TOKEN_RE.findall(normalized)
        if not all("\u4e00" <= char <= "\u9fff" for char in token)
    ]
    cjk = "".join(char for char in normalized if "\u4e00" <= char <= "\u9fff")
    grams = [cjk[index : index + 2] for index in range(max(0, len(cjk) - 1))]
    return base + grams


def _char_ngrams(text: str) -> Counter[str]:
    normalized = "".join(text.casefold().split())
    if not normalized:
        return Counter()
    width = 3 if len(normalized) >= 3 else len(normalized)
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
class RouteResult:
    """Selected definitions and auditable rankings for one query."""

    definitions: tuple[dict[str, Any], ...]
    selected_names: tuple[str, ...]
    lexical_ranking: tuple[str, ...]
    dense_ranking: tuple[str, ...]


class ToolRouter:
    """Rank registered tool schemas without changing the executable registry."""

    def __init__(
        self,
        definitions: Sequence[dict[str, Any]],
        *,
        strategy: RoutingStrategy = "hybrid_topk",
        top_k: int = 6,
        always_include: Sequence[str] = (),
        rrf_k: int = 60,
        aliases: dict[str, str] | None = None,
        allow_empty: bool = False,
    ) -> None:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        if strategy not in {"all_tools", "bm25_topk", "hybrid_topk"}:
            raise ValueError(f"unsupported tool routing strategy: {strategy}")
        self._definitions = tuple(definitions)
        self.strategy = strategy
        self.top_k = top_k
        self.always_include = tuple(dict.fromkeys(always_include))
        self.rrf_k = rrf_k
        self.allow_empty = allow_empty
        alias_map = dict(_DEFAULT_ALIASES)
        alias_map.update(aliases or {})
        self._documents = [
            (name, f"{text} {alias_map.get(name, '')}".strip())
            for name, text in map(_schema_parts, self._definitions)
        ]
        self._by_name = {
            name: schema
            for (name, _), schema in zip(self._documents, self._definitions)
            if name
        }
        self._doc_tokens = [Counter(_tokens(text)) for _, text in self._documents]
        self._doc_ngrams = [_char_ngrams(text) for _, text in self._documents]
        self._document_frequency = Counter(
            token
            for tokens in self._doc_tokens
            for token in tokens
        )
        self._average_length = (
            sum(sum(tokens.values()) for tokens in self._doc_tokens) / len(self._doc_tokens)
            if self._doc_tokens
            else 0.0
        )

    def _bm25_ranking(self, query: str) -> list[str]:
        query_tokens = Counter(_tokens(query))
        if not query_tokens:
            return []
        n_documents = len(self._documents)
        scores: list[tuple[str, float]] = []
        for (name, _), frequencies in zip(self._documents, self._doc_tokens):
            length = sum(frequencies.values())
            score = 0.0
            for token, query_frequency in query_tokens.items():
                frequency = frequencies.get(token, 0)
                if frequency == 0:
                    continue
                document_frequency = self._document_frequency[token]
                inverse_frequency = math.log(
                    1.0 + (n_documents - document_frequency + 0.5)
                    / (document_frequency + 0.5)
                )
                denominator = frequency + 1.5 * (
                    1.0 - 0.75
                    + 0.75 * length / max(self._average_length, 1.0)
                )
                score += query_frequency * inverse_frequency * frequency * 2.5 / denominator
            if score > 0.0:
                scores.append((name, score))
        scores.sort(key=lambda item: (-item[1], item[0]))
        return [name for name, _ in scores]

    def _dense_ranking(self, query: str) -> list[str]:
        query_vector = _char_ngrams(query)
        scores = [
            (name, _cosine(query_vector, vector))
            for (name, _), vector in zip(self._documents, self._doc_ngrams)
        ]
        scores = [item for item in scores if item[1] > 0.0]
        scores.sort(key=lambda item: (-item[1], item[0]))
        return [name for name, _ in scores]

    def _explicit_names(self, query: str) -> list[str]:
        normalized = query.casefold()
        return [
            name
            for name in self._by_name
            if name.casefold() in normalized
            or name.replace("_", " ").casefold() in normalized
        ]

    def route(self, query: str) -> RouteResult:
        if self.strategy == "all_tools" or not query.strip():
            names = tuple(name for name, _ in self._documents if name)
            return RouteResult(self._definitions, names, names, names)
        if self.allow_empty and any(marker in query for marker in _NO_TOOL_MARKERS):
            return RouteResult((), (), (), ())

        lexical = self._bm25_ranking(query)
        dense = self._dense_ranking(query)
        if self.strategy == "bm25_topk":
            ranking = lexical
        else:
            scores: dict[str, float] = {}
            for source in (lexical, dense):
                for rank, name in enumerate(source, start=1):
                    scores[name] = scores.get(name, 0.0) + 1.0 / (self.rrf_k + rank)
            ranking = sorted(scores, key=lambda name: (-scores[name], name))

        selected = list(ranking[: self.top_k])
        for name in (*self._explicit_names(query), *self.always_include):
            if name in self._by_name and name not in selected:
                selected.append(name)

        if not selected:
            if self.allow_empty:
                return RouteResult((), (), tuple(lexical), tuple(dense))
            names = tuple(name for name, _ in self._documents if name)
            return RouteResult(self._definitions, names, tuple(lexical), tuple(dense))

        selected_set = set(selected)
        definitions = tuple(
            schema
            for (name, _), schema in zip(self._documents, self._definitions)
            if name in selected_set
        )
        names = tuple(_schema_parts(schema)[0] for schema in definitions)
        return RouteResult(definitions, names, tuple(lexical), tuple(dense))
