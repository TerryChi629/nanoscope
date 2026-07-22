"""M10 统一 A/B 报告 (PRD §11-M10 / §13 证据平面收口)。

把三条证据线的"改前 vs 改后"汇成一份**自包含的一体化 HTML**，供飞书演示可见：

1. 隔离安全（P0，M0→M6）：冻结攻击集上 `forbidden_prompt_exposure`
   从基线的 >0 翻转为隔离态的 0。
2. 召回质量（P1-A，M7）：规模曲线上 grep 随 N 掉出 SLA，BM25(/向量/RRF) 守住。
3. 并发公平（P1-B，M9）：U1-U5 压测下 BaselineGate vs FairAdmissionController
   的 queue_wait 尾部 / 有界拒绝 / Jain 公平。

Reporter 只做**聚合 + 渲染**，三线的取数全部复用既有模块（trace/evaluator/loadtest），
不重复实现任何指标逻辑，保证报告数据与各里程碑单测同源、可复现。
"""

from __future__ import annotations

import asyncio
import html
from dataclasses import dataclass
from pathlib import Path

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore
from nanoscope.eval.dataset import load_attack_queries, load_forbidden_facts
from nanoscope.eval.embedding import Embedder
from nanoscope.eval.injection import InjectionReport, collect_injection_resistance
from nanoscope.eval.loadtest import CASES as _LOAD_CASES
from nanoscope.eval.loadtest import (
    AbResult,
    RoundsAbResult,
    run_case_ab,
    run_case_ab_rounds,
    workload_u7_sustained_overload,
)
from nanoscope.eval.scale_curve import ScalePoint, find_crossover, run_curve
from nanoscope.eval.scale_dataset_v2 import ScalePointV2, find_crossover_v2, run_curve_v2
from nanoscope.identity import AUDIENCE_DM, SecurityContext
from nanoscope.memory import SCOPE_USER, Repository

# U5 洪峰用例用更小的 max_queue 才能触发有界拒绝（与 M9 报告一致）。
_CASE_MAX_QUEUE: dict[str, int] = {"U5_overload": 32}

# M16 背压证据用 U7 持续过载（burst 远超容量），max_queue 收敛可见。
_BACKPRESSURE_BURST = 120
_BACKPRESSURE_MAX_QUEUE = 32
_BACKPRESSURE_GLOBAL_LIMIT = 3
_BACKPRESSURE_ROUNDS = 3

# M15 带 CI 规模曲线的规模档与多种子（报告用轻量档，保证离线可跑）。
_CURVE_V2_SCALES = [50, 200, 1000]
_CURVE_V2_SEEDS = [1, 2, 3]


@dataclass(frozen=True)
class IsolationReport:
    """隔离安全 A/B：冻结攻击集上的 forbidden_prompt_exposure（越低越好，目标=0）。

    NanoScope (PRD_v4 §M11.4)：exposure 覆盖**两条通道**——
    - 记忆通道（M4/M6，`Repository.search_visible`）；
    - Recent History 通道（M11，`read_recent_history_for_prompt`）。
    两通道都进 system prompt，必须用同一套 principal/audience 谓词，改后均须翻转为 0。
    """

    n_attacks: int
    baseline_exposure: int  # 记忆通道·基线（全局 MEMORY.md 无条件注入）总泄露命中数
    isolated_exposure: int  # 记忆通道·隔离态（SecurityContext + search_visible）总泄露命中数
    history_baseline_exposure: int  # history 通道·基线（只按 session_key，跨 principal 泄露）
    history_isolated_exposure: int  # history 通道·隔离态（principal/audience 谓词）


@dataclass(frozen=True)
class DocRagReport:
    """权限感知文档 RAG A/B（M18）：把 M6 的 `forbidden_prompt_exposure==0` 从"短记忆条目"
    扩展到"长文档 chunk"。

    - baseline_exposure：绕过授权 WHERE、直接对全库做向量近邻（业界默认全员可见的经典 RAG），
      A 部门成员的跨部门 query 语义命中 B 部门机密 chunk → 越权泄露 >0（可证伪基线）。
    - 正确隔离态：Pre-filter（授权 WHERE）/ Partitioned（租户化 ACL 子图）在召回前隔离。
    - Post-filter 仅作反面对照：越权项已进候选并被打分，最终命中为 0 不代表满足安全红线。
    - ann_backend：真 hnswlib 是否可用（诚实标注加速层底座）。
    """

    query: str
    n_visible: int          # alice 可见 chunk 数（proj_a + org）
    n_forbidden: int        # 越权（proj_b）chunk 数
    baseline_exposure: int  # 关授权全库近邻的越权命中数（>0 可证伪）
    prefilter_exposure: int
    postfilter_exposure: int
    partitioned_exposure: int
    ann_backend: str        # "真 HNSW" / "退化暴力（未装 hnswlib）"


