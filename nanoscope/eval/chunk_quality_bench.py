"""LegalBench-RAG Mini 切片质量专项：固定字符级 gold，不依赖切片后动态判定。"""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from nanoscope.eval.artifacts import atomic_write_json, atomic_write_jsonl
from nanoscope.eval.legalbench_dataset import EvidenceSpan, LegalBenchCase, load_legalbench_mini
from nanoscope.rag import (
    StubReranker,
    chunk_by_structure_spans,
    chunk_fixed_window_spans,
)
from nanoscope.rag.sweep import HashingEmbedder


@dataclass(frozen=True)
class ChunkConfig:
    strategy: str
    size: int
    overlap: int = 0

    @property
    def label(self) -> str:
        return f"{self.strategy}:{self.size}:{self.overlap}"


DEFAULT_CHUNK_CONFIGS = (
    ChunkConfig("fixed", 400, 0),
    ChunkConfig("fixed", 400, 80),
    ChunkConfig("fixed", 800, 0),
    ChunkConfig("fixed", 800, 160),
    ChunkConfig("fixed", 1600, 160),
    ChunkConfig("structure", 800, 0),
)


@dataclass(frozen=True)
class IndexedChunk:
    file_path: str
    start: int
    end: int
    content: str

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class ChunkQualityCaseResult:
    case_id: str
    benchmark: str
    evidence_recall: float
    evidence_precision: float
    evidence_iou: float
    boundary_intrusion_rate: float
    complete_evidence_rate: float
    mean_min_cover_chunks: float
    retrieved_chunk_count: int
    retrieved_char_count: int
    retrieved_duplication_ratio: float


