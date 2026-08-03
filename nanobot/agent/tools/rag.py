"""Permission-aware document search tool backed by the NanoScope RAG store."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_context
from nanobot.config_base import Base
from nanoscope.identity import SecurityContext
from nanoscope.memory.sanitize import wrap_untrusted_memory
from nanoscope.rag import (
    ChunkStore,
    LinearFusionModel,
    PartitionedSearcher,
    PlattCalibrator,
    SiliconFlowReranker,
    StubReranker,
    doc_search_visible,
)
from nanoscope.rag.sweep import HashingEmbedder


class RagToolConfig(Base):
    enabled: bool = False
    database_path: str = "~/.nanobot/rag.db"
    embedder: Literal["hashing", "glm"] = "hashing"
    reranker: Literal["stub", "siliconflow"] = "stub"
    top_k: int = Field(default=5, ge=1, le=10)
    fusion_weights: list[float] | None = None
    fusion_bias: float = 0.0
    calibration_slope: float | None = None
    calibration_intercept: float = 0.0
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    vector_index: Literal["query", "partitioned", "hnsw"] = "query"
    hnsw_m: int = Field(default=16, ge=4, le=64)
    hnsw_ef_construction: int = Field(default=200, ge=16, le=1000)
    hnsw_ef_search: int = Field(default=50, ge=1, le=1000)


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 1000},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
)
class DocumentSearchTool(Tool):
    """Search only chunks visible to the authenticated request principal."""

    config_key = "rag"

    @classmethod
    def config_cls(cls):
        return RagToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.config.rag.enabled

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        config = ctx.config.rag
        if config.embedder == "glm":
            from nanoscope.eval.embedding import GlmEmbedder

            embedder = GlmEmbedder()
        else:
            embedder = HashingEmbedder(dim=128)
        reranker = (
            SiliconFlowReranker() if config.reranker == "siliconflow" else StubReranker()
        )
        fusion_model = (
            LinearFusionModel(tuple(config.fusion_weights), config.fusion_bias)
            if config.fusion_weights is not None
            else None
        )
        calibrator = (
            PlattCalibrator(config.calibration_slope, config.calibration_intercept)
            if config.calibration_slope is not None
            else None
        )
        store = ChunkStore(Path(config.database_path).expanduser())
        vector_searcher = (
            PartitionedSearcher(
                store,
                embedder,
                use_ann=config.vector_index == "hnsw",
                m=config.hnsw_m,
                ef_construction=config.hnsw_ef_construction,
                ef_search=config.hnsw_ef_search,
            )
            if config.vector_index != "query"
            else None
        )
        return cls(
            store,
            embedder=embedder,
            reranker=reranker,
            default_top_k=config.top_k,
            fusion_model=fusion_model,
            calibrator=calibrator,
            confidence_threshold=config.confidence_threshold,
            vector_searcher=vector_searcher,
        )

    def __init__(
        self,
        store: ChunkStore,
        *,
        embedder,
        reranker=None,
        default_top_k: int = 5,
        fusion_model: LinearFusionModel | None = None,
        calibrator: PlattCalibrator | None = None,
        confidence_threshold: float | None = None,
        vector_searcher: PartitionedSearcher | None = None,
    ):
        self.store = store
        self.embedder = embedder
        self.reranker = reranker or StubReranker()
        self.default_top_k = default_top_k
        self.fusion_model = fusion_model
        self.calibrator = calibrator
        self.confidence_threshold = confidence_threshold
        self.vector_searcher = vector_searcher

    @property
    def name(self) -> str:
        return "document_search"

    @property
    def description(self) -> str:
        return "Search document chunks authorized for the current authenticated user."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, query: str, top_k: int | None = None) -> Any:
        request = current_request_context()
        if request is None or not (
            request.tenant_id and request.principal_id and request.audience_type
        ):
            return ToolResult.error(
                "RAG search requires an authenticated multi-user security context"
            )
        security = SecurityContext(
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            session_key=request.session_key or "",
            audience_type=request.audience_type,
            audience_id=request.audience_id,
            roles=tuple(request.roles),
        )
        results = doc_search_visible(
            self.store,
            security,
            query,
            embedder=self.embedder,
            reranker=self.reranker,
            top_k=top_k or self.default_top_k,
            fusion_model=self.fusion_model,
            calibrator=self.calibrator,
            confidence_threshold=self.confidence_threshold,
            vector_searcher=self.vector_searcher,
        )
        if not results:
            return "No authorized document chunks found."
        return wrap_untrusted_memory((result.id, result.content) for result in results)
