# M9 并发有界公平准入 · 压测报告 (LOADTEST_REPORT)

> 本文件是 NanoScope M9（PRD §15）的压测报告，**自包含**：环境 → 方法 → 用例 → 结果 → 结论 → 答辩口径。
> 随 git 版本更新；数据由 `nanoscope/eval/loadtest.py` 的 A/B runner 产出，测试见 `tests/scope/test_m9_admission.py`。
> 相关代码：调度器 [`nanoscope/concurrency/admission.py`](nanoscope/concurrency/admission.py)、分析器 [`nanoscope/eval/load.py`](nanoscope/eval/load.py)、压测 harness [`nanoscope/eval/loadtest.py`](nanoscope/eval/loadtest.py)。

---

## 1. 背景与目标

nanobot 基线的并发调度是**单用户假设**下的模型，用于**单组织多用户 IM Bot**（飞书/Slack，群+私聊）时暴露三个机制缺陷（PRD §15.2，均已核实 `file:line`）：

| # | 缺陷 | 机制 | 后果 |
|---|---|---|---|
| ① | **全局 FIFO 队头阻塞** | `_dispatch` 里 `async with lock, gate:`，`gate=Semaphore(3)` 持有到整个 turn 结束 | 3 个慢 turn 占满名额后，第 4 个不管多快都排在后面；慢会话拖垮快会话 |
| ② | **无 per-principal 配额** | gate 是全局单一计数，不区分 principal | 一个刷屏用户可占满全部名额，其他人集体饿等 |
| ③ | **无全局有界 admission 背压** | inbound bus 是无界 `asyncio.Queue()`；per-session `maxsize=20` 满后回退排 task，不拒绝 | 洪峰下 task/内存无上限增长，过载即雪崩 |

**目标（不加机器、只改调度）**：在单进程 asyncio 内做应用层公平调度，把 nanobot 从"个人助手并发模型"提升为"团队级共享服务"能扛的调度。验收标准不是"单机覆盖 N 无压力"，而是**验证能否满足冻结 SLA**，并给出实测拐点。

**改造方案（MVP-0 + P1-B.1/B.2，落在 [`FairAdmissionController`](nanoscope/concurrency/admission.py)）**：
- 全局有界 admission 队列：等待者超过 `max_queue` → 优雅拒绝（解缺陷③）。
- per-principal in-flight 上限：单人最多同时 `per_principal_limit` 个在飞（解缺陷②）。
- least-in-flight 公平出队：释放名额时优先唤醒"当前占用最少"的 principal，同 principal 内 FIFO（解缺陷①②）。

> **隔离红线**：本调度层只决定"谁先进、进不进得来"，**绝不触碰记忆可见性**——可见性由 M2 `Repository.search_visible` 的授权 WHERE 强制，二者正交。

---

## 2. 测试环境

| 项 | 值 |
|---|---|
| 机器 | Apple M5 Pro，18 核 |
| OS | macOS 26.5.2 (arm64) |
| Python | 3.11.15（`.venv`） |
| 事件循环 | asyncio 默认（单线程） |
| 依赖 | 纯标准库（无 numpy / 无外部压测框架） |
| LLM Provider | **Mock LLM（确定性 `asyncio.sleep`）**，见下方方法论 |
| 被测代码 | `nanoscope/concurrency/admission.py` @ 分支 `ljj/scope_v0` |

---

## 3. 测试方法论

### 3.1 为什么用 Mock LLM（关键前提）

> **PRD §15.5 红线**：Provider 用 Mock LLM（可控固定/可配延迟）**隔离网络抖动**——否则测的是 OpenAI 的延迟，不是调度层。

每个 turn 的耗时用一次确定性 `asyncio.sleep(service_time)` 代表其 IO 密集成本（等 LLM / 工具链）。这样 `queue_wait` / `Jain` **纯粹反映调度算法**，改前/改后收益完全可归因，不被真实网络波动污染。

### 3.2 A/B 对照

同一 workload（同到达序、同 service_time、同事件循环、同机器）在两个控制器下各跑一遍：

- **baseline**（改前，[`BaselineGate`](nanoscope/concurrency/admission.py)）：全局单一 FIFO `Semaphore(3)`，忽略 principal、永不拒绝——三缺陷的载体。
- **nanoscope**（改后，[`FairAdmissionController`](nanoscope/concurrency/admission.py)）：`global_limit=3`，`per_principal_limit=1`，`max_queue` 按用例设定。

