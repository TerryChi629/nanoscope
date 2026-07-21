# M10 统一 A/B 报告 · 证据平面收口 (UNIFIED_REPORT)

> 本文件是 NanoScope M10（PRD §11-M10 / §13）的收口报告，**自包含**：动机 → 结构 → 三线数据 → 复现 → 答辩口径。
> M10 不新增功能，只做**证据收口**：把 P0/P1 三条线的"改前 vs 改后"汇成一份可交付的一体化 A/B。
> 相关代码：统一报告器 [`nanoscope/eval/reporter.py`](nanoscope/eval/reporter.py)；测试 [`tests/scope/test_m10_reporter.py`](tests/scope/test_m10_reporter.py)；产物 [`M10_UNIFIED_REPORT.html`](M10_UNIFIED_REPORT.html)。

---

## 1. 背景与目标

前面 9 个里程碑各自留下了独立证据：M6 隔离验收（`forbidden_prompt_exposure=0`）、M7 规模曲线（Recall@K）、M9 压测（queue_wait/Jain）。但它们散在各自的测试与报告里，**面试/答辩时无法一屏看全"这次魔改到底改善了什么"**。

M10 的目标（PRD §11-M10）：用一个 `Reporter` 出**改前 vs 改后一体化报告**（隔离安全 + 召回质量 + 并发公平），飞书演示可见。

**设计红线**：Reporter **只做聚合 + 渲染，不重复实现任何指标逻辑**。三条线的取数全部复用既有模块——
- 隔离安全：复用 M0/M6 的冻结攻击集（[`dataset.py`](nanoscope/eval/dataset.py)）、`ContextBuilder`、`Repository.search_visible`。
- 召回质量：复用 M7 的 [`scale_curve.run_curve`](nanoscope/eval/scale_curve.py)。
- 并发公平：复用 M9 的 [`loadtest.run_case_ab`](nanoscope/eval/loadtest.py)。

这样保证**报告数据与各里程碑单测同源、可复现**，不会出现"报告里一个数、测试里另一个数"的漂移。

---

## 2. 测试环境

| 项 | 值 |
|---|---|
| 机器 | Apple M5 Pro，18 核 |
| OS | macOS 26.5.2 (arm64) |
| Python | 3.11.15（`.venv`） |
| 依赖 | 纯标准库（HTML 内联 CSS，无前端框架、无 numpy） |
| LLM Provider | 隔离/召回线为确定性白盒；并发线为 **Mock LLM**（`asyncio.sleep`） |
| 被测代码 | `nanoscope/eval/reporter.py` @ 分支 `ljj/scope_v0` |

---

## 3. 报告结构与采集方法

`Reporter` 的数据流：`build_report()` → 三个 `collect_*()` → `UnifiedReport` → `render_html()` → 自包含 HTML。

### 3.1 隔离安全线（P0，`collect_isolation`）

同一冻结攻击集，两条路径各构造攻击者 prompt，数命中他人私聊 canary 的次数：

- **改前 baseline**：把所有私聊个人事实（模拟 Dream 后门效果）写进全局 `MEMORY.md`，基线 `ContextBuilder.build_system_prompt` 对**任意**攻击者/群聊**无条件注入** → 每条攻击都命中他人 canary。
- **改后 nanoscope**：同样的事实经 user-scope 写入口落 SQLite（owner=本人，DM 语境）→ 用攻击者 `SecurityContext` 走 `search_visible` 拿到**自己可见集合** → 隔离态 `ContextBuilder`（`multi_user_isolation=True`）构造 prompt → 授权 WHERE + DM 门使命中为 0。

指标：`forbidden_prompt_exposure`（越低越好，目标=0）。

### 3.2 召回质量线（P1-A，`collect_retrieval`）

直接复用 M7 `run_curve`，在冻结检索集（`data/frozen_retrieval_v1.json`，规模 N=50/200/1000/5000）上跑 grep（改前近似：新近序）vs BM25（改后：相关性序）。传入 `embedder` 时额外出 vector/rrf 两列。指标：Recall@5，SLA=0.8，定位拐点 M₀。

### 3.3 并发公平线（P1-B，`collect_concurrency`）

复用 M9 用例注册表 `CASES`，U1-U5 每个用例都在 `BaselineGate`（裸 FIFO Semaphore）与 `FairAdmissionController`（有界 admission + per-principal 配额 + least-in-flight 公平出队）下各跑一遍。U5 洪峰用 `max_queue=32` 触发有界拒绝。指标：queue_wait p95/p99、拒绝数、Jain、throughput。

### 3.4 渲染

`render_html` 输出**自包含 HTML**（内联 CSS，无外部依赖，可直接发飞书/邮件/浏览器打开），分三段小节 + 每段一句"翻转结论"，红/绿高亮跌破 SLA / 泄露命中。

---

## 4. 测试结果（真实数据）

> 数据由 `build_report()` 单次运行采集（并发线 Mock LLM，波动 <5%）。产物见 `M10_UNIFIED_REPORT.html`。

### 4.1 隔离安全 A/B（★ P0 承重腿）

| 指标 | 改前 baseline | 改后 nanoscope |
|---|---|---|
| 攻击条数 | 3 | 3 |
| **forbidden_prompt_exposure** | **9** | **0** |

**读数**：基线在 3 条攻击上累计泄露 **9** 次他人私聊 canary（每条攻击的 prompt 都被全局 `MEMORY.md` 无条件塞进全部 3 条私聊事实）；隔离内核就位后翻转为 **0**——这是本项目最硬的安全不变量证据（M0 的 >0 → M6/M10 的 =0）。