@dataclass
class ChunkQualityConfigResult:
    label: str
    strategy: str
    size: int
    overlap: int
    n_chunks: int
    corpus_duplication_ratio: float
    evidence_recall: float
    evidence_precision: float
    evidence_iou: float
    boundary_intrusion_rate: float
    complete_evidence_rate: float
    mean_min_cover_chunks: float
    mean_retrieved_char_count: float
    mean_retrieved_duplication_ratio: float
    by_benchmark: dict[str, dict[str, float]] = field(default_factory=dict)
    records: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        data = asdict(self)
        data.pop("records", None)
        return data


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def _merge_intervals(intervals: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if start < 0 or end <= start:
            raise ValueError(f"非法字符区间: {(start, end)!r}")
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _interval_length(intervals: Sequence[tuple[int, int]]) -> int:
    return sum(end - start for start, end in _merge_intervals(intervals))


def _intersection_length(
    left: Sequence[tuple[int, int]],
    right: Sequence[tuple[int, int]],
) -> int:
    a = _merge_intervals(left)
    b = _merge_intervals(right)
    i = j = total = 0
    while i < len(a) and j < len(b):
        total += max(0, min(a[i][1], b[j][1]) - max(a[i][0], b[j][0]))
        if a[i][1] <= b[j][1]:
            i += 1
        else:
            j += 1
    return total


def _document_interval_length(chunks: Sequence[IndexedChunk]) -> tuple[int, int]:
    raw = sum(chunk.length for chunk in chunks)
    by_file: dict[str, list[tuple[int, int]]] = {}
    for chunk in chunks:
        by_file.setdefault(chunk.file_path, []).append((chunk.start, chunk.end))
    unique = sum(_interval_length(intervals) for intervals in by_file.values())
    return raw, unique


def _split_document(file_path: str, text: str, config: ChunkConfig) -> list[IndexedChunk]:
    if config.strategy == "fixed":
        spans = chunk_fixed_window_spans(
            text,
            size=config.size,
            overlap=config.overlap,
        )
    elif config.strategy == "structure":
        if config.overlap != 0:
            raise ValueError("structure 策略暂不支持 overlap")
        spans = chunk_by_structure_spans(text, max_size=config.size)
    else:
        raise ValueError(f"未知 chunking 策略: {config.strategy!r}")
    return [
        IndexedChunk(
            file_path=file_path,
            start=span.start,
            end=span.end,
            content=span.content,
        )
        for span in spans
    ]


def _build_chunks(documents: dict[str, str], config: ChunkConfig) -> list[IndexedChunk]:
    chunks: list[IndexedChunk] = []
    for file_path, text in sorted(documents.items()):
        document_chunks = _split_document(file_path, text, config)
        for chunk in document_chunks:
            if text[chunk.start:chunk.end] != chunk.content:
                raise AssertionError(f"chunk 无法映射回原文: {file_path}:{chunk.start}-{chunk.end}")
        chunks.extend(document_chunks)
    return chunks


def _minimal_cover_count(span: EvidenceSpan, chunks: Sequence[IndexedChunk]) -> int:
    candidates = sorted(
        (
            (chunk.start, chunk.end)
            for chunk in chunks
            if chunk.file_path == span.file_path
            and chunk.end > span.start
            and chunk.start < span.end
        ),
        key=lambda item: (item[0], -item[1]),
    )
    cursor = span.start
    used = 0
    index = 0
    while cursor < span.end:
        furthest = cursor
        while index < len(candidates) and candidates[index][0] <= cursor:
            furthest = max(furthest, candidates[index][1])
            index += 1
        if furthest <= cursor:
            return 0
        used += 1
        cursor = furthest
    return used


def _case_result(
    case: LegalBenchCase,
    all_chunks: Sequence[IndexedChunk],
    retrieved: Sequence[IndexedChunk],
) -> ChunkQualityCaseResult:
    gold_by_file: dict[str, list[tuple[int, int]]] = {}
    for span in case.evidence:
        gold_by_file.setdefault(span.file_path, []).append((span.start, span.end))
    retrieved_by_file: dict[str, list[tuple[int, int]]] = {}
    for chunk in retrieved:
        retrieved_by_file.setdefault(chunk.file_path, []).append((chunk.start, chunk.end))

    gold_chars = sum(_interval_length(intervals) for intervals in gold_by_file.values())
    retrieved_chars = sum(
        _interval_length(intervals) for intervals in retrieved_by_file.values()
    )
    intersection = sum(
        _intersection_length(intervals, retrieved_by_file.get(file_path, ()))
        for file_path, intervals in gold_by_file.items()
    )
    union = gold_chars + retrieved_chars - intersection

    intrusion: list[float] = []
    complete: list[float] = []
    cover_counts: list[int] = []
    for span in case.evidence:
        document_chunks = [
            chunk for chunk in all_chunks if chunk.file_path == span.file_path
        ]
        boundaries = {
            boundary
            for chunk in document_chunks
            for boundary in (chunk.start, chunk.end)
            if span.start < boundary < span.end
        }
        intrusion.append(float(bool(boundaries)))
        complete.append(
            float(
                any(
                    chunk.start <= span.start and chunk.end >= span.end
                    for chunk in document_chunks
                )
            )
        )
        cover_counts.append(_minimal_cover_count(span, document_chunks))

    raw_retrieved = sum(chunk.length for chunk in retrieved)
    duplication = (
        (raw_retrieved - retrieved_chars) / raw_retrieved if raw_retrieved else 0.0
    )
    return ChunkQualityCaseResult(
        case_id=case.case_id,
        benchmark=case.benchmark,
        evidence_recall=intersection / gold_chars if gold_chars else 0.0,
        evidence_precision=intersection / retrieved_chars if retrieved_chars else 0.0,
        evidence_iou=intersection / union if union else 0.0,
        boundary_intrusion_rate=sum(intrusion) / len(intrusion),
        complete_evidence_rate=sum(complete) / len(complete),
        mean_min_cover_chunks=sum(cover_counts) / len(cover_counts),
        retrieved_chunk_count=len(retrieved),
        retrieved_char_count=retrieved_chars,
        retrieved_duplication_ratio=duplication,
    )


def _averages(records: Sequence[ChunkQualityCaseResult]) -> dict[str, float]:
    if not records:
        raise ValueError("无法聚合空 chunk quality 记录")
    fields = (
        "evidence_recall",
        "evidence_precision",
        "evidence_iou",
        "boundary_intrusion_rate",
        "complete_evidence_rate",
        "mean_min_cover_chunks",
        "retrieved_char_count",
        "retrieved_duplication_ratio",
    )
    return {
        name: sum(float(getattr(record, name)) for record in records) / len(records)
        for name in fields
    }


def run_chunk_config(
    dataset,
    config: ChunkConfig,
    *,
    embedder,
    reranker,
    top_k: int = 5,
    fanout: int = 20,
) -> ChunkQualityConfigResult:
    if top_k <= 0 or fanout < top_k:
        raise ValueError("必须满足 top_k > 0 且 fanout >= top_k")
    chunks = _build_chunks(dataset.documents, config)
    vectors = embedder.embed([chunk.content for chunk in chunks])
    if len(vectors) != len(chunks):
        raise ValueError("Embedder 返回的向量数量与 chunk 数不一致")

    records: list[ChunkQualityCaseResult] = []
    for case in dataset.cases:
        query_vector = embedder.embed([case.query])[0]
        ranked = sorted(
            range(len(chunks)),
            key=lambda index: (
                -_cosine(query_vector, vectors[index]),
                chunks[index].file_path,
                chunks[index].start,
            ),
        )
        candidate_indexes = ranked[:fanout]
        order = reranker.rerank(
            case.query,
            [chunks[index].content for index in candidate_indexes],
        )
        if sorted(order) != list(range(len(candidate_indexes))):
            raise ValueError("Reranker 必须返回候选下标的全排列")
        retrieved = [chunks[candidate_indexes[index]] for index in order[:top_k]]
        records.append(_case_result(case, chunks, retrieved))

    overall = _averages(records)
    by_benchmark = {
        benchmark: _averages(
            [record for record in records if record.benchmark == benchmark]
        )
        for benchmark in dataset.benchmarks
    }
    raw_corpus_chars, unique_corpus_chars = _document_interval_length(chunks)
    corpus_duplication = (
        (raw_corpus_chars - unique_corpus_chars) / raw_corpus_chars
        if raw_corpus_chars
        else 0.0
    )
    return ChunkQualityConfigResult(
        label=config.label,
        strategy=config.strategy,
        size=config.size,
        overlap=config.overlap,
        n_chunks=len(chunks),
        corpus_duplication_ratio=corpus_duplication,
        evidence_recall=overall["evidence_recall"],
        evidence_precision=overall["evidence_precision"],
        evidence_iou=overall["evidence_iou"],
        boundary_intrusion_rate=overall["boundary_intrusion_rate"],
        complete_evidence_rate=overall["complete_evidence_rate"],
        mean_min_cover_chunks=overall["mean_min_cover_chunks"],
        mean_retrieved_char_count=overall["retrieved_char_count"],
        mean_retrieved_duplication_ratio=overall["retrieved_duplication_ratio"],
        by_benchmark=by_benchmark,
        records=[asdict(record) for record in records],
    )


def _pareto_front(
    results: Sequence[ChunkQualityConfigResult],
) -> list[ChunkQualityConfigResult]:
    front: list[ChunkQualityConfigResult] = []
    for point in results:
        dominated = any(
            other is not point
            and other.evidence_recall >= point.evidence_recall
            and other.evidence_iou >= point.evidence_iou
            and other.complete_evidence_rate >= point.complete_evidence_rate
            and other.mean_retrieved_char_count <= point.mean_retrieved_char_count
            and (
                other.evidence_recall > point.evidence_recall
                or other.evidence_iou > point.evidence_iou
                or other.complete_evidence_rate > point.complete_evidence_rate
                or other.mean_retrieved_char_count < point.mean_retrieved_char_count
            )
            for other in results
        )
        if not dominated:
            front.append(point)
    return front


def run_chunk_quality_board(
    dataset,
    *,
    embedder=None,
    reranker=None,
    configs: Sequence[ChunkConfig] = DEFAULT_CHUNK_CONFIGS,
    top_k: int = 5,
    fanout: int = 20,
    level: str = "L0",
) -> dict:
    emb = embedder or HashingEmbedder(dim=128)
    ranker = reranker or StubReranker()
    results = [
        run_chunk_config(
            dataset,
            config,
            embedder=emb,
            reranker=ranker,
            top_k=top_k,
            fanout=fanout,
        )
        for config in configs
    ]
    front = _pareto_front(results)
    return {
        "dataset_version": dataset.version,
        "dataset_source_fingerprint": dataset.source_fingerprint,
        "benchmarks": list(dataset.benchmarks),
        "cases_per_benchmark": dataset.cases_per_benchmark,
        "n_cases": len(dataset.cases),
        "n_documents": len(dataset.documents),
        "level": level,
        "embedder": getattr(emb, "name", type(emb).__name__),
        "reranker": getattr(ranker, "name", type(ranker).__name__),
        "top_k": top_k,
        "fanout": fanout,
        "metric_unit": "unicode_codepoint",
        "configs": [result.summary() for result in results],
        "pareto_front": [result.label for result in front],
        "quality_gate": None,
        "quality_gate_reason": "v1 仅建立可复现基线，暂不设置无依据阈值",
        "records": [
            {"config": result.label, **record}
            for result in results
            for record in result.records
        ],
    }


def _level_components(
    level: str,
    *,
    requests_per_second: float,
    embedding_batch_size: int,
    embedding_cache: str | Path,
):
    if level == "L0":
        return HashingEmbedder(dim=128), StubReranker()
    from nanoscope.eval.embedding import GlmEmbedder, PersistentCachingEmbedder
    from nanoscope.eval.http_client import JsonHttpClient, RetryPolicy

    retry_policy = RetryPolicy(
        max_attempts=8,
        initial_backoff_s=2.0,
        max_backoff_s=60.0,
        jitter_ratio=0.1,
    )
    embedding_client = JsonHttpClient(
        timeout=60.0,
        requests_per_second=requests_per_second,
        retry_policy=retry_policy,
    )
    raw_embedder = GlmEmbedder(
        dimensions=256,
        batch_size=embedding_batch_size,
        timeout=60.0,
        requests_per_second=requests_per_second,
        http_client=embedding_client,
    )
    embedder = PersistentCachingEmbedder(
        raw_embedder,
        embedding_cache,
        write_batch_size=embedding_batch_size,
    )

    if level == "L1":
        return embedder, StubReranker()
    if level == "L2":
        from nanoscope.rag import SiliconFlowReranker

        rerank_client = JsonHttpClient(
            timeout=60.0,
            requests_per_second=requests_per_second,
            retry_policy=retry_policy,
        )
        return embedder, SiliconFlowReranker(
            timeout=60.0,
            requests_per_second=requests_per_second,
            http_client=rerank_client,
        )
    raise ValueError(f"未知 level: {level!r}")


def _parse_chunk_config(value: str) -> ChunkConfig:
    try:
        strategy, size, overlap = value.split(":")
        return ChunkConfig(strategy=strategy, size=int(size), overlap=int(overlap))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "config 必须使用 strategy:size:overlap，例如 fixed:800:160"
        ) from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, help="LegalBench-RAG 官方 data 目录")
    parser.add_argument("--cases-per-benchmark", type=int, default=25)
    parser.add_argument("--level", choices=("L0", "L1", "L2"), default="L0")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--fanout", type=int, default=20)
    parser.add_argument(
        "--config",
        action="append",
        type=_parse_chunk_config,
        help="可重复指定；默认运行内置六配置",
    )
    parser.add_argument("--requests-per-second", type=float, default=0.5)
    parser.add_argument("--embedding-batch-size", type=int, default=16)
    parser.add_argument(
        "--embedding-cache",
        default=".nanobot/cache/chunk_quality_embeddings.sqlite",
    )
    parser.add_argument(
        "--out",
        default="reports/baselines/CHUNK_QUALITY_LEGALBENCH_V1.json",
    )
    parser.add_argument(
        "--checkpoint",
        default="reports/checkpoints/chunk_quality/legalbenchrag-mini.v1.jsonl",
    )
    args = parser.parse_args(argv)

    dataset = load_legalbench_mini(
        args.data_dir,
        cases_per_benchmark=args.cases_per_benchmark,
    )
    embedder, reranker = _level_components(
        args.level,
        requests_per_second=args.requests_per_second,
        embedding_batch_size=args.embedding_batch_size,
        embedding_cache=args.embedding_cache,
    )
    board = run_chunk_quality_board(
        dataset,
        embedder=embedder,
        reranker=reranker,
        top_k=args.top_k,
        fanout=args.fanout,
        level=args.level,
        configs=tuple(args.config) if args.config else DEFAULT_CHUNK_CONFIGS,
    )
    records = board.pop("records")
    atomic_write_json(Path(args.out), board)
    atomic_write_jsonl(Path(args.checkpoint), records)
    print(f"summary: {args.out}")
    print(f"checkpoint: {args.checkpoint}")
    print(f"pareto_front: {', '.join(board['pareto_front'])}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