### 3.3 指标（[`LoadAnalyzer`](nanoscope/eval/load.py)）

- **queue_wait 分布**：mean/p50/**p95/p99**/max（取分位数而非均值，看尾部劣化）。
- **Jain's Fairness Index**：`J = (Σxᵢ)² / (n·Σxᵢ²)`，取值 `(1/n, 1]`；1=完全公平，1/n=一人独占。**双变量输出**：
  - `Jain(completed_turns)`：xᵢ=完成 turn 数，**偏向短任务用户**（短任务多、自然完成得多）。
  - `Jain(service_time)`：xᵢ=占用服务时间，反映"gate 名额时间"的真实分配。
- **rejected_total**：优雅拒绝数（有界背压是否触发）。
- **throughput**：完成 turn / 墙钟秒。
- **per-principal queue_wait**：把"谁被保护/谁被限流"拆开看（公平的真信号）。

### 3.4 测试执行

```bash
source .venv/bin/activate
python -m ruff check nanoscope/ tests/scope/
python -m pytest tests/scope/test_m9_admission.py -q   # 12 项断言
```

---

## 4. 压测用例（PRD §15.5）

| # | 用例 | 负载构造 | 验证缺陷 | 关键指标 |
|---|---|---|---|---|
| **U1** | 跨 session 并发爬坡 | N 个不同 principal 各发 1 turn（N=3→48） | gate 饱和点、queue_wait 随 N 劣化 → 找拐点 M | queue_wait p99/max |
| **U2** | 同 session 洪水 | 单 principal 连发 30 turn | per-principal 配额下的自我限流 | 该 principal 完成延迟 / throughput |
| **U3** | 慢会话隔离（★核心） | 单个慢 principal 连发 6 个长 turn（0.20s）+ 8 个不同快 principal 各 1 短 turn（0.02s） | ① 慢会话拖垮快会话 | **快 turn 的 queue_wait p95** |
| **U4** | 混合负载 + 公平 | 刷屏用户 hog 每轮 3 长 turn + 3 普通用户各 1 短 turn，共 6 轮 | ② per-principal 分配公平 | 普通用户 vs hog 的 queue_wait |
| **U5** | 洪峰过载 | 瞬时灌 200 个请求（20 principal），gate 仅 3 | ③ 有界 admission 是否稳住 | rejected_total / queue_wait 尾部 |

---

## 5. 测试结果（真实数据）

> 数据由 A/B runner 单次运行采集（Mock LLM 确定性延迟，波动 <5%）。`per_principal_limit=1`，`global_limit=3`。

### 5.1 U1 跨 session 并发爬坡（拐点 M）

| N | baseline p99(s) | nanoscope p99(s) | baseline max(s) | nanoscope max(s) |
|---|---|---|---|---|
| 3  | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| 6  | 0.0473 | 0.0481 | 0.0473 | 0.0481 |
| 12 | 0.1438 | 0.1444 | 0.1439 | 0.1446 |
| 24 | 0.3351 | 0.3340 | 0.3353 | 0.3342 |
| 48 | 0.7199 | 0.7185 | 0.7203 | 0.7189 |

**读数**：都是**不同 principal 各 1 turn** 的均质负载，per-principal 配额不产生差异——两条曲线几乎重合（改后无回退，符合预期）。queue_wait 随 N 线性上升：`gate=3`、单 turn 0.05s，则第 k 批（每批 3 个）等待约 `(⌈k/3⌉-1)·0.05s`。以冻结 SLA `queue_wait_p99 ≤ 0.3s` 判定，**实测拐点 M≈20~24**（N=24 时 p99=0.335s 首次越界）——即"单进程 gate=3 在约 20 个并发 principal 后需要扩容或调高并发上限"。这是**可证伪的容量结论**，不是"单机无压力"的空话。

### 5.2 U3 慢会话隔离（★核心，缺陷①）

按 principal 拆开的 queue_wait p95（5 次取样，稳定）：

| principal 组 | baseline p95(s) | nanoscope p95(s) | 变化 |
|---|---|---|---|
| **快会话 fast\***（8 个不同 principal） | ~0.427 | **~0.247** | **↓ 约 42%** |

