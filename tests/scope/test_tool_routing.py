from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from agent.runner_helpers import make_run_spec

from nanobot.agent.runner import AgentRunner
from nanobot.config.schema import ToolRoutingConfig
from nanobot.providers.base import LLMResponse
from nanoscope.eval.agent_live_dataset import load_agent_live_v1
from nanoscope.eval.agent_live_runner import LiveToolHarness
from nanoscope.eval.tool_routing_bench import FROZEN_TOOL_SCHEMAS, run_tool_routing_board
from nanoscope.routing import ToolRouter


def test_hybrid_router_keeps_explicit_and_forced_tools():
    router = ToolRouter(
        FROZEN_TOOL_SCHEMAS,
        strategy="hybrid_topk",
        top_k=1,
        always_include=("document_search",),
    )

    result = router.route("请明确调用 read_file 打开配置")

    assert "read_file" in result.selected_names
    assert "document_search" in result.selected_names


def test_empty_query_falls_back_to_full_disclosure():
    router = ToolRouter(FROZEN_TOOL_SCHEMAS, strategy="hybrid_topk", top_k=2)

    assert len(router.route("").definitions) == len(FROZEN_TOOL_SCHEMAS)


def test_allow_empty_abstains_for_context_only_and_unmatched_queries():
    router = ToolRouter(
        FROZEN_TOOL_SCHEMAS,
        strategy="hybrid_topk",
        top_k=3,
        allow_empty=True,
    )

    assert router.route("只依据上下文回答这个问题").definitions == ()
    assert router.route("中国的首都是哪座城市？").definitions == ()


def test_live_top2_covers_expected_tools_and_abstains_on_no_tool_cases():
    router = ToolRouter(
        LiveToolHarness().registry.get_definitions(),
        strategy="hybrid_topk",
        top_k=2,
        allow_empty=True,
    )
    tasks = load_agent_live_v1()

    assert all(
        set(task.expected_tools).issubset(router.route(task.prompt).selected_names)
        for task in tasks
    )
    no_tool = [task for task in tasks if task.suite in {"no_tool", "context"}]
    assert all(not router.route(task.prompt).selected_names for task in no_tool)


def test_offline_routing_board_has_full_recall_and_reduces_disclosure():
    board = run_tool_routing_board(strategy="hybrid_topk", top_k=3)

    assert board.recall >= 0.95
    assert board.precision >= 0.30
    assert board.disclosure_reduction >= 0.60


@pytest.mark.asyncio
async def test_runner_routes_disclosed_schemas_without_restricting_execution():
    provider = MagicMock()
    captured: list[dict] = []

    async def chat_with_retry(*, tools, **kwargs):
        captured.extend(tools)
        return LLMResponse(content="done", tool_calls=[])

    provider.chat_with_retry = chat_with_retry
    registry = MagicMock()
    registry.get_definitions.return_value = list(FROZEN_TOOL_SCHEMAS)
    registry.execute = AsyncMock()
    result = await AgentRunner().run(
        make_run_spec(
            provider,
            initial_messages=[{"role": "user", "content": "计算 21 乘以 2"}],
            tools=registry,
            model="test",
            max_iterations=1,
            max_tool_result_chars=1000,
            tool_routing=ToolRoutingConfig(enabled=True, top_k=2),
        )
    )

    disclosed = {
        item["function"]["name"]
        for item in captured
    }
    assert result.final_content == "done"
    assert "calculate" in disclosed
    assert len(disclosed) <= 2
    registry.execute.assert_not_awaited()