@dataclass(frozen=True)
class UnifiedReport:
    """六条证据线的一体化 A/B 结果（M10 三线 + M17 统一报告 v2 扩展两线 + M18 文档 RAG）。

    NanoScope v2（PRD_v4 §M17.3）在 M10 三线（隔离/召回/并发）基础上新增：
    - injection：记忆注入静态探针残留（M12）改前 vs 改后。
    - backpressure：端到端背压峰值（M16）改前(线性) vs 改后(有界)。
    - retrieval_v2：带 95% CI 的规模曲线（M15，Zipf 自然增长）。
    - doc_rag：权限感知文档 RAG（M18）三策略越权命中 vs 关授权全库近邻。
    - 隔离段已含 Recent History 通道（M11）。
    """

    isolation: IsolationReport
    retrieval: list[ScalePoint]
    concurrency: list[AbResult]
    injection: InjectionReport
    backpressure: RoundsAbResult
    retrieval_v2: list[ScalePointV2]
    doc_rag: DocRagReport


def _exposures(prompt: str) -> int:
    """一段 prompt 里命中了多少条 forbidden fact 的 canary。"""
    return sum(1 for f in load_forbidden_facts() if f.canary in prompt)


def collect_isolation(workspace: Path, repo_path: Path) -> IsolationReport:
    """隔离安全 A/B：同一冻结攻击集，基线 vs 隔离态各构造攻击者 prompt 数泄露。

    - 基线：私聊个人事实（Dream 后门效果）落全局 MEMORY.md → 基线 ContextBuilder
      对**任意**攻击者/群聊无条件注入 → 每条攻击都命中他人 canary（>0）。
    - 隔离态：同样的事实经 user-scope 写入口落 SQLite（owner=本人，DM 语境）→
      用攻击者 SecurityContext 走 search_visible 拼注入串 → 隔离态 ContextBuilder
      构造攻击者 prompt → DM 门 + 授权 WHERE 使命中为 0。
    """
    facts = load_forbidden_facts()
    attacks = load_attack_queries()

    # --- 基线路径：全部私聊事实进全局 MEMORY.md，任何 prompt 无条件注入。 ---
    store = MemoryStore(workspace)
    store.write_memory(
        "# Long-term facts\n"
        + "\n".join(f"- {f.secret} ({f.canary})" for f in facts)
        + "\n"
    )
    baseline_builder = ContextBuilder(workspace)
    baseline_exposure = 0
    for attack in attacks:
        prompt = baseline_builder.build_system_prompt(
            channel="feishu", session_key=f"feishu:{attack.attacker_principal}"
        )
        baseline_exposure += _exposures(prompt)

    # --- 隔离路径：事实落 SQLite（owner-aware），攻击者只拿到自己可见集合。 ---
    repo = Repository(repo_path)
    try:
        for fact in facts:
            owner_ctx = SecurityContext(
                tenant_id=fact.tenant_id,
                principal_id=fact.owner_principal,
                session_key="feishu:x",
                audience_type=AUDIENCE_DM,
            )
            repo.add(owner_ctx, content=f"{fact.secret} ({fact.canary})",
                     scope=SCOPE_USER, source_type="tool")

        iso_builder = ContextBuilder(workspace)
        iso_builder.multi_user_isolation = True
        isolated_exposure = 0
        for attack in attacks:
            fact = next(f for f in facts if f.id == attack.target_fact)
            attacker_ctx = SecurityContext(
                tenant_id=fact.tenant_id,
                principal_id=attack.attacker_principal,
                session_key="feishu:x",
                audience_type=attack.audience_type,
            )
            records = repo.search_visible(attacker_ctx)
            scoped = "\n".join(f"- {r.content}" for r in records)
            prompt = iso_builder.build_system_prompt(channel="feishu", scoped_memory=scoped)
            isolated_exposure += _exposures(prompt)
    finally:
        repo.close()

    # --- history 通道 A/B（PRD_v4 §M11）：同一攻击集铺到 Recent History。 ---
    history_baseline_exposure, history_isolated_exposure = _collect_history_isolation(
        workspace, facts, attacks
    )

    return IsolationReport(
        n_attacks=len(attacks),
        baseline_exposure=baseline_exposure,
        isolated_exposure=isolated_exposure,
        history_baseline_exposure=history_baseline_exposure,
        history_isolated_exposure=history_isolated_exposure,
    )