**读数**：基线全局 FIFO + gate 下，慢 principal 的 6 个长 turn（每个 0.20s）占满 3 个名额，8 个快 turn 只能排在长 turn 之后集体等待；改后 `per_principal_limit=1` 把慢 principal 限制在**最多 1 个名额**，另外 2 个名额始终留给快 principal → **快 turn 排队时间下降约 42%，慢会话不再拖垮快会话**。（聚合表里 nanoscope 的整体 p99 看似更高，是因为把慢会话自己的排队算了进来——它被限流后自身等待变长，但这正是"把资源让给快会话"的代价，是设计意图。**per-principal 拆分才是诚实信号**。）

### 5.3 U4 混合负载 + 公平（缺陷②）

按 principal 拆开的 queue_wait p95（5 次取样，稳定）：

| principal 组 | baseline p95(s) | nanoscope p95(s) | 变化 |
|---|---|---|---|
| **普通用户 alice/bob/carol** | ~0.438 | **~0.192** | **↓ 约 56%** |
| 刷屏用户 hog | ~0.379 | ~0.954 | ↑（被限流，符合预期） |

**读数**：基线下刷屏用户 hog 凭"先到 + 无配额"与普通用户平等抢占名额，普通用户被迫陪等；改后 per-principal 配额把 hog 压到 1 个名额，**普通用户排队延迟下降约 56%，hog 被限流**——这正是"防单人霸占、保护多数用户体验"的公平目标。

> **Jain 双变量说明**：本用例所有 turn 都完成、每个 principal 的总服务时间由任务固有成本决定，故 `Jain(service_time)`（基线 0.430 vs 改后 0.429）近似不变——这符合数学预期：调度改变的是**时序（谁先谁后）**而非**总量**。公平的真实收益体现在 **per-principal 排队延迟**，而非完成量/服务时间的 Jain。这说明"只看一个聚合数会误判"，per-principal 拆分才诚实。

### 5.4 U5 洪峰过载（缺陷③）

| 指标 | baseline | nanoscope | 说明 |
|---|---|---|---|
| 总请求数 | 200 | 200 | 瞬时灌入 |
| 完成数 | 200 | 35 | — |
| **拒绝数（有界背压）** | **0** | **165** | 基线无界从不拒绝；改后队满优雅拒绝 |
| queue_wait p95(s) | 3.2149 | **0.5242** | ↓ 84% |
| queue_wait p99(s) | 3.3173 | **0.5601** | ↓ 83% |
| queue_wait max(s) | 3.3674 | **0.5601** | ↓ 83% |
| throughput(turn/s) | 58.48 | 57.20 | 基本持平 |

**读数**：基线无界排队，200 个请求全部堆在队列里，尾部等待被拖到 **3.3s**（且队列长度无上限，真实场景下 task/RSS 会无上限增长，过载即雪崩）；改后 `max_queue=32` 触发**有界优雅拒绝**（165 个被拒，可回 retry-after），入场者的 queue_wait p99 从 3.32s **压到 0.56s（↓83%）**——**用"可控拒绝"换"稳定延迟 + 有界资源"**，这正是背压的价值。被拒不是失败，是系统在过载下保住 SLA 的正确行为。

### 5.5 U2 同 session 洪水

单 principal 连发 30 turn 时，改后 `per_principal_limit=1` 使其**串行化**（同一人同时只占 1 名额），p95 从 0.159s 升到 0.550s、throughput 从 144 降到 48 turn/s。**这是设计意图**：单个 principal 自我洪水时被限流，把另外 2 个名额留给其他用户；牺牲单人吞吐，换全局公平——与 U3/U4 同一逻辑的另一面。

---

## 6. 结论

| 缺陷 | 用例 | 改造效果（可证伪） |
|---|---|---|
| ① 队头阻塞 | U3★ | 快会话 queue_wait p95 **↓42%**（0.427s→0.247s），慢会话不再拖垮快会话 |
| ② 单人霸占 | U4 | 普通用户 queue_wait p95 **↓56%**（0.438s→0.192s），刷屏用户被限流 |
| ③ 无界雪崩 | U5 | 有界优雅拒绝生效（165 拒），入场 p99 **↓83%**（3.32s→0.56s），资源有上限 |
| 容量拐点 | U1 | 实测拐点 **M≈20~24**（SLA p99≤0.3s），改后均质负载无回退 |

**一句话**：不加机器、只在单进程 asyncio 内换调度策略（有界 admission + per-principal 配额 + least-in-flight 公平出队），把三个框架内部算法缺陷逐一转化为可证伪的收益，收益完全归因于调度算法（Mock LLM 隔离网络）。

