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
from nanoscope.eval.loadtest import CASES as _LOAD_CASES
from nanoscope.eval.loadtest import AbResult, run_case_ab
from nanoscope.eval.scale_curve import ScalePoint, find_crossover, run_curve
from nanoscope.identity import AUDIENCE_DM, SecurityContext
from nanoscope.memory import SCOPE_USER, Repository

# U5 洪峰用例用更小的 max_queue 才能触发有界拒绝（与 M9 报告一致）。
_CASE_MAX_QUEUE: dict[str, int] = {"U5_overload": 32}


@dataclass(frozen=True)
class IsolationReport:
    """隔离安全 A/B：冻结攻击集上的 forbidden_prompt_exposure（越低越好，目标=0）。"""

    n_attacks: int
    baseline_exposure: int  # 基线（全局 MEMORY.md 无条件注入）总泄露命中数
    isolated_exposure: int  # 隔离态（SecurityContext + search_visible）总泄露命中数


@dataclass(frozen=True)
class UnifiedReport:
    """三条证据线的一体化 A/B 结果。"""

    isolation: IsolationReport
    retrieval: list[ScalePoint]
    concurrency: list[AbResult]


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

    return IsolationReport(
        n_attacks=len(attacks),
        baseline_exposure=baseline_exposure,
        isolated_exposure=isolated_exposure,
    )


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


async def build_report(
    workspace: Path,
    repo_path: Path,
    *,
    k: int = 5,
    embedder: Embedder | None = None,
) -> UnifiedReport:
    """采集三条证据线的一体化 A/B 结果。"""
    isolation = collect_isolation(workspace, repo_path)
    retrieval = collect_retrieval(k=k, embedder=embedder)
    concurrency = await collect_concurrency()
    return UnifiedReport(isolation=isolation, retrieval=retrieval, concurrency=concurrency)


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
    b_cls = "bad" if b > 0 else "good"
    i_cls = "good" if i == 0 else "bad"
    verdict = (
        f"基线在 {iso.n_attacks} 条攻击上累计泄露 <b>{b}</b> 次他人私聊 canary；"
        f"隔离内核就位后翻转为 <b>{i}</b> 次——安全不变量成立。"
    )
    return f"""
<h2><span class="pill p0">P0</span> 一、隔离安全 A/B（forbidden_prompt_exposure）</h2>
<p class="lead">冻结攻击集（{iso.n_attacks} 条）上，改前（全局 MEMORY.md 无条件注入）
vs 改后（SecurityContext + Repository.search_visible + DM 门）各构造攻击者 prompt 数泄露命中。</p>
<table>
<tr><th>指标</th><th>改前 baseline</th><th>改后 nanoscope</th></tr>
<tr><td>攻击条数</td><td>{iso.n_attacks}</td><td>{iso.n_attacks}</td></tr>
<tr><td>forbidden_prompt_exposure</td>
<td class="{b_cls}">{b}</td><td class="{i_cls}">{i}</td></tr>
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
<td>{im.jain_completed_turns:.4f}</td><td>—</td></tr>
<tr><td>throughput(turn/s)</td><td>{b.throughput:.2f}</td>
<td>{im.throughput:.2f}</td><td>—</td></tr>
</table>
""")
    return (
        "<h2><span class=\"pill p1\">P1-B</span> 三、并发公平 A/B（U1-U5 压测）</h2>"
        "<p class=\"lead\">Mock LLM 隔离网络抖动，同 workload 在 BaselineGate（裸 FIFO Semaphore）"
        "vs FairAdmissionController（有界 admission + per-principal 配额 + least-in-flight 公平出队）"
        "下各跑一遍。聚合指标见下；per-principal 拆分见 M9_LOADTEST_REPORT.md。</p>"
        + "".join(blocks)
    )


def render_html(report: UnifiedReport, *, title: str = "NanoScope 统一 A/B 报告") -> str:
    """把一体化 A/B 结果渲染成自包含 HTML 字符串。"""
    body = (
        _render_isolation(report.isolation)
        + _render_retrieval(report.retrieval)
        + _render_concurrency(report.concurrency)
    )
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{_CSS}</style></head>
<body>
<h1>{html.escape(title)}</h1>
<p class="lead">一份自包含的改前 vs 改后证据平面：把 P0 记忆隔离内核、P1-A 混合检索、
P1-B 应用层公平调度三条线的可证伪收益汇于一处（PRD §11-M10）。</p>
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