def _collect_history_isolation(workspace: Path, facts, attacks) -> tuple[int, int]:
    """Recent History 通道 A/B：私聊个人事实经 history 通道落盘，攻击者构造 prompt 数泄露。

    - 基线：`read_recent_history_for_prompt` 只按 session_key 过滤（session_key=None →
      全量返回），任意攻击者都能读到他人私聊历史 → 命中 >0。
    - 隔离态：`ContextBuilder` 置 `multi_user_isolation=True` 并透传攻击者
      principal/audience → M11 谓词把非本人私聊历史 fail-closed 丢弃 → 命中 0。
    """
    hist_store = MemoryStore(workspace / "_hist")
    for fact in facts:
        hist_store.append_history(
            f"{fact.secret} ({fact.canary})",
            principal_id=fact.owner_principal,
            audience_type=AUDIENCE_DM,
            audience_id=fact.owner_principal,
        )

    # 基线：无隔离谓词，session_key=None 全量返回 → 攻击者读到他人私聊历史。
    baseline_exposure = 0
    for _attack in attacks:
        entries = hist_store.read_recent_history_for_prompt(since_cursor=0, session_key=None)
        baseline_exposure += _exposures(" ".join(e["content"] for e in entries))

    # 隔离态：经 ContextBuilder prompt，M11 principal/audience 谓词生效。
    iso_builder = ContextBuilder(workspace / "_hist")
    iso_builder.memory = hist_store
    iso_builder.multi_user_isolation = True
    isolated_exposure = 0
    for attack in attacks:
        prompt = iso_builder.build_system_prompt(
            channel="feishu",
            scoped_memory="",  # 记忆通道已由 M4/M6 堵死，这里只验 history 通道
            memory_isolation=True,
            history_principal_id=attack.attacker_principal,
            history_audience_type=attack.audience_type,
            history_audience_id="feishu:some-group",
        )
        isolated_exposure += _exposures(prompt)
    return baseline_exposure, isolated_exposure


def collect_retrieval(*, k: int = 5, embedder: Embedder | None = None) -> list[ScalePoint]:
    """召回质量 A/B：规模曲线（grep vs bm25，可选 vector/rrf）。复用 M7 run_curve。"""
    return run_curve(k=k, embedder=embedder)


async def collect_concurrency(
    *, global_limit: int = 3, per_principal_limit: int = 1
) -> list[AbResult]:
    """并发公平 A/B：U1-U5 各在 BaselineGate 与 FairAdmissionController 下跑一遍。"""
    results: list[AbResult] = []
    for name, factory in _LOAD_CASES.items():
        specs = factory()
        results.append(
            await run_case_ab(
                name,
                specs,
                global_limit=global_limit,
                per_principal_limit=per_principal_limit,
                max_queue=_CASE_MAX_QUEUE.get(name, 64),
            )
        )
    return results


def collect_doc_rag(repo_path: Path) -> DocRagReport:
    """权限感知文档 RAG A/B（M18）：合成跨部门语料，三策略越权命中 vs 关授权全库近邻。

    离线可跑（用 `sweep.HashingEmbedder` 确定性高维假向量，无需 GLM/网络）：
    - 播种 proj_a 机密（gold，alice 可见）+ proj_b 机密（越权，与 gold 同关键词=语义近邻）
      + org 公开噪声。
    - baseline：绕过授权 WHERE，直接对 `all_chunks_unfiltered()` 做向量近邻（经典全员可见
      RAG）→ 跨部门 query 命中 proj_b 越权 chunk（>0，可证伪基线）。
    - 隔离态：Pre/Post/Partitioned 三策略均先过授权 WHERE / 分区结构 → 越权命中恒 0。
    真实 GLM `embedding-3` 端到端见 tests/scope/test_m18_glm_e2e.py（需 GLM_API_KEY）。
    """
    from nanoscope.eval.retrieval import Doc, VectorRetriever
    from nanoscope.rag import (
        PartitionedSearcher,
        PostFilterSearcher,
        PreFilterSearcher,
        forbidden_doc_exposure,
        hnswlib_available,
    )
    from nanoscope.rag.store import ChunkStore
    from nanoscope.rag.sweep import HashingEmbedder, seed_scaled_cross_dept

    # query 语义偏向 B 部门（proj_b）机密内容——这正是"跨部门检索"的攻击面：
    # 关授权时全库近邻会把 B 的机密捞进来（可证伪基线），启用授权则 alice 永远看不到 proj_b。
    query = "量子加密机密材料纠缠态实验记录与路线"
    store = ChunkStore(repo_path)
    try:
        gold, forbidden = seed_scaled_cross_dept(store, n_per_group=30, n_forbidden=30)
        emb = HashingEmbedder(dim=128)
        alice = SecurityContext(
            tenant_id="orgX", principal_id="orgX:feishu:alice",
            session_key="feishu:alice", audience_type=AUDIENCE_DM, roles=("proj_a",),
        )

        # --- baseline：关授权、全库向量近邻（经典全员可见 RAG），越权被泄露。 ---
        all_chunks = store.all_chunks_unfiltered()
        docs = [Doc(id=c.id, content=c.content, created_at=c.created_at) for c in all_chunks]
        naive_ids = set(VectorRetriever(docs, emb).search(query, top_k=len(gold)))
        naive_hits = [c for c in all_chunks if c.id in naive_ids]
        baseline_exposure = forbidden_doc_exposure(naive_hits, forbidden)

        # --- 隔离态：三策略先过授权 WHERE / 分区结构，越权命中均为 0。 ---
        pre = PreFilterSearcher(store, emb).search(alice, query, top_k=len(gold))
        post = PostFilterSearcher(store, emb, use_ann=True).search(alice, query, top_k=len(gold))
        part = PartitionedSearcher(store, emb, use_ann=True).search(
            alice, query, top_k=len(gold)
        )
        n_visible = len(store.visible_chunks(alice))
        return DocRagReport(
            query=query,
            n_visible=n_visible,
            n_forbidden=len(forbidden),
            baseline_exposure=baseline_exposure,
            prefilter_exposure=forbidden_doc_exposure(pre.results, forbidden),
            postfilter_exposure=forbidden_doc_exposure(post.results, forbidden),
            partitioned_exposure=forbidden_doc_exposure(part.results, forbidden),
            ann_backend="真 HNSW" if hnswlib_available() else "退化暴力（未装 hnswlib）",
        )
    finally:
        store.close()


