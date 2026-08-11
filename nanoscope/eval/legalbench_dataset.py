"""LegalBench-RAG 字符级证据数据适配与确定性 Mini 抽样。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

LEGALBENCH_MINI_VERSION = "legalbenchrag-mini.v1"
DEFAULT_BENCHMARKS = ("contractnli", "cuad", "maud", "privacy_qa")


@dataclass(frozen=True)
class EvidenceSpan:
    """LegalBench-RAG 原文中的 gold evidence，区间采用左闭右开字符下标。"""

    file_path: str
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class LegalBenchCase:
    case_id: str
    benchmark: str
    query: str
    evidence: tuple[EvidenceSpan, ...]


@dataclass(frozen=True)
class LegalBenchMini:
    version: str
    source_fingerprint: str
    documents: dict[str, str]
    cases: tuple[LegalBenchCase, ...]
    benchmarks: tuple[str, ...]
    cases_per_benchmark: int


def _safe_corpus_path(corpus_dir: Path, file_path: str) -> Path:
    relative = PurePosixPath(file_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"非法 LegalBench-RAG 文档路径: {file_path!r}")
    target = (corpus_dir / Path(*relative.parts)).resolve()
    root = corpus_dir.resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"LegalBench-RAG 文档路径越界: {file_path!r}")
    if not target.is_file():
        raise FileNotFoundError(f"LegalBench-RAG 文档不存在: {target}")
    return target


def _case_sort_key(benchmark: str, raw_case: dict[str, Any]) -> str:
    material = json.dumps(
        {
            "benchmark": benchmark,
            "query": raw_case.get("query"),
            "snippets": [
                {
                    "file_path": item.get("file_path"),
                    "span": item.get("span"),
                }
                for item in raw_case.get("snippets", [])
                if isinstance(item, dict)
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _load_raw_cases(benchmark_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(benchmark_path.read_text(encoding="utf-8"))
    tests = payload.get("tests") if isinstance(payload, dict) else None
    if not isinstance(tests, list):
        raise ValueError(f"LegalBench-RAG benchmark 缺少 tests 数组: {benchmark_path}")
    if not all(isinstance(item, dict) for item in tests):
        raise ValueError(f"LegalBench-RAG tests 必须全部是对象: {benchmark_path}")
    return tests


def _parse_case(
    *,
    benchmark: str,
    raw_case: dict[str, Any],
    corpus_dir: Path,
    document_cache: dict[str, str],
) -> LegalBenchCase:
    query = raw_case.get("query")
    snippets = raw_case.get("snippets")
    if not isinstance(query, str) or not query.strip():
        raise ValueError(f"{benchmark} 存在空 query")
    if not isinstance(snippets, list) or not snippets:
        raise ValueError(f"{benchmark} query 缺少 gold snippets: {query!r}")

    evidence: list[EvidenceSpan] = []
    for raw_snippet in snippets:
        if not isinstance(raw_snippet, dict):
            raise ValueError(f"{benchmark} snippet 必须是对象")
        file_path = raw_snippet.get("file_path")
        span = raw_snippet.get("span")
        if (
            not isinstance(file_path, str)
            or not isinstance(span, list)
            or len(span) != 2
            or not all(isinstance(value, int) for value in span)
        ):
            raise ValueError(f"{benchmark} snippet 字段非法: {raw_snippet!r}")
        if file_path not in document_cache:
            target = _safe_corpus_path(corpus_dir, file_path)
            document_cache[file_path] = target.read_text(encoding="utf-8")
        text = document_cache[file_path]
        start, end = span
        if start < 0 or end <= start or end > len(text):
            raise ValueError(
                f"{benchmark} gold span 越界: {file_path}:{start}-{end}/{len(text)}"
            )
        declared_answer = raw_snippet.get("answer")
        if isinstance(declared_answer, str) and text[start:end] != declared_answer:
            raise ValueError(f"{benchmark} gold answer 与原文区间不一致: {file_path}")
        evidence.append(EvidenceSpan(file_path=file_path, start=start, end=end))

    identity = _case_sort_key(benchmark, raw_case)[:16]
    return LegalBenchCase(
        case_id=f"{benchmark}:{identity}",
        benchmark=benchmark,
        query=query,
        evidence=tuple(evidence),
    )


def _fingerprint(documents: dict[str, str], cases: tuple[LegalBenchCase, ...]) -> str:
    digest = hashlib.sha256()
    for file_path in sorted(documents):
        digest.update(file_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(documents[file_path].encode("utf-8"))
        digest.update(b"\0")
    for case in cases:
        digest.update(case.case_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(case.query.encode("utf-8"))
        for span in case.evidence:
            digest.update(f"{span.file_path}:{span.start}:{span.end}".encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()


def load_legalbench_mini(
    data_dir: str | Path,
    *,
    benchmarks: tuple[str, ...] = DEFAULT_BENCHMARKS,
    cases_per_benchmark: int = 25,
) -> LegalBenchMini:
    """从官方 data 目录稳定抽取均衡 Mini；不复制或改写上游语料。"""
    if cases_per_benchmark <= 0:
        raise ValueError("cases_per_benchmark 必须为正")
    if not benchmarks or len(set(benchmarks)) != len(benchmarks):
        raise ValueError("benchmarks 必须是非空且不重复的名称序列")

    root = Path(data_dir)
    corpus_dir = root / "corpus"
    benchmark_dir = root / "benchmarks"
    if not corpus_dir.is_dir() or not benchmark_dir.is_dir():
        raise FileNotFoundError(
            "LegalBench-RAG data_dir 必须包含 corpus/ 与 benchmarks/ 目录"
        )

    document_cache: dict[str, str] = {}
    cases: list[LegalBenchCase] = []
    for benchmark in benchmarks:
        if not benchmark.replace("_", "").isalnum():
            raise ValueError(f"非法 benchmark 名称: {benchmark!r}")
        benchmark_path = benchmark_dir / f"{benchmark}.json"
        raw_cases = _load_raw_cases(benchmark_path)
        selected = sorted(
            raw_cases,
            key=lambda item: _case_sort_key(benchmark, item),
        )[:cases_per_benchmark]
        if len(selected) < cases_per_benchmark:
            raise ValueError(
                f"{benchmark} 只有 {len(selected)} 条，少于要求的 {cases_per_benchmark} 条"
            )
        cases.extend(
            _parse_case(
                benchmark=benchmark,
                raw_case=raw_case,
                corpus_dir=corpus_dir,
                document_cache=document_cache,
            )
            for raw_case in selected
        )

    frozen_cases = tuple(cases)
    return LegalBenchMini(
        version=LEGALBENCH_MINI_VERSION,
        source_fingerprint=_fingerprint(document_cache, frozen_cases),
        documents=dict(sorted(document_cache.items())),
        cases=frozen_cases,
        benchmarks=benchmarks,
        cases_per_benchmark=cases_per_benchmark,
    )
