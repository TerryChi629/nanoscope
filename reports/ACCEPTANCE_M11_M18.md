# NanoScope · M11~M18 增量硬化验收文档（PRD_v4）

> 本文件是 PRD_v4_hardening.md（M11~M18，增量硬化与闭环）的**完整验收汇总**，供项目负责人一次性验收。
> 所有验收数字均来自 `tests/scope/` 同源可复现代码（红线④：禁止手填）。
> 承接基线：M0~M10（见 `PROGRESS.md`）；逐里程碑进展见 `PROGRESS_v4.md`；红线与代码位置见 `CLAUDE.md`。

## 0. 一句话结论

M11~M18 全部交付完成并逐里程碑提交。项目内核「隔离墙 + 检索两层正交、隔离在召回前」在
**记忆通道 / Recent History 通道 / 文档 RAG 通道**三条链路上均已闭环验证；六条红线全程未破。
M18 可选纵深已完整落地：三策略 + 授权 WHERE + **真 hnswlib ANN 索引 + ef/M 参数扫描帕累托**，
并用**真实 GLM `embedding-3` 向量**跑通端到端隔离验证（0 越权 + 可证伪对照翻转）。

## 1. 里程碑状态总表

| 里程碑 | 主题 | 优先级 | 状态 | commit |
|---|---|---|---|---|
| M11 | Recent History 的 principal/audience 隔离（修 H1） | P0 | ✅ | 856c8e4c |
| M13 | 端到端有界准入（修 H3） | P1 | ✅ | 5681329c |
| M12 | 不可信记忆 data-block 包裹 + 防注入（修 H2） | P1 | ✅ | eaf3fd8a |
| M14 | 检索质量硬化：min_score + tie-break + abstention（修 H5） | P1 | ✅ | 73a2c8d8 |
| M15 | 规模曲线重做：Zipf 自然增长 + 多种子 CI（修 H4） | P1 | ✅ | 90b55a76 |
| M16 | 压测闭环 v2：端到端背压证据 + 排队论 + 多目标 | P1 | ✅ | 1b3a18e6 |
| M17 | 项目门面与可复现闭环 + 统一报告 v2（修 H6） | P2 | ✅ | a5d09b99 |
| M18 | 权限感知机密文档 RAG（可选纵深，独立分支） | P2·可选 | ✅ 已完成 | 05b7489b |

> 施工顺序（PRD_v4 指定，未擅自调整）：M11 → M13 → M12 → M14 → M15 → M16 → M17，最后 M18。
> `tests/scope` 累计测试：M10 基线 75 → **159 passed + 3 skipped**（GLM e2e 缺 key 时 skip，配 `GLM_API_KEY` 后 3 passed）；`ruff` 全绿；单用户/基线路径逐字节零回归。

## 2. 逐里程碑验收数字

### M11 · Recent History 隔离（P0-blocker）✅
补齐 M6 盲区：进入 system prompt 的第二通道「最近对话历史」此前仅按 session_key 过滤，
A 的私聊敏感发言可经 Recent History 进入 B 的 prompt。承重命题 `forbidden_prompt_exposure==0`
从"仅覆盖记忆通道"扩展到"双通道"。

| 通道 | 攻击条数 | 改前 baseline exposure | 改后 nanoscope exposure |
|---|---|---|---|
| 记忆通道（M4/M6） | 3 | 9 | **0** |
| Recent History 通道（M11） | 3 | 9 | **0** |

- 测试：`test_m11_recent_history_isolation.py` R1~R6 共 7 项；R6 复现→修复→翻转闭环（改前 exposure>0 可证伪，改后 ==0）。
- scope：75 → 82 passed。

### M13 · 端到端有界准入（P1）✅
修 M9 准入"算法对但接线错"：admission 前移到 session lock 之外（改动点①）、入口非阻塞预检
不建 task（改动点②）、被拒真实用户发回执（改动点③）、配置护栏 fail-closed 拒无界（改动点④）。