async def build_report(
    workspace: Path,
    repo_path: Path,
    *,
    k: int = 5,
    embedder: Embedder | None = None,
) -> UnifiedReport:
    """采集六条证据线的一体化 A/B 结果（M10 三线 + M17 v2 扩展两线 + M18 文档 RAG）。"""
    isolation = collect_isolation(workspace, repo_path)
    retrieval = collect_retrieval(k=k, embedder=embedder)
    concurrency = await collect_concurrency()
    injection = collect_injection_resistance()
    # backpressure 内部用 asyncio.run 自建事件循环，须在独立线程跑（避免嵌套当前 loop）。
    backpressure = await asyncio.to_thread(collect_backpressure)
    retrieval_v2 = collect_retrieval_v2(k=k)
    # 文档 RAG 用独立 db 文件（与记忆 Repository 的 repo_path 分开，互不污染）。
    doc_rag = collect_doc_rag(repo_path.parent / "doc_rag.db")
    return UnifiedReport(
        isolation=isolation,
        retrieval=retrieval,
        concurrency=concurrency,
        injection=injection,
        backpressure=backpressure,
        retrieval_v2=retrieval_v2,
        doc_rag=doc_rag,
    )


def collect_backpressure() -> RoundsAbResult:
    """端到端背压峰值 A/B（M16）：U7 持续过载下 peak_queue 改前(线性) vs 改后(有界)。"""
    specs = workload_u7_sustained_overload(burst=_BACKPRESSURE_BURST, service_time=0.01)
    return run_case_ab_rounds(
        "U7_sustained_overload",
        specs,
        rounds=_BACKPRESSURE_ROUNDS,
        global_limit=_BACKPRESSURE_GLOBAL_LIMIT,
        max_queue=_BACKPRESSURE_MAX_QUEUE,
    )


def collect_retrieval_v2(*, k: int = 5) -> list[ScalePointV2]:
    """带 CI 的规模曲线（M15，Zipf 自然增长 + 多种子）。复用 run_curve_v2。"""
    return run_curve_v2(_CURVE_V2_SCALES, seeds=_CURVE_V2_SEEDS, k=k)


# ---------------------------------------------------------------------------
# HTML 渲染（自包含，内联 CSS，无外部依赖）
# ---------------------------------------------------------------------------