---

## 7. 答辩口径（PRD §15.6）

| 追问 | 可防守回答 |
|---|---|
| 为什么不加机器 / 水平扩展？ | 目标规模（几十并发）瓶颈不是机器数而是**缺调度策略**；FIFO 队头阻塞/无公平是**框架内部算法缺陷**，加机器解决不了。单进程 A/B 能把收益纯归因于调度。 |
| gate=3 改大点不就行了？ | gate 只是并发上限旋钮，调大它治不了结构问题：没有公平（单人仍霸占）、没有背压（洪峰仍雪崩）、慢会话仍队头阻塞。我改的是**调度策略**，不是那个数字。 |
| 怎么证明不是随机波动？ | 冻结 workload + **Mock LLM 隔离网络** + 同机器 A/B + 取分布（p95/p99 非均值）+ Jain 双变量 + per-principal 拆分 + 多次取样。可复现、可证伪。 |
| asyncio 单线程能算高并发吗？ | 这里是 **IO 密集的 turn 并发**（等 LLM/工具），单线程事件循环足够"同时在飞"多个；瓶颈从来不是 CPU 线程，而是 **gate + 调度策略**。 |
| Jain 为什么要两个变量？ | 只看完成 turn 数会**偏向短任务用户**、掩盖对长任务的不公；本报告进一步指出 U4 里连 `Jain(service_time)` 都近似不变，**真信号在 per-principal 排队延迟**——两个 Jain + 拆分一起看才诚实。 |
| U3/U4 里 nanoscope 的聚合 p99 反而更高？ | 那是把"被限流的慢/刷屏用户自己的等待"也算进了聚合。设计意图就是**牺牲霸占者、保护多数**；per-principal 拆分显示被保护用户（fast/normals）延迟大幅下降。 |

---

## 8. 复现步骤

```bash
source .venv/bin/activate
# 单测（12 项断言：控制器不变量 + 分析器数学 + U1-U5 A/B 可证伪）
python -m pytest tests/scope/test_m9_admission.py -q

# 自定义 A/B（示例：跑 U5 洪峰并打印对比表）
python - <<'PY'
import asyncio
from nanoscope.eval.load import format_ab
from nanoscope.eval.loadtest import run_case_ab, workload_u5_overload
res = asyncio.run(run_case_ab("U5", workload_u5_overload(burst=200),
                              global_limit=3, per_principal_limit=1, max_queue=32))
print(format_ab(res.baseline, res.improved))
PY
```

生产接线：`multiUser.enabled=true` 时，[`AgentLoop`](nanobot/agent/loop.py) 在 `_dispatch` 用 `FairAdmissionController` 替换基线裸 `Semaphore`，准入键为 `principal_id`（跨 session 同一人共享配额）；旋钮 `multiUser.perPrincipalLimit` / `multiUser.admissionMaxQueue`（[`config/schema.py`](nanobot/config/schema.py)）。`enabled=false` 时行为与基线完全一致。

---

## 9. M16 压测闭环 v2（PRD_v4 §M16）

> v1（§1~§8）证明了"改后 queue_wait 尾延迟下降"，但存在两个削弱证据可信度的方法学缺口：
> (a) 只看 completed-only 尾延迟，会把"拒绝了大量请求"误读成"尾延迟改善"（样本选择偏差）；
> (b) 单次运行无区间，无法排除随机波动；(c) 缺"背压峰值有界"的直接压测级证据。
> v2 把它们补齐：**拒绝-延迟联合视图 + sweep-line 峰值 + Little's Law + 多轮 CI**。
> 数据由 [`run_case_ab_rounds`](nanoscope/eval/loadtest.py) 产出，测试见 [`test_m16_loadtest_v2.py`](tests/scope/test_m16_loadtest_v2.py)（8 项 L1~L5，全绿）；分析函数在 [`load.py`](nanoscope/eval/load.py)。

### 9.1 新增度量（修 v1 样本偏差）