| 验收点 | 结果 |
|---|---|
| B1 前移生效（队满在拿锁前被拒 + 回执） | ✅ |
| B2 task 峰值有界（入口预检队满不建 task） | ✅ |
| B3 拒绝回执含 retry_after | ✅ |
| B4 不误伤（cron/internal continuation 不发回执） | ✅ |
| B5 单用户零回归（`_admission is None` + 配置护栏拒无界） | ✅ |

- 测试：`test_m9_admission.py`（+6 单测）+ `test_m13_*` e2e（B1~B5）；scope：82 → 96 passed。

### M12 · 不可信记忆 data-block 包裹 + 防注入（P1）✅
`memory_remember` 可写入"忽略以上所有指令……"形成持久化 Prompt Injection。新增写入清洗 +
读取转义 + `<memory><item>` 不可信数据块包裹。**诚实边界**：结构化包裹只能显著降低、不能数学消除。

| 指标 | 改前 baseline | 改后 hardened |
|---|---|---|
| 记忆型注入攻击总数 | 21 | 21 |
| 注入成功数 | 21（100%） | 5（≈23.8%） |
| 其中结构型残留 | — | **0**（全中和） |
| 其中纯 NL 残留（诚实非零） | — | 5 |

- 测试：`test_m12_memory_injection.py` I1~I4 共 12 项；scope：96 → 108 passed。

### M14 · 检索质量硬化（P1）✅
`VectorRetriever` 不过滤零分致无关 query 虚增召回；`rrf_fuse` 同分依赖 dict 插入顺序不稳定。
新增 min_score 过滤（abstention）+ 确定性 tie-break `(score, hit_count, best_rank, doc_id)` +
casefold 大小写一致 + 内存库消除临时文件泄漏。

| 验收点 | 结果 |
|---|---|
| Q1 零分过滤（无关 query → 空 + min_score） | ✅ |
| Q2 tie-break 确定性（打乱子检索器输出一致） | ✅ |
| Q3 abstention（全空 → 空） | ✅ |
| Q4 大小写 casefold 不敏感 | ✅ |
| Q5 无临时文件泄漏 | ✅ |
| Q6 隔离红线不破（DM 门 / 跨 principal 双证） | ✅ |

- 测试：`test_m14_retrieval_quality.py` Q1~Q6 共 10 项 + M7 全回归；scope：108 → 118 passed。

### M15 · 规模曲线重做（P1）✅
v1 手工 `_BURY_AT` 预设埋点 → M₀ 本质是数据参数非系统拐点（易被面试官击穿）。v2 用 Zipf 自然增长
（干扰数 `round(λ_t·N)` 连续增长）+ 多种子 CI（`1.96·std/√n`），以带 CI 区间替代单点结论。

| 验收点 | 结果 |
|---|---|
| S1 无阈值（无 `_BURY_AT` + Zipf 热度衰减） | ✅ |
| S2 单调性（grep 均值跌破 SLA、BM25 守高位、有拐点） | ✅ |
| S3 可复现 + 多种子 mean/std/ci95 | ✅ |
| S4 gold rank + 候选集大小分布可观测 | ✅ |
| S5 诚实标注（合成集 + CI，不写单点绝对结论） | ✅ |

- 测试：`test_m15_scale_v2.py` S1~S5 共 7 项；scope：118 → 125 passed。

### M16 · 压测闭环 v2（P1）✅
补 M9 三缺口：completed-only 样本选择偏差、无区间、缺背压峰值直接证据。新增
rejection/goodput 分离 + per-principal 尾延迟 + sweep-line peak_concurrency + Little's Law + 多轮 CI。

| 用例 / 指标 | baseline | nanoscope | 读数 |
|---|---|---|---|
| **U7 峰值排队 peak_queue**（burst=120, max_queue=32） | 117.0 ± 0.00 | **32.0 ± 0.00** | 无界线性堆积 → 精确收敛到 max_queue（L2 铁证） |
| U7 rejection_ratio | 0.000 | 0.708 | 优雅拒绝换有界资源 |
| U7 goodput(完成/s) | 256.9 ± 2.56 | 249.6 ± 1.88 | 基本持平 |
| U6 峰值排队（rate=50 欠载） | 1.0 | 1.0 | 欠载区无回退 |
| **Little's Law**（U7 改后） | — | rel_err ≈ 0.000 | L_meas=L_pred=18.23，L=λ·W 闭环（L3） |