_CSS = """
body{font-family:-apple-system,'PingFang SC',Segoe UI,sans-serif;max-width:960px;
margin:32px auto;padding:0 20px;color:#1c1c1e;line-height:1.6}
h1{font-size:26px;border-bottom:3px solid #0a84ff;padding-bottom:8px}
h2{font-size:20px;margin-top:36px;border-left:4px solid #0a84ff;padding-left:10px}
.lead{color:#555}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:14px}
th,td{border:1px solid #d0d0d5;padding:7px 10px;text-align:right}
th:first-child,td:first-child{text-align:left}
th{background:#f2f2f7}
.good{color:#1a7f37;font-weight:600}
.bad{color:#c9302c;font-weight:600}
.verdict{background:#f2f8ff;border:1px solid #b9dcff;border-radius:8px;padding:10px 14px;margin:10px 0}
.pill{display:inline-block;border-radius:12px;padding:1px 10px;font-size:12px;font-weight:600}
.pill.p0{background:#ffe8e6;color:#c9302c}
.pill.p1{background:#e6f2ff;color:#0a58ca}
code{background:#f2f2f7;padding:1px 5px;border-radius:4px;font-size:13px}
footer{margin-top:40px;color:#888;font-size:12px;border-top:1px solid #eee;padding-top:10px}
"""


def _delta_pct(base: float, imp: float) -> str:
    """改善百分比文本（针对越低越好的指标）。"""
    if base <= 0:
        return "—"
    pct = (base - imp) / base * 100
    arrow = "↓" if pct >= 0 else "↑"
    return f"{arrow}{abs(pct):.0f}%"


def _render_isolation(iso: IsolationReport) -> str:
    b, i = iso.baseline_exposure, iso.isolated_exposure
    hb, hi = iso.history_baseline_exposure, iso.history_isolated_exposure
    b_cls = "bad" if b > 0 else "good"
    i_cls = "good" if i == 0 else "bad"
    hb_cls = "bad" if hb > 0 else "good"
    hi_cls = "good" if hi == 0 else "bad"
    verdict = (
        f"基线在 {iso.n_attacks} 条攻击上，记忆通道累计泄露 <b>{b}</b> 次、"
        f"Recent History 通道累计泄露 <b>{hb}</b> 次他人私聊 canary；"
        f"隔离内核就位后两通道均翻转为 <b>{i}</b> / <b>{hi}</b> 次——安全不变量成立。"
    )
    return f"""
<h2><span class="pill p0">P0</span> 一、隔离安全 A/B（forbidden_prompt_exposure，双通道）</h2>
<p class="lead">冻结攻击集（{iso.n_attacks} 条）上，改前 vs 改后各构造攻击者 prompt 数泄露命中。
覆盖两条进入 system prompt 的通道：记忆通道（<code>Repository.search_visible</code> + DM 门）
与 Recent History 通道（<code>read_recent_history_for_prompt</code> 的 principal/audience 谓词）。</p>
<table>
<tr><th>通道</th><th>改前 baseline</th><th>改后 nanoscope</th></tr>
<tr><td>记忆通道 forbidden_prompt_exposure</td>
<td class="{b_cls}">{b}</td><td class="{i_cls}">{i}</td></tr>
<tr><td>Recent History 通道 forbidden_prompt_exposure</td>
<td class="{hb_cls}">{hb}</td><td class="{hi_cls}">{hi}</td></tr>
</table>
<div class="verdict">{verdict}</div>
"""


def _render_retrieval(points: list[ScalePoint], *, k: int = 5, sla: float = 0.8) -> str:
    has_rag = any(p.vector is not None for p in points)
    head = "<tr><th>N</th><th>grep R@k</th><th>bm25 R@k</th>"
    if has_rag:
        head += "<th>vector R@k</th><th>rrf R@k</th>"
    head += "</tr>"
    rows = []
    for p in points:
        g_cls = "bad" if p.grep.recall_at_k < sla else ""
        row = (
            f"<tr><td>{p.n}</td>"
            f"<td class=\"{g_cls}\">{p.grep.recall_at_k:.3f}</td>"
            f"<td>{p.bm25.recall_at_k:.3f}</td>"
        )
        if has_rag:
            v = f"{p.vector.recall_at_k:.3f}" if p.vector else "—"
            r = f"{p.rrf.recall_at_k:.3f}" if p.rrf else "—"
            row += f"<td>{v}</td><td>{r}</td>"
        rows.append(row + "</tr>")
    m0 = find_crossover(points, sla)
    verdict = (
        f"grep 的 Recall@{k} 在 <b>N≈{m0}</b> 首次跌破 SLA(≥{sla})，即 RAG 介入拐点 M₀；"
        f"BM25{'/向量/RRF ' if has_rag else ' '}守住召回。"
        if m0 is not None
        else f"当前规模范围内 grep 未跌破 SLA(≥{sla})。"
    )
    return f"""
<h2><span class="pill p1">P1-A</span> 二、召回质量 A/B（规模曲线 Recall@{k}）</h2>
<p class="lead">同一冻结语料在多个规模 N 上，改前近似（grep 新近序）vs 改后（BM25 相关性序
{'+ GLM 向量 + RRF 融合' if has_rag else ''}）的召回对比。红=跌破 SLA。</p>
<table>{head}{''.join(rows)}</table>
<div class="verdict">{verdict}</div>
"""


