"""Deterministic offline benchmark for query-aware tool disclosure."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from nanoscope.eval.artifacts import atomic_write_json
from nanoscope.routing import ToolRouter


def _schema(name: str, description: str, parameters: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    parameter: {"type": "string", "description": parameter.replace("_", " ")}
                    for parameter in parameters
                },
            },
        },
    }


FROZEN_TOOL_SCHEMAS = (
    _schema("calculate", "Evaluate a mathematical expression and arithmetic", ("expression",)),
    _schema("document_search", "Search authorized documents and knowledge base", ("query",)),
    _schema("web_search", "Search the public internet for current information", ("query",)),
    _schema("web_fetch", "Fetch and read one web page URL", ("url",)),
    _schema("read_file", "Read a local file from the workspace", ("path",)),
    _schema("write_file", "Create or overwrite a local file", ("path", "content")),
    _schema("list_dir", "List files and directories in a workspace path", ("path",)),
    _schema("memory_remember", "Store a durable personal memory for the user", ("content",)),
    _schema("create_task", "Create a confirmed project task for an assignee", ("title",)),
    _schema("cron", "Create or manage a scheduled recurring job", ("action", "cron_expression")),
)


@dataclass(frozen=True)
class ToolRoutingCase:
    case_id: str
    query: str
    gold_tools: frozenset[str]


FROZEN_TOOL_ROUTING_CASES = (
    ToolRoutingCase("T01", "计算 128 除以 4", frozenset({"calculate"})),
    ToolRoutingCase("T02", "从内部知识库查找量化方案", frozenset({"document_search"})),
    ToolRoutingCase("T03", "搜索今天发布的模型新闻", frozenset({"web_search"})),
    ToolRoutingCase("T04", "读取这个网页 URL 的正文", frozenset({"web_fetch"})),
    ToolRoutingCase("T05", "打开项目里的 pyproject.toml", frozenset({"read_file"})),
    ToolRoutingCase("T06", "把分析结果写入 report.md", frozenset({"write_file"})),
    ToolRoutingCase("T07", "看看当前目录有哪些文件", frozenset({"list_dir"})),
    ToolRoutingCase("T08", "记住我的默认编程语言是 Python", frozenset({"memory_remember"})),
    ToolRoutingCase("T09", "给张三创建一个修复召回率的任务", frozenset({"create_task"})),
    ToolRoutingCase("T10", "每周一上午九点运行评测", frozenset({"cron"})),
    ToolRoutingCase(
        "T11",
        "先在知识库查预算，再计算平均到三个项目",
        frozenset({"document_search", "calculate"}),
    ),
    ToolRoutingCase(
        "T12",
        "搜索官方文档并读取搜索结果页面",
        frozenset({"web_search", "web_fetch"}),
    ),
)


@dataclass(frozen=True)
class ToolRoutingBoard:
    strategy: str
    top_k: int
    precision: float
    recall: float
    disclosure_reduction: float
    n_cases: int
    n_missed: int


def run_tool_routing_board(
    *,
    strategy: str,
    top_k: int,
    schemas: tuple[dict[str, Any], ...] = FROZEN_TOOL_SCHEMAS,
    cases: tuple[ToolRoutingCase, ...] = FROZEN_TOOL_ROUTING_CASES,
) -> ToolRoutingBoard:
    router = ToolRouter(schemas, strategy=strategy, top_k=top_k)
    true_positive = 0
    selected_total = 0
    gold_total = 0
    missed = 0
    for case in cases:
        selected = set(router.route(case.query).selected_names)
        hits = selected & case.gold_tools
        true_positive += len(hits)
        selected_total += len(selected)
        gold_total += len(case.gold_tools)
        if hits != case.gold_tools:
            missed += 1
    precision = true_positive / selected_total if selected_total else 0.0
    recall = true_positive / gold_total if gold_total else 1.0
    full_disclosure = len(schemas) * len(cases)
    reduction = 1.0 - selected_total / full_disclosure if full_disclosure else 0.0
    return ToolRoutingBoard(
        strategy=strategy,
        top_k=top_k,
        precision=precision,
        recall=recall,
        disclosure_reduction=reduction,
        n_cases=len(cases),
        n_missed=missed,
    )


def write_offline_artifact(path: str | Path) -> Path:
    output = Path(path)
    boards = [
        asdict(run_tool_routing_board(strategy=strategy, top_k=top_k))
        for strategy, top_k in (
            ("all_tools", len(FROZEN_TOOL_SCHEMAS)),
            ("bm25_topk", 2),
            ("hybrid_topk", 2),
            ("hybrid_topk", 3),
        )
    ]
    atomic_write_json(
        output,
        {
            "schema_version": "1.0",
            "artifact_type": "tool_routing_offline",
            "dataset_version": "tool-routing12.v1",
            "n_tools": len(FROZEN_TOOL_SCHEMAS),
            "n_cases": len(FROZEN_TOOL_ROUTING_CASES),
            "boards": boards,
        },
    )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="reports/baselines/TOOL_ROUTING_OFFLINE_V1.json",
    )
    args = parser.parse_args()
    print(write_offline_artifact(args.output))


if __name__ == "__main__":
    main()