- 测试：`test_m16_loadtest_v2.py` L1~L5 共 8 项；scope：125 → 133 passed。

### M17 · 项目门面 + 统一报告 v2（P2）✅
统一报告从 M10 三线（隔离/召回/并发）additive 扩为**六条线**（+ M12 注入抵抗率 / M16 背压峰值 /
M15 带 CI 曲线）；README 加「🔭 NanoScope」门面；产物归位 `reports/`；`python -m nanoscope.eval.report` 一键入口。

| 验收点 | 结果 |
|---|---|
| P1 报告含六条证据线 | ✅ |
| P2 render_html 自包含含新段落 | ✅ |
| P3 README NanoScope 门面 | ✅ |
| P4 产物归位 + 学习文件 gitignore | ✅ |

- 测试：`test_m10_reporter.py`（+1，共 6 项）；scope：133 → 134 passed。
- 一键入口实测：`python -m nanoscope.eval.report -o /tmp/out.html` 成功生成自包含 HTML。

### M18 · 权限感知机密文档 RAG（P2·可选，已完成）✅
把 M11 隔离墙 + M14 硬化检索器从"短记忆条目"延伸到"长文档 chunk"。卖点=在向量近邻**之前**先过
授权 WHERE。与记忆检索**并存、不替换、不合并**（`search_visible` / `_scoped_memory_for_message` 逐字节未动）。
核心亮点：Filtered-ANN 完备三策略（Pre-filter 正确性基线 / Post-filter 反面对照 / Partitioned 选定解）+
真 `hnswlib` HNSW 索引接入 + ef/M 参数扫描帕累托 + 真实 GLM `embedding-3` 端到端验证。

| 验收点 | 结果 |
|---|---|
| D1 chunk 隔离 + 可证伪闭环 | ✅ 授权下越权命中=0；关授权全库近邻下 >0（翻转即证） |
| D2 标签不可伪造 + fail-closed | ✅ owner 由 ctx 注入；缺 acl_group 拒；DB CHECK 拒非法组合 |
| D3 chunking 正确 + ingest 管线 | ✅ 边界重叠符合配置；坏参数抛错；带 ACL 落库 |
| D4 ANN 一致性 | ✅ 分区 top_k == pre-filter 天花板 |
| D5 重排增益 | ✅ 重排后 nDCG/MRR ≥ 重排前 |
| D6 abstention | ✅ 全库无关 query 返回空 |
| D7 防注入一致 | ✅ chunk 经 M12 data-block 包裹，尖括号转义无法闭合 |
| D8 单用户/未接入零回归 | ✅ 空库返回空不报错 |
| F1 三策略正确性 + post-filter 泄露证据 | ✅ 三策略 0 越权；post-filter 候选含越权 id，pre/part 不含 |
| F2 recall 对照 | ✅ 低可见率下 post-filter recall 崩塌，pre/part 守住=1.0 |
| F3 分区隔离 | ✅ 越权子图 `project_proj_b` 从未被访问 |
| F4 兜底降级 | ✅ 分区膨胀退化 pre-filter，越权仍 0、结果一致 |
| F5 剪枝趋势 | ✅ 分区打分候选集 < 全局候选集 |
| A1 真 HNSW 越权=0（ef∈{10,50,100}） | ✅ 分区 ANN 越权命中恒 0 + 越权子图不访问；post-filter 硬伤：越权进候选被打分 |
| A2 ANN↔暴力一致 | ✅ 候选≤fanout 时分区 ANN 结果逐字节等于 pre-filter 天花板 |
| A3 参数扫描帕累托 | ✅ recall 全 ≥0.8、max_ef≥min_ef；帕累托前沿非空；表诚实标注 ANN 底座 |

