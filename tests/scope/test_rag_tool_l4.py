from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.rag import DocumentSearchTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import Config
from nanobot.providers.base import GenerationSettings, LLMResponse, ToolCallRequest
from nanobot.utils.llm_runtime import LLMRuntime
from nanoscope.eval.rag_bench import build_benchmark
from nanoscope.rag import ChunkStore, PartitionedSearcher, StubReranker
from nanoscope.rag.sweep import HashingEmbedder


class RagToolProvider:
    generation = GenerationSettings(temperature=0.0, max_tokens=256)

    def __init__(self):
        self.calls = 0
        self.tool_content = ""

    async def chat_with_retry(self, *, messages, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="rag-call",
                        name="document_search",
                        arguments={"query": "B组机密项目预算3000万明细", "top_k": 5},
                    )
                ],
                finish_reason="tool_calls",
            )
        self.tool_content = next(
            message["content"] for message in messages if message.get("role") == "tool"
        )
        return LLMResponse(content=f"可见证据如下：{self.tool_content}")


class CountingEmbedder(HashingEmbedder):
    def __init__(self):
        super().__init__(dim=128)
        self.batch_sizes: list[int] = []

    def embed(self, texts):
        self.batch_sizes.append(len(texts))
        return super().embed(texts)


@pytest.fixture
def rag_tool(tmp_path: Path):
    store = ChunkStore(tmp_path / "rag.db")
    bench = build_benchmark(store)
    tool = DocumentSearchTool(
        store,
        embedder=HashingEmbedder(dim=128),
        reranker=StubReranker(),
    )
    yield tool, bench
    store.close()


def test_rag_tool_config_is_explicit_and_disabled_by_default():
    config = Config()

    assert config.tools.rag.enabled is False
    assert config.tools.rag.database_path == "~/.nanobot/rag.db"


@pytest.mark.asyncio
async def test_rag_tool_fails_closed_without_security_context(rag_tool):
    tool, _bench = rag_tool

    result = await tool.execute("预算")

    assert result.is_error is True
    assert "authenticated multi-user" in str(result)


@pytest.mark.asyncio
async def test_l4_runner_rag_tool_never_returns_forbidden_chunks(rag_tool):
    tool, bench = rag_tool
    registry = ToolRegistry()
    registry.register(tool)
    provider = RagToolProvider()
    runtime = LLMRuntime(
        provider=provider,
        model="rag-tool-l4",
        generation=provider.generation,
        context_window_tokens=4096,
    )
    context = RequestContext(
        channel="feishu",
        chat_id="alice",
        session_key="feishu:alice",
        tenant_id=bench.asker.tenant_id,
        principal_id=bench.asker.principal_id,
        audience_type=bench.asker.audience_type,
        roles=bench.asker.roles,
    )

    with request_context(context):
        result = await AgentRunner().run(
            AgentRunSpec(
                initial_messages=[{"role": "user", "content": "查询B组机密预算"}],
                tools=registry,
                runtime=runtime,
                max_iterations=3,
                max_tool_result_chars=20_000,
            )
        )

    assert result.stop_reason == "completed"
    assert result.tools_used == ["document_search"]
    assert "<memory>" in provider.tool_content
    assert "B组机密项目2025年预算为3000万元" not in provider.tool_content
    assert "B组机密项目2025年预算为3000万元" not in (result.final_content or "")
    assert all(str(forbidden_id) not in provider.tool_content for forbidden_id in bench.forbidden_ids)


@pytest.mark.asyncio
async def test_rag_tool_reuses_prebuilt_partitioned_index(tmp_path: Path):
    store = ChunkStore(tmp_path / "indexed.db")
    bench = build_benchmark(store)
    embedder = CountingEmbedder()
    searcher = PartitionedSearcher(store, embedder, use_ann=True)
    embedder.batch_sizes.clear()
    tool = DocumentSearchTool(
        store,
        embedder=embedder,
        reranker=StubReranker(),
        vector_searcher=searcher,
    )
    context = RequestContext(
        channel="feishu",
        chat_id="alice",
        session_key="feishu:alice",
        tenant_id=bench.asker.tenant_id,
        principal_id=bench.asker.principal_id,
        audience_type=bench.asker.audience_type,
        roles=bench.asker.roles,
    )

    with request_context(context):
        result = await tool.execute("量子加密预算")

    assert "<memory>" in str(result)
    assert embedder.batch_sizes == [1]
    store.close()