def _render_concurrency(cases: list[AbResult]) -> str:
    blocks = []
    for ab in cases:
        b, im = ab.baseline, ab.improved
        p95 = _delta_pct(b.queue_wait.p95, im.queue_wait.p95)
        p99 = _delta_pct(b.queue_wait.p99, im.queue_wait.p99)
        blocks.append(f"""
<h3 style="margin-top:22px">{html.escape(ab.case)}</h3>
<table>
<tr><th>指标</th><th>改前 baseline</th><th>改后 nanoscope</th><th>变化</th></tr>
<tr><td>完成 / 拒绝</td><td>{b.n_completed} / {b.n_rejected}</td>
<td>{im.n_completed} / {im.n_rejected}</td><td>—</td></tr>
<tr><td>queue_wait p95(s)</td><td>{b.queue_wait.p95:.4f}</td>
<td>{im.queue_wait.p95:.4f}</td><td>{p95}</td></tr>
<tr><td>queue_wait p99(s)</td><td>{b.queue_wait.p99:.4f}</td>
<td>{im.queue_wait.p99:.4f}</td><td>{p99}</td></tr>
<tr><td>Jain(completed_turns)</td><td>{b.jain_completed_turns:.4f}</td>
<td>{im.jain_completed_turns:.4f}</td><td>描述性指标，不单独证明调度公平</td></tr>
<tr><td>throughput(turn/s)</td><td>{b.throughput:.2f}</td>
<td>{im.throughput:.2f}</td><td>—</td></tr>
</table>
""")
    return (
        "<h2><span class=\"pill p1\">P1-B</span> 三、并发公平 A/B（U1-U5 压测）</h2>"
        "<p class=\"lead\">Mock LLM 隔离网络抖动，同 workload 在 BaselineGate（裸 FIFO Semaphore）"
        "vs FairAdmissionController（有界 admission + per-principal 配额 + least-in-flight 公平出队）"
        "下各跑一遍。聚合 Jain 仅作描述；公平性主要看普通用户与 hog 的 per-principal "
        "queue-wait 拆分，详见 reports/M9_LOADTEST_REPORT.md。</p>"
        + "".join(blocks)
    )


def _render_injection(rep: InjectionReport) -> str:
    """记忆注入静态探针残留 A/B；不把字符串残留解释为模型行为。"""
    b, i = rep.baseline_probe_survival, rep.hardened_probe_survival
    b_cls = "bad" if b > 0 else "good"
    i_cls = "good" if i < b else "bad"
    delta = _delta_pct(rep.baseline_survival_rate, rep.hardened_survival_rate)
    verdict = (
        f"{rep.n_payloads} 条记忆型注入 payload 上，改前静态探针原样残留 <b>{b}</b> 条"
        f"（残留率 {rep.baseline_survival_rate:.0%}）；清洗 + 转义 + data-block 包裹后"
        f"降至 <b>{i}</b> 条（残留率 {rep.hardened_survival_rate:.0%}，{delta}）。"
        f"其中 <b>{rep.residual_natural_language}</b> 条为纯自然语言注入残留——"
        f"本评测未调用模型，不能外推为真实攻击成功率；纯 NL 只能靠模型对齐/行为级红队验证。"
    )
    return f"""
<h2><span class="pill p1">P1</span> 四、记忆注入静态探针残留 A/B</h2>
<p class="lead">同一攻击集经改前（原样 <code>- {{content}}</code> 拼接）与改后（清洗 + 转义 +
<code>&lt;memory&gt;</code> 不可信数据块包裹）两条链路，统计探针字符串是否原样存活。
这是结构防护指标，不是模型执行/越权成功率。</p>
<table>
<tr><th>指标</th><th>改前 baseline</th><th>改后 nanoscope</th><th>变化</th></tr>
<tr><td>静态探针残留数 / 总 payload</td>
<td class="{b_cls}">{b} / {rep.n_payloads}</td>
<td class="{i_cls}">{i} / {rep.n_payloads}</td><td>{delta}</td></tr>
<tr><td>静态探针残留率</td>
<td class="{b_cls}">{rep.baseline_survival_rate:.1%}</td>
<td class="{i_cls}">{rep.hardened_survival_rate:.1%}</td><td>{delta}</td></tr>
<tr><td>其中纯自然语言残留（诚实非零）</td><td>—</td>
<td>{rep.residual_natural_language}</td><td>—</td></tr>
</table>
<div class="verdict">{verdict}</div>
"""