- 测试：`test_m18_doc_rag.py` D1~D8 + F1~F5 共 **19 项** + `test_m18_ann_sweep.py` A1~A3 共 **5 项** + `test_m18_glm_e2e.py` **3 项**（缺 `GLM_API_KEY` 时 skip）+ `test_m10_reporter.py` 文档 RAG 段 **1 项**；scope：134 → **159 passed + 3 skipped**（配 key 后 GLM 3 passed）。
- 子包 `nanoscope/rag/`（7 文件）自包含、密钥零入库、合成语料可复现。
- **真 hnswlib-0.8.0 参数扫描实测**（n_per_group=80/n_forbidden=80/org=26，top_k=10，repeats=5，`num_threads=1` 可复现）：pre_filter recall=1.0 / 延迟 2.089ms / 候选 106（暴力天花板）；**partitioned ef=25 recall=1.0 / 延迟 0.463ms**（帕累托最优，≈4.5× 加速）/ 候选 106；post_filter ef=25 recall=1.0 / 延迟 5.444ms / 候选 186（越权进候选，反面对照）。所有点越权命中=0（调参与隔离正交）。帕累托前沿=`[('partitioned',25,1.0,0.4631)]`。
- **真实 GLM `embedding-3` 端到端**：`GLM_API_KEY=… pytest test_m18_glm_e2e.py` → 3 passed；三策略 exposure=0 + `doc_search_visible` 全链路 0 越权 + 关授权全库真实向量近邻可证伪泄露 >0（翻转即证）。凭证只走环境变量，零入库。

## 3. 决策记录（按推荐选定，需负责人知悉）

按新工作方式"遇需抉择处按推荐方案选并记录"，本轮关键决策：

- **M17 报告 v2 采 additive 扩展**（保留 M7 三线，新增三段），低回归风险；背压用 U7 burst=120/max_queue=32/rounds=3 轻量档；带 CI 曲线用 scales=[50,200,1000]/seeds=[1,2,3]。
- **M18 ANN 底座：暴力余弦作正确性基线 + 真 hnswlib HNSW 作加速层双轨**（PRD_v4 §M18.7.3）。pre-filter 永远暴力作 recall 天花板；partitioned/post-filter 通过 `use_ann` 开关启用真 HNSW，ANN 只召回候选、仍用精确余弦重打分（同候选集下逐字节可复现）；未装/关闭时退化暴力，契约不变。ef/M 参数扫描（`sweep.py`）已完成。
- **M18 scope 新增 `project` 用新建 `documents`/`doc_chunks` 表**（不改既有 `memories` 表），避免污染记忆通道。
- **M18 用 `SecurityContext.roles` 承载所属项目集合**（前向兼容字段，不改 identity schema）。
- **M18 机密语料程序合成**（跨部门"量子加密/量子通信"互为语义近邻），真实机密数据绝不入库。
- **M15 外部脱敏验证集**：框架预留、暂缓（`run_curve_v2` 的 docs/queries 注入点）。

## 4. 需负责人提供的 API / 需你操作的事项

GLM 凭证已提供并跑通、hnswlib 已自行 `pip install` 接入，M18 闭环所需已全部到位。下表仅剩**可选**项，**均不 blocking 已交付闭环**：