| 度量 | 函数 | 修的问题 |
|---|---|---|
| **rejection_ratio** | `rejection_ratio(records)` | 拒绝数/总数，与 completed-only 尾延迟**分开报告**，杜绝"高拒绝率被误读成尾延迟改善" |
| **effective_goodput** | `effective_goodput(records, wall=…)` | 单位时间成功**完成**数（不含被拒），衡量真实产出 |
| **group_percentiles** | `group_percentiles(records, principals)` | per-principal 子集拆分尾延迟（普通用户 vs hog），聚合 p99 会被限流的 hog 自身等待污染 |
| **peak_concurrency** | `peak_concurrency(records)` | sweep-line 重建时间轴 (峰值在飞, 峰值排队)，给"背压有界"直接压测级证据（v1 只有推断） |
| **littles_law** | `littles_law(records, wall=…)` | 排队论闭环：实测 L ≈ λ·W 一致性校验 |
| **RoundStat** | 多轮聚合 | 均值 ± 95% CI（`1.96·std/√n`），使结论带区间可证伪 |

### 9.2 新增用例

| # | 用例 | 负载构造 | 验证点 |
|---|---|---|---|
| **U6** | 泊松变到达率 | 到达间隔 ~ Exp(rate)，`workload_u6_poisson` | 随机洪峰下 goodput/拒绝率稳定性（真实 IM 是突发泊松流，非均匀节拍） |
| **U7** | 持续过载 | 瞬时灌 burst 个（全 t=0），`workload_u7_sustained_overload` | 背压峰值排队**有界 vs 无界**（L2 核心） |

### 9.3 排队论解释（M16.2）

- **Little's Law（L = λ·W）**：稳态下"系统内平均请求数"= 到达率 × 平均逗留时间。有界 admission 把 W 钉在上界 → L 有天花板，即"在飞+排队"总量不随洪峰无限增长。
- **M/M/c 直觉**：`global_limit=c` 个服务台；到达率超过 `c·μ` 时无界队列的期望队长发散（→∞），这正是基线雪崩的数学根因；有界 `max_queue` 把队长强制截断，代价是拒绝率上升。
- **Jain(queue_wait)**：调度改时序不改总量，故用 per-principal queue_wait 作 xᵢ 衡量公平。

### 9.4 结果（多轮聚合，rounds=5，同源可复现）

**U7 持续过载**（burst=120，global_limit=3，max_queue=32）：

| 指标 | baseline（无界） | nanoscope（有界） | 读数 |
|---|---|---|---|
| **峰值排队 peak_queue** | **117.0 ± 0.00** | **32.0 ± 0.00** | 基线 = burst−global_limit（**线性堆积**）；改后**精确收敛到 max_queue**（有界，L2 铁证） |
| rejection_ratio | 0.000 | 0.708 | 基线从不拒绝（代价是队列无上限）；改后 70.8% 优雅拒绝换有界资源 |
| goodput(完成/s) | 256.9 ± 2.56 | 249.6 ± 1.88 | 基本持平——有界化几乎不损失有效吞吐 |

**U6 泊松变到达**（rate=50，duration=0.3s，max_queue=64，17 个到达）：

| 指标 | baseline | nanoscope | 读数 |
|---|---|---|---|
| 峰值排队 | 1.0 ± 0.00 | 1.0 ± 0.00 | 欠载区：容量足够，两者一致（改后无回退） |
| rejection_ratio | — | 0.000 | 欠载不拒绝（基线路径不变，L5） |
| goodput | 59.5 ± 0.08 | 59.4 ± 0.08 | 一致 |

**Little's Law 校验**（U7 改后单轮）：`λ=253.25/s`，`W=0.0720s`，`L_predicted=18.23`，`L_measured=18.23`，**相对误差 ≈ 0.000**——实测在飞+排队总量与 λ·W 高度一致，佐证"有界准入把 L 钉在上界"的排队论闭环（L3）。

### 9.5 v2 结论

| 缺口 | v2 证据 |
|---|---|
| 样本选择偏差 | rejection_ratio 与 goodput/尾延迟**分开报告**：U7 改后"70.8% 拒绝 + goodput 持平"如实呈现，不再把拒绝伪装成延迟改善 |
| 背压有界（v1 仅推断） | peak_queue **117→32（=max_queue）** 的压测级铁证；U6 欠载区 1→1 无回退 |
| 随机波动 | 多轮 CI：U7 peak_queue CI=0（确定性收敛），goodput CI<3；结论带区间 |
| 数学闭环 | Little's Law rel_err≈0，L=λ·W 一致 |

> **复现**：`python -m pytest tests/scope/test_m16_loadtest_v2.py -q`（8 项）；或直接调 `run_case_ab_rounds("U7", workload_u7_sustained_overload(burst=120), rounds=5, global_limit=3, max_queue=32)`。