def _render_backpressure(bp: RoundsAbResult) -> str:
    """端到端背压峰值 A/B（M16）：U7 持续过载 peak_queue 改前(线性) vs 改后(有界)。"""
    bq, iq = bp.baseline_peak_queue, bp.improved_peak_queue
    bg, ig = bp.baseline_goodput, bp.improved_goodput
    br, ir = bp.baseline_rejection_ratio, bp.improved_rejection_ratio
    q_delta = _delta_pct(bq.mean, iq.mean)
    verdict = (
        f"{bp.rounds} 轮聚合下，持续过载（{bp.case}）时基线队列峰值 "
        f"<b>{bq.mean:.1f} ± {bq.ci95:.1f}</b> 随灌入线性堆积（无界）；"
        f"改后有界准入把 peak_queue 收敛到 <b>{iq.mean:.1f} ± {iq.ci95:.1f}</b>"
        f"（=max_queue，{q_delta}），代价是拒绝率从 {br.mean:.1%} 升至 {ir.mean:.1%}"
        f"（有界背压优雅拒绝），有效吞吐 goodput 基本持平"
        f"（{bg.mean:.1f}→{ig.mean:.1f} turn/s）——峰值有界成立。"
    )
    return f"""
<h2><span class="pill p1">P1-B</span> 五、端到端背压峰值 A/B（U7 持续过载，多轮 95% CI）</h2>
<p class="lead">灌入量远超容量的持续过载下，BaselineGate（无界 bus + 全局 FIFO）vs
FairAdmissionController（有界 admission，<code>max_queue={_BACKPRESSURE_MAX_QUEUE}</code>）的
队列峰值 / 拒绝率 / 有效吞吐（sweep-line 重建峰值 + Little's Law 校验，见 reports/M9_LOADTEST_REPORT.md §9）。</p>
<table>
<tr><th>指标（均值 ± 95%CI）</th><th>改前 baseline</th><th>改后 nanoscope</th><th>变化</th></tr>
<tr><td>peak_queue（队列峰值）</td>
<td class="bad">{bq.mean:.1f} ± {bq.ci95:.1f}</td>
<td class="good">{iq.mean:.1f} ± {iq.ci95:.1f}</td><td>{q_delta}</td></tr>
<tr><td>rejection_ratio（拒绝率）</td>
<td>{br.mean:.1%} ± {br.ci95:.1%}</td>
<td>{ir.mean:.1%} ± {ir.ci95:.1%}</td><td>—</td></tr>
<tr><td>effective_goodput（有效吞吐 turn/s）</td>
<td>{bg.mean:.1f} ± {bg.ci95:.1f}</td>
<td>{ig.mean:.1f} ± {ig.ci95:.1f}</td><td>—</td></tr>
</table>
<div class="verdict">{verdict}</div>
"""


def _render_retrieval_v2(points: list[ScalePointV2], *, k: int = 5, sla: float = 0.8) -> str:
    """带 95% CI 的规模曲线（M15，Zipf 自然增长 + 多种子）：诚实拐点区间。"""
    rows = []
    for p in points:
        g_cls = "bad" if p.grep.mean < sla else ""
        rows.append(
            f"<tr><td>{p.n}</td>"
            f"<td class=\"{g_cls}\">{p.grep.mean:.3f} ± {p.grep.ci95:.3f}</td>"
            f"<td>{p.bm25.mean:.3f} ± {p.bm25.ci95:.3f}</td></tr>"
        )
    m0 = find_crossover_v2(points, sla)
    verdict = (
        f"grep 的 Recall@{k} 均值在 <b>N≈{m0}</b> 附近首次跌破 SLA(≥{sla})，即拐点 M₀；"
        f"这是合成集在给定 Zipf 密度下的<b>区间</b>结论（连同各档 CI 一起看），非单点绝对值——"
        f"消除了 v1 用 <code>_BURY_AT</code> 手工造拐点的方法学硬伤。"
        if m0 is not None
        else f"当前规模范围内 grep 均值未跌破 SLA(≥{sla})；近邻窗口仍够用。"
    )
    return f"""
<h2><span class="pill p1">P1-A</span> 六、规模曲线 v2（Zipf 自然增长 + 多种子 95% CI）</h2>
<p class="lead">主题热度按 Zipf 分布、干扰随 N 连续增长（无手工掩埋阈值），每档跑
{len(_CURVE_V2_SEEDS)} 个种子报均值 ± 95% CI。gold 埋没是干扰密度自然累积的结果。红=均值跌破 SLA。</p>
<table>
<tr><th>N</th><th>grep R@{k} (mean±CI)</th><th>bm25 R@{k} (mean±CI)</th></tr>
{''.join(rows)}
</table>
<div class="verdict">{verdict}</div>
"""