### 4.2 召回质量 A/B（P1-A）

| N | grep Recall@5 | bm25 Recall@5 |
|---|---|---|
| 50   | 0.625 | 0.875 |
| 200  | 0.500 | 0.875 |
| 1000 | 0.250 | 0.875 |
| 5000 | 0.000 | 0.875 |

**读数**：grep 随规模 N 阶梯下滑（0.625→0.000），在 **N=50 即跌破 SLA 0.8**（此冻结检索集的拐点 M₀，规模起点就已越界，说明全量注入/新近序在这套语料上很快失效）；BM25 全程守住 0.875。（M7 报告里 M₀≈20 是**程序合成**的另一套曲线数据集；此处用的是冻结 JSON 检索集，规模起点为 50，两者是不同语料，结论方向一致：grep 随 N 失效、BM25 守住。）

### 4.3 并发公平 A/B（P1-B，U1-U5 聚合）

| 用例 | 完成(前/后) | 拒绝(前/后) | p95(s)(前→后) | p99(s)(前→后) | throughput(前→后) |
|---|---|---|---|---|---|
| U1 跨 session 爬坡 | 12/12 | 0/0 | 0.143→0.143 | 0.144→0.143 | 58.8→58.9 |
| U2 同 session 洪水 | 30/30 | 0/0 | 0.161→0.551 | 0.162→0.574 | 142.7→47.6 |
| U3 慢会话隔离 | 14/14 | 0/0 | 0.428→0.870 | 0.429→0.974 | 30.1→11.6 |
| U4 混合负载公平 | 36/36 | 0/0 | 0.437→0.902 | 0.438→0.985 | 73.1→32.8 |
| **U5 洪峰过载** | 200/**35** | 0/**165** | **3.212→0.526** | **3.315→0.562** | 58.5→57.1 |

**读数（关键诚实性说明）**：
- **U5 是聚合层最直观的胜负**：基线无界排队把尾部拖到 3.3s；改后有界拒绝 165 个、入场 p99 压到 0.56s（↓83%），资源有上限。
- **U2/U3/U4 的聚合 p95/p99 在改后反而更高**——这是**预期且诚实**的：`per_principal_limit=1` 把刷屏/慢会话限流后，它们**自身的等待被算进了聚合分位数**。聚合数掩盖了"被保护用户"的收益。**公平的真信号在 per-principal 拆分**（U3 快会话 p95 ↓42%、U4 普通用户 p95 ↓56%），详见 [`M9_LOADTEST_REPORT.md`](M9_LOADTEST_REPORT.md) §5.2/§5.3。本统一报告保留聚合数不美化，正是"可证伪、不过满"的态度。

---

## 5. 结论

| 证据线 | 层级 | 改前 → 改后 | 结论 |
|---|---|---|---|
| 隔离安全 | P0 | exposure 9 → **0** | 安全不变量成立（承重腿） |
| 召回质量 | P1-A | grep 0.625→0.000 / bm25 守 0.875 | RAG 介入拐点 M₀=50，BM25 守 SLA |
| 并发公平 | P1-B | U5 p99 3.32s→0.56s + 有界拒绝 165 | 有界背压生效；per-principal 保护见 M9 报告 |

**一句话**：M10 把"一个场景 → 一条承重腿（隔离）+ 两个 P1 数据点（检索/并发）→ 一套证据"收口成一份可交付、同源、可复现的一体化 A/B 报告。

---

## 6. 答辩口径

| 追问 | 可防守回答 |
|---|---|
| 报告数据可信吗？会不会是手搓的？ | Reporter 只做聚合+渲染，三线取数全复用各里程碑的既有模块（`run_curve`/`run_case_ab`/`search_visible`），**与 `tests/scope` 单测同源**，`build_report()` 一键复现。 |
| U2/U3/U4 改后聚合 p95 更高，不是变差了吗？ | 那是把被限流的刷屏/慢会话**自身**的等待算进了聚合。设计意图是牺牲霸占者、保护多数。公平真信号在 per-principal 拆分（M9 报告：快会话 ↓42%、普通用户 ↓56%）。聚合数我不美化，保留原始诚实。 |
| 召回拐点这里是 50，M9/M7 说 20，矛盾吗？ | 两套是不同语料：M7≈20 是程序合成曲线集，M10=50 是冻结 JSON 检索集。规模起点不同，但结论方向一致——grep 随 N 失效、BM25 守住。 |
| 为什么隔离线是白盒而并发线是 Mock？ | 隔离/召回是**确定性判定**（canary 命中、Recall 计算），无需真 LLM；并发线要测**调度算法**，用 Mock LLM 隔离网络抖动才能把收益纯归因于调度（PRD §15.5 红线）。 |

---

## 7. 复现步骤

```bash
source .venv/bin/activate
# 单测（5 项：隔离翻转/U1-U5 覆盖+U5 有界拒绝/三线携带/HTML 自包含/写文件）
python -m pytest tests/scope/test_m10_reporter.py -q

# 一键生成一体化 HTML 报告到仓库根
python - <<'PY'
import asyncio, tempfile
from pathlib import Path
from nanoscope.eval.reporter import build_report, render_html
with tempfile.TemporaryDirectory() as d:
    b = Path(d)
    rep = asyncio.run(build_report(b/"ws", b/"mem.db"))
    Path("M10_UNIFIED_REPORT.html").write_text(render_html(rep), encoding="utf-8")
print("生成：M10_UNIFIED_REPORT.html")
PY
```

传入 GLM `embedder` 可让召回线额外出 vector/rrf 两列（凭证走环境变量 `GLM_API_KEY`，见 M7 报告）。
