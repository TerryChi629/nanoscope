"""摄取质量基准 ingest.v1：chunking 策略/参数网格 × 召回质量 × 隔离安全。

设计要点：
- 对每种 (strategy, size, overlap) 配置，用 `ingest_document` 真正切分并落库，再用
  `PreFilterSearcher`（授权 WHERE 在召回前）跑查询。
- **gold 动态判定**：属 proj_a 且 content 含该 query needle 的 chunk 即 gold。切分若把
  needle 事实句切断/稀释，Recall/nDCG/MRR 就会下降——这正是摄取质量的度量目标。
- **安全零容忍硬门禁**：任一配置只要检索结果命中 proj_b 越权 chunk（forbidden_exposure>0），
  该专项整体 FAIL（fail-closed）。质量维度只做帕累托对照，不设拍脑袋硬阈值。
- 全离线确定性（HashingEmbedder）；真 GLM 端到端由单测在有 GLM_API_KEY 时单独覆盖。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from nanoscope.eval.artifacts import atomic_write_json, atomic_write_jsonl
from nanoscope.eval.ingest_dataset import (
    INGEST_DATASET_VERSION,
    IngestQuery,
    forbidden_documents,
    load_ingest_queries,
    org_documents,
    visible_documents,
)
from nanoscope.eval.metrics import mrr, ndcg_at_k, recall_at_k
from nanoscope.identity import AUDIENCE_DM, SecurityContext
from nanoscope.rag import ChunkStore, PreFilterSearcher, forbidden_doc_exposure, ingest_document
from nanoscope.rag.sweep import HashingEmbedder

# 摄取质量网格：固定窗口的 size×overlap + 结构感知。overlap 必须 < size（fail-closed）。
INGEST_GRID: tuple[dict, ...] = (
    {"strategy": "fixed", "size": 100, "overlap": 0},
    {"strategy": "fixed", "size": 100, "overlap": 40},
    {"strategy": "fixed", "size": 200, "overlap": 0},
    {"strategy": "fixed", "size": 200, "overlap": 40},
    {"strategy": "fixed", "size": 400, "overlap": 80},
    {"strategy": "structure", "size": 400, "overlap": 0},
)

_TOP_K = 5


@dataclass(frozen=True)
class IngestConfigResult:
    """一种 chunking 配置在整组查询上的聚合结果。"""

    strategy: str
    size: int
    overlap: int
    n_queries: int
    n_visible_chunks: int
    recall_at_k: float
    ndcg_at_k: float
    mrr: float
    forbidden_exposure: int
    top_k: int


def _asker_ctx() -> SecurityContext:
    """proj_a 成员（DM 语境），只应看到 proj_a + org。"""
    return SecurityContext(
        tenant_id="orgX",
        principal_id="orgX:feishu:asker",
        session_key="feishu:asker",
        audience_type=AUDIENCE_DM,
        roles=("proj_a",),
    )


def _admin_ctx() -> SecurityContext:
    """摄取用管理员上下文（可写入 proj_a/proj_b/org）。"""
    return SecurityContext(
        tenant_id="orgX",
        principal_id="orgX:feishu:admin",
        session_key="feishu:admin",
        audience_type=AUDIENCE_DM,
        roles=("proj_a", "proj_b"),
    )


def _seed_config(store: ChunkStore, admin: SecurityContext, config: dict) -> None:
    """按给定 chunking 配置把三类文档切分落库。"""
    strategy = config["strategy"]
    size = config["size"]
    overlap = config["overlap"]
    for doc in visible_documents() + forbidden_documents():
        ingest_document(
            store, admin, title=doc.title, text=doc.text,
            scope=doc.scope, acl_group=doc.acl_group,
            strategy=strategy, size=size, overlap=overlap,
        )
    for doc in org_documents():
        ingest_document(
            store, admin, title=doc.title, text=doc.text,
            scope=doc.scope, acl_group=doc.acl_group,
            strategy=strategy, size=size, overlap=overlap,
        )


def _gold_ids(store: ChunkStore, asker: SecurityContext, needle: str) -> set[str]:
    """可见集合内 content 含 needle 的 chunk 即该 query 的 gold（动态判定）。"""
    return {c.id for c in store.visible_chunks(asker) if needle in c.content}


def _forbidden_ids(store: ChunkStore) -> set[str]:
    """全库中属 proj_b 的 chunk id（越权靶点，任何配置下都不得出现在结果里）。"""
    return {c.id for c in store.all_chunks_unfiltered() if c.acl_group == "proj_b"}


def run_ingest_config(
    config: dict,
    queries: list[IngestQuery],
    *,
    embedder,
    top_k: int = _TOP_K,
) -> IngestConfigResult:
    """在一个临时内存库上按 config 摄取并跑完整组查询。"""
    admin = _admin_ctx()
    asker = _asker_ctx()
    store = ChunkStore(Path(":memory:"))
    try:
        _seed_config(store, admin, config)
        forbidden = _forbidden_ids(store)
        n_visible = len(store.visible_chunks(asker))
        searcher = PreFilterSearcher(store, embedder)
        recalls: list[float] = []
        ndcgs: list[float] = []
        mrrs: list[float] = []
        exposure = 0
        for q in queries:
            gold = _gold_ids(store, asker, q.needle)
            outcome = searcher.search(asker, q.query, top_k=top_k)
            ranked = [c.id for c in outcome.results]
            recalls.append(recall_at_k(ranked, gold, top_k))
            ndcgs.append(ndcg_at_k(ranked, gold, top_k))
            mrrs.append(mrr(ranked, gold))
            exposure += forbidden_doc_exposure(outcome.results, forbidden)
    finally:
        store.close()
    n = max(len(queries), 1)
    return IngestConfigResult(
        strategy=config["strategy"],
        size=config["size"],
        overlap=config["overlap"],
        n_queries=len(queries),
        n_visible_chunks=n_visible,
        recall_at_k=sum(recalls) / n,
        ndcg_at_k=sum(ndcgs) / n,
        mrr=sum(mrrs) / n,
        forbidden_exposure=exposure,
        top_k=top_k,
    )


def pareto_front(results: list[IngestConfigResult]) -> list[IngestConfigResult]:
    """质量最大化 + chunk 数最小化（存储代理）的帕累托前沿。

    维度：recall_at_k↑、ndcg_at_k↑、n_visible_chunks↓（越少 chunk 达到同等质量越优）。
    """
    front: list[IngestConfigResult] = []
    for p in results:
        dominated = any(
            q is not p
            and q.recall_at_k >= p.recall_at_k
            and q.ndcg_at_k >= p.ndcg_at_k
            and q.n_visible_chunks <= p.n_visible_chunks
            and (
                q.recall_at_k > p.recall_at_k
                or q.ndcg_at_k > p.ndcg_at_k
                or q.n_visible_chunks < p.n_visible_chunks
            )
            for q in results
        )
        if not dominated:
            front.append(p)
    return front


def run_ingest_board(*, embedder=None, top_k: int = _TOP_K) -> dict:
    """跑完整网格，聚合成一份可落盘的 board（含安全门禁）。"""
    emb = embedder if embedder is not None else HashingEmbedder(dim=128)
    queries = load_ingest_queries()
    results = [run_ingest_config(cfg, queries, embedder=emb, top_k=top_k) for cfg in INGEST_GRID]
    total_exposure = sum(r.forbidden_exposure for r in results)
    security_gate_pass = total_exposure == 0
    front = pareto_front(results)
    best = max(results, key=lambda r: (r.recall_at_k, r.ndcg_at_k, -r.n_visible_chunks))
    return {
        "dataset_version": INGEST_DATASET_VERSION,
        "top_k": top_k,
        "n_configs": len(results),
        "n_queries": len(queries),
        "configs": [vars(r) for r in results],
        "pareto_front": [
            {"strategy": r.strategy, "size": r.size, "overlap": r.overlap,
             "recall_at_k": r.recall_at_k, "ndcg_at_k": r.ndcg_at_k,
             "n_visible_chunks": r.n_visible_chunks}
            for r in front
        ],
        "best_quality_config": {
            "strategy": best.strategy, "size": best.size, "overlap": best.overlap,
            "recall_at_k": best.recall_at_k, "ndcg_at_k": best.ndcg_at_k,
        },
        "total_forbidden_exposure": total_exposure,
        "security_gate_pass": security_gate_pass,
        "gate": {"security": security_gate_pass, "overall": security_gate_pass},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="reports/baselines")
    parser.add_argument("--checkpoint-dir", default="reports/checkpoints/ingest")
    parser.add_argument("--top-k", type=int, default=_TOP_K)
    args = parser.parse_args()
    board = run_ingest_board(top_k=args.top_k)
    out_dir = Path(args.out_dir)
    summary_path = out_dir / "RAG_INGEST_V1.json"
    atomic_write_json(summary_path, board)
    checkpoint_path = Path(args.checkpoint_dir) / f"{INGEST_DATASET_VERSION}_k{args.top_k}.jsonl"
    atomic_write_jsonl(
        checkpoint_path,
        [{"_type": "ingest_config", **config} for config in board["configs"]],
    )
    print(f"summary: {summary_path}")
    print(f"checkpoint: {checkpoint_path}")
    print(
        f"security_gate={board['security_gate_pass']} "
        f"best={board['best_quality_config']}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
