from __future__ import annotations

import json

import pytest

from nanoscope.eval.chunk_quality_bench import (
    ChunkConfig,
    run_chunk_config,
    run_chunk_quality_board,
)
from nanoscope.eval.embedding import PersistentCachingEmbedder
from nanoscope.eval.legalbench_dataset import load_legalbench_mini
from nanoscope.rag import (
    StubReranker,
    chunk_by_structure,
    chunk_by_structure_spans,
    chunk_fixed_window,
    chunk_fixed_window_spans,
)


class _KeywordEmbedder:
    name = "keyword"

    def embed(self, texts):
        return [
            [1.0, 0.0] if "TARGET" in text else [0.0, 1.0]
            for text in texts
        ]


def _write_legalbench_fixture(tmp_path, *, n_cases: int = 2):
    data_dir = tmp_path / "data"
    corpus_dir = data_dir / "corpus" / "contractnli"
    benchmark_dir = data_dir / "benchmarks"
    corpus_dir.mkdir(parents=True)
    benchmark_dir.mkdir(parents=True)
    text = "prefix xxxx\n\nTARGET EVIDENCE COMPLETE.\n\nsuffix yyyy"
    document = corpus_dir / "sample.txt"
    document.write_text(text, encoding="utf-8")
    start = text.index("TARGET")
    end = start + len("TARGET EVIDENCE COMPLETE.")
    tests = [
        {
            "query": f"TARGET query {index}",
            "snippets": [
                {
                    "file_path": "contractnli/sample.txt",
                    "span": [start, end],
                    "answer": text[start:end],
                }
            ],
        }
        for index in range(n_cases)
    ]
    (benchmark_dir / "contractnli.json").write_text(
        json.dumps({"tests": tests}),
        encoding="utf-8",
    )
    return data_dir, text


def test_span_chunkers_preserve_existing_content_contract():
    text = "  alpha beta\n\nsecond paragraph is longer  "

    fixed = chunk_fixed_window_spans(text, size=8, overlap=2)
    structured = chunk_by_structure_spans(text, max_size=10)

    assert [chunk.content for chunk in fixed] == chunk_fixed_window(
        text, size=8, overlap=2
    )
    assert [chunk.content for chunk in structured] == chunk_by_structure(
        text, max_size=10
    )
    assert all(text[chunk.start:chunk.end] == chunk.content for chunk in fixed)
    assert all(text[chunk.start:chunk.end] == chunk.content for chunk in structured)


def test_legalbench_mini_selection_is_deterministic_and_validates_answers(tmp_path):
    data_dir, _text = _write_legalbench_fixture(tmp_path)

    first = load_legalbench_mini(
        data_dir,
        benchmarks=("contractnli",),
        cases_per_benchmark=2,
    )
    second = load_legalbench_mini(
        data_dir,
        benchmarks=("contractnli",),
        cases_per_benchmark=2,
    )

    assert first.source_fingerprint == second.source_fingerprint
    assert [case.case_id for case in first.cases] == [
        case.case_id for case in second.cases
    ]
    assert len(first.documents) == 1
    assert len(first.cases) == 2

    benchmark = data_dir / "benchmarks" / "contractnli.json"
    payload = json.loads(benchmark.read_text(encoding="utf-8"))
    payload["tests"][0]["snippets"][0]["answer"] = "wrong"
    benchmark.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="gold answer 与原文区间不一致"):
        load_legalbench_mini(
            data_dir,
            benchmarks=("contractnli",),
            cases_per_benchmark=2,
        )


def test_complete_evidence_and_boundary_metrics_distinguish_chunkers(tmp_path):
    data_dir, _text = _write_legalbench_fixture(tmp_path)
    dataset = load_legalbench_mini(
        data_dir,
        benchmarks=("contractnli",),
        cases_per_benchmark=2,
    )
    embedder = _KeywordEmbedder()
    reranker = StubReranker()

    fragmented = run_chunk_config(
        dataset,
        ChunkConfig("fixed", 10, 0),
        embedder=embedder,
        reranker=reranker,
        top_k=3,
        fanout=5,
    )
    structured = run_chunk_config(
        dataset,
        ChunkConfig("structure", 100, 0),
        embedder=embedder,
        reranker=reranker,
        top_k=3,
        fanout=3,
    )

    assert fragmented.complete_evidence_rate == 0.0
    assert fragmented.boundary_intrusion_rate == 1.0
    assert fragmented.mean_min_cover_chunks > 1.0
    assert structured.complete_evidence_rate == 1.0
    assert structured.boundary_intrusion_rate == 0.0
    assert structured.mean_min_cover_chunks == 1.0


def test_overlap_metrics_are_union_based_and_board_needs_no_api_key(tmp_path):
    data_dir, _text = _write_legalbench_fixture(tmp_path)
    dataset = load_legalbench_mini(
        data_dir,
        benchmarks=("contractnli",),
        cases_per_benchmark=2,
    )
    board = run_chunk_quality_board(
        dataset,
        embedder=_KeywordEmbedder(),
        reranker=StubReranker(),
        configs=(ChunkConfig("fixed", 20, 10),),
        top_k=3,
        fanout=5,
    )

    result = board["configs"][0]
    assert 0.0 <= result["evidence_recall"] <= 1.0
    assert 0.0 <= result["evidence_precision"] <= 1.0
    assert 0.0 <= result["evidence_iou"] <= 1.0
    assert result["corpus_duplication_ratio"] > 0.0
    assert board["quality_gate"] is None


def test_persistent_embedding_cache_resumes_without_storing_source_text(tmp_path):
    class _CountingEmbedder:
        name = "counting"
        model = "fake-v1"
        dimensions = 2
        base_url = "offline"

        def __init__(self):
            self.calls = []

        def embed(self, texts):
            self.calls.append(list(texts))
            return [[float(len(text)), 1.0] for text in texts]

    cache_path = tmp_path / "embeddings.sqlite"
    first_delegate = _CountingEmbedder()
    first = PersistentCachingEmbedder(
        first_delegate,
        cache_path,
        write_batch_size=2,
    )
    expected = first.embed(["secret legal text", "other clause", "secret legal text"])
    first.close()

    second_delegate = _CountingEmbedder()
    second = PersistentCachingEmbedder(
        second_delegate,
        cache_path,
        write_batch_size=2,
    )
    actual = second.embed(["secret legal text", "other clause"])
    second.close()

    assert expected[:2] == actual
    assert first_delegate.calls == [["secret legal text", "other clause"]]
    assert second_delegate.calls == []
    assert b"secret legal text" not in cache_path.read_bytes()