| 事项 | 用途 | 接入点 | 现状 |
|---|---|---|---|
| **GLM embedding 凭证** | M15 曲线真实向量 / M18 文档向量 | 环境变量 `GLM_API_KEY`（`GLM_BASE_URL`/`GLM_EMBED_MODEL` 可选）；复用 `nanoscope.eval.embedding.GlmEmbedder` | ✅ 已到位：`GLM_API_KEY=… pytest test_m18_glm_e2e.py` → 3 passed（真 `embedding-3` 向量，0 越权，可证伪对照翻转）。凭证只走环境变量、零入库。 |
| **hnswlib 真 ANN 索引** | M18 三策略延迟/内存帕累托曲线 + ef/M 参数扫描 | `nanoscope/rag/index.py` 的 `_HnswIndex` / `_corpus_topk`；`sweep.py` | ✅ 已接入：真 hnswlib-0.8.0（pip 依赖，`nanobot[rag]` extra），`use_ann` 开关默认关，未装/关闭退化暴力，契约不变。partitioned ef=25 recall 追平天花板 1.0、延迟 0.463ms（≈4.5× 加速）。**无需负责人操作**（pip 装即可）。 |
| **脱敏机密语料**（可选） | M18 文档 RAG 真实数据点 / M15 第二数据方向佐证 | M18：`ingest_document(store, ctx, ...)`；M15：`run_curve_v2` docs/queries 注入 | 已拍板"先用合成语料演示"。若想跑真实脱敏语料：放本地 gitignore 目录并告知路径即可本地私跑，绝不入库。非必须。 |
| **统一报告 v2 追加"文档 RAG 段"** | 报告纳入 forbidden_doc_exposure==0（第七条证据线） | `nanoscope/eval/reporter.py` 的 `_render_doc_rag` / `collect_doc_rag` / `DocRagReport` | ✅ 已接入：统一报告从六线扩为**七线**，新增"权限感知文档 RAG A/B"段（关授权经典 RAG 泄露 >0 vs 三策略越权命中恒 0）。离线可跑（HashingEmbedder），`python -m nanoscope.eval.report` 一键生成。 |

> 红线守护确认：`.nanobot/config.json` 保持 gitignore；LLM/GLM 凭证只走环境变量（本轮 GLM key 仅经环境变量注入跑测，未写入任何文件/仓库）；真实机密文档只本地私跑。
> 本轮所有 commit 均**本地未 push**（红线⑤：未经确认不 push、不改写历史、不做破坏性 git 操作）。

## 5. 一键复现命令

```bash
# 全量 scope 验收测试（159 passed + 3 skipped）
.venv/bin/pytest tests/scope -q

# M18 文档 RAG 单独验收（19 项：D1~D8 + F1~F5）
.venv/bin/pytest tests/scope/test_m18_doc_rag.py -q

# M18 真 HNSW ANN 一致性 + ef/M 参数扫描（5 项：A1~A3）
.venv/bin/pytest tests/scope/test_m18_ann_sweep.py -q

# M18 真实 GLM embedding-3 端到端隔离（3 项，需凭证走环境变量）
GLM_API_KEY=<你的key> .venv/bin/pytest tests/scope/test_m18_glm_e2e.py -q

# 真 ANN 加速层（可选依赖）
pip install 'nanobot[rag]'   # 装 hnswlib；未装时自动退化暴力余弦，契约不变

# lint
.venv/bin/ruff check nanoscope/

# 统一报告 v2 一键生成（六条证据线，自包含 HTML）
python -m nanoscope.eval.report -o reports/NANOSCOPE_UNIFIED_REPORT.html
```

## 6. 答辩措辞纪律（诚实边界，禁止夸大）

- **记忆检索** = "权限感知的记忆召回"（RAG 最小定义：检索+注入，无 chunking/向量/重排）；**M18** 才是"权限感知的经典文档 RAG"（完整链路）。不把记忆检索夸成经典 RAG。
- M12 防注入：结构型注入改后 100% 中和，纯自然语言注入**仍残留 5 条**（靠模型对齐兜底）——诚实标注非零残留。
- M18 ANN：**真 hnswlib HNSW 已接入**（`use_ann` 开关）。诚实口径：ANN 只召回候选、仍用精确余弦重打分，故同候选集下逐字节可复现；实测 partitioned ef=25 recall 追平暴力天花板 1.0、延迟从 2.089ms 降到 0.463ms（≈4.5× 加速）——**这是"追平天花板前提下的加速"，不是无损全能**。HNSW recall 本身非严格单调（ef 10→25→50→100 实测 0.9→1.0→0.9→1.0），已如实标注、不宣称"越大越好"。关键：**ANN 调参（ef/M）绝不影响可见性**，越权命中在所有参数点恒为 0（隔离与检索正交）。
- M18 语料：合成 + 结构/固定两种 chunking；真实机密数据本地私跑不入库。