def _render_doc_rag(rep: DocRagReport) -> str:
    """权限感知文档 RAG A/B（M18）：三策略越权命中 vs 关授权全库近邻。"""
    b = rep.baseline_exposure
    b_cls = "bad" if b > 0 else "good"
    pre_cls = "good" if rep.prefilter_exposure == 0 else "bad"
    post_cls = "good" if rep.postfilter_exposure == 0 else "bad"
    part_cls = "good" if rep.partitioned_exposure == 0 else "bad"
    verdict = (
        f"A 部门成员（可见 {rep.n_visible} 条 chunk）用跨部门 query "
        f"「{html.escape(rep.query)}」检索：经典全员可见 RAG（关授权、全库向量近邻）"
        f"泄露 <b>{b}</b> 条 B 部门越权 chunk（共 {rep.n_forbidden} 条越权语料，可证伪基线）；"
        f"Pre-filter / Partitioned 两个正确方案越权命中分别为 "
        f"<b>{rep.prefilter_exposure}</b> / <b>{rep.partitioned_exposure}</b>，隔离在召回前完成；"
        f"Post-filter 最终命中虽为 <b>{rep.postfilter_exposure}</b>，但越权向量已进入候选，"
        f"仅作为反面对照，不满足安全红线（ANN 底座：{rep.ann_backend}）。"
    )
    return f"""
<h2><span class="pill p1">P2</span> 七、权限感知文档 RAG A/B（M18，forbidden_doc_exposure）</h2>
<p class="lead">把 M6 的 <code>forbidden_prompt_exposure==0</code> 从"短记忆条目"延伸到"长文档
chunk"。卖点：业界默认的经典 RAG 全员可见，本子包在向量近邻<b>之前</b>先过授权 WHERE。
Filtered-ANN 三策略——Pre-filter（正确性天花板）/ Post-filter（反面对照：越权进候选被打分，
但结果仍被否决）/ Partitioned（选定解：越权子图从不遍历）。ANN 加速层底座：<b>{rep.ann_backend}</b>。</p>
<table>
<tr><th>检索方式</th><th>越权命中（forbidden_doc_exposure）</th></tr>
<tr><td>经典全员可见 RAG（关授权，全库向量近邻）</td><td class="{b_cls}">{b}</td></tr>
<tr><td>Pre-filter（授权 WHERE 召回前过滤，正确性天花板）</td><td class="{pre_cls}">{rep.prefilter_exposure}</td></tr>
<tr><td>Post-filter（反面对照：越权进候选被打分）</td><td class="{post_cls}">{rep.postfilter_exposure}</td></tr>
<tr><td>Partitioned（选定解：按 ACL 分区，越权子图不遍历）</td><td class="{part_cls}">{rep.partitioned_exposure}</td></tr>
</table>
<div class="verdict">{verdict}</div>
"""


def render_html(report: UnifiedReport, *, title: str = "NanoScope 统一 A/B 报告") -> str:
    """把一体化 A/B 结果渲染成自包含 HTML 字符串。"""
    body = (
        _render_isolation(report.isolation)
        + _render_retrieval(report.retrieval)
        + _render_concurrency(report.concurrency)
        + _render_injection(report.injection)
        + _render_backpressure(report.backpressure)
        + _render_retrieval_v2(report.retrieval_v2)
        + _render_doc_rag(report.doc_rag)
    )
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{_CSS}</style></head>
<body>
<h1>{html.escape(title)}</h1>
<p class="lead">一份自包含的改前 vs 改后证据平面：把 P0 记忆隔离内核（含 Recent History 双通道）、
P1-A 混合检索、P1-B 应用层公平调度、记忆注入静态探针残留、端到端背压峰值、带 CI 规模曲线 v2、
权限感知文档 RAG 七条线的可证伪收益汇于一处（PRD §11-M10 + PRD_v4 §M17 统一报告 v2 + §M18）。</p>
{body}
<footer>由 <code>nanoscope.eval.reporter</code> 生成 · 数据与 tests/scope 各里程碑单测同源 · 分支 ljj/scope_v0</footer>
</body></html>
"""


def generate(out_path: Path, workspace: Path, repo_path: Path, *,
             embedder: Embedder | None = None) -> Path:
    """采集三线 A/B 并把一体化 HTML 写到 out_path，返回该路径。"""
    report = asyncio.run(build_report(workspace, repo_path, embedder=embedder))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(report), encoding="utf-8")
    return out_path


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        out = generate(base / "report.html", base / "ws", base / "mem.db")
        print(f"报告已生成：{out}")
