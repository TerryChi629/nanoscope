# NanoScope 进展 (PROGRESS.md)

> 随 git 版本更新。每个里程碑收口时在此打勾并记录关键改动/验收结果。
> 详细需求见 [PRD.md](./PRD.md)，施工清单见 [CLAUDE.md](./CLAUDE.md)。

- 分支：`ljj/scope_v0`
- LLM：deepseek v4-flash（已配置，配置文件已 gitignore）
- 上游锁定 commit：`d45d4ebf`（PRD 基线）

## 当前状态
**M10 完成（统一 A/B 报告 · 证据平面收口）。P1 全部收口，NanoScope 魔改主线交付完成。** 新建 `nanoscope/eval/reporter.py`——只做聚合+渲染、三线取数全复用既有模块（隔离用 dataset/ContextBuilder/search_visible、召回用 M7 run_curve、并发用 M9 run_case_ab），出改前 vs 改后一体化自包含 HTML（内联 CSS 无外部依赖，飞书可见）。实测：隔离 exposure 9→0、召回 grep 0.625→0.000/bm25 守 0.875（拐点 M₀=50）、并发 U5 p99 3.32→0.56s + 有界拒绝 165。产出报告 [M10_UNIFIED_REPORT.md](./M10_UNIFIED_REPORT.md) + 产物 M10_UNIFIED_REPORT.html。scope 累计 75 测试绿。**M0~M10 全部完成。**

## 里程碑

| 里程碑 | 层级 | 状态 | 备注 |
|---|---|---|---|
| 文档初始化 | - | ✅ 完成 | CLAUDE.md / PRD.md / PROGRESS.md 就绪；config 已 gitignore |
| M0 冻结基线 + 评测骨架 | P0 | ✅ 完成 | 白盒复现 `forbidden_prompt_exposure > 0`，3 测试绿 |
| M1 SecurityContext + principal 持久化 | P0 | ✅ 完成 | IdentityResolver + tenant fail-closed；10 测试绿 |
| M2 SQLite memories 表 + Repository | P0 | ✅ 完成 | DB CHECK + search_visible 单入口 + DM 门；17 测试绿（含全网格属性测试） |
| M3 memory_remember 工具 | P0 | ✅ 完成 | owner/scope 运行时注入不进 schema + fail-closed + org 门；23 测试绿 |
| M4 检索注入替换全量注入 | P0 | ✅ 完成 | multi_user 下走 search_visible 可见集合替换全局 MEMORY.md，绝不回退；4 测试绿 |
| M5 闭合 Dream 后门 | P0 | ✅ 完成 | multi_user 下 Dream 禁写三文件 + USER.md 停注；5 测试绿 |
| M6 隔离验收 | P0 | ✅ 完成 | `forbidden_prompt_exposure = 0`（M0 >0 翻转）；3 测试绿，scope 累计 35 |
| M7 FTS5/BM25 + 规模曲线 | P1 | ✅ 完成 | BM25+向量(GLM)+RRF；拐点 M₀≈20，四路曲线；52 测试绿 |
| M8 owner-aware Dream candidate | P1 | ✅ 完成 | 按 principal 分批独立 prompt 不混人 + owner 批次确定性继承（非 LLM 指定）+ 无 owner 历史丢弃；6 测试绿，累计 58 |
| M9 并发有界公平准入 + 压测 | P1 | ✅ 完成 | FairAdmissionController（有界 admission + per-principal 配额 + least-in-flight 公平出队）；U1-U5 A/B 实测三缺陷可证伪收益；压测报告 M9_LOADTEST_REPORT.md；累计 70 测试绿 |
| M10 统一 A/B 报告 | P1 | ✅ 完成 | Reporter 聚合三线（隔离/召回/并发）改前 vs 改后一体化自包含 HTML；exposure 9→0、grep→0/bm25 守 0.875、U5 p99 3.32→0.56s；报告 M10_UNIFIED_REPORT.md；累计 75 测试绿 |

## 变更日志
- 初始化：编写 CLAUDE.md（清单式指导）、PROGRESS.md；确认 `.nanobot/config.json` 已被 `.gitignore` 覆盖，API key 不入库。
- M0：新建 `nanoscope/eval/`（冻结数据集 `frozen_v1.json` + `dataset.py` 加载器 + 最小 `TraceCollector` 写 JSONL）；白盒复现测试 `tests/scope/test_m0_baseline_leak.py` 断言基线 `forbidden_prompt_exposure > 0`（私聊个人事实经全局 MEMORY.md 注入他人群聊 prompt），并核实 `read_unprocessed_history` 签名无 owner 过滤（Dream 后门根因）。3 测试全绿。
  - 注：测试目录用 `tests/scope/`（避免与真实包 `nanoscope/` 在 pytest 下命名冲突）。
- M1：新建 `nanoscope/identity.py`（`SecurityContext` 冻结数据类 + `IdentityResolver`，`principal_id = tenant:channel:platform_user_id` 含 tenant 防碰撞，同 workspace 第二 tenant → `TenantConflictError` fail-closed）；`config/schema.py` 加 `MultiUserConfig` 总开关（`enabled=False` 时行为同基线）；`bus/events.py` 的 `InboundMessage` 补 `principal_id/audience_type/audience_id`（默认 None 前向兼容）；`channels/base.py` 渠道边界确定性填 `audience_type/audience_id`；`agent/loop.py` 加 `resolve_security_context()` 并在 `_dispatch` stamp `principal_id`，经 `_persist_user_message_early` 落 history；`agent/memory.py` 的 `append_history` 持久化 `principal_id`。测试 `tests/scope/test_m1_identity.py` 10 项全绿（私聊/群聊受众解析、tenant 防碰撞、fail-closed、history 持久化），memory_store 回归 47 绿。
- M2：新建 `nanoscope/memory/repository.py`（SQLite `memories` 表 + DB CHECK fail-closed + `Repository.add`/`search_visible` 单入口）。`add` 从 `SecurityContext` 注入 `tenant_id/owner_id`（user→owner=principal_id，org→owner=NULL），模型无法伪造；`search_visible` 内部强制授权谓词（org 同 tenant 全员可见；user 仅本人且仅 `audience_type='dm'` 召回——DM 门），上层拿不到拼 WHERE 机会。测试 `tests/scope/test_m2_repository.py` 17 项全绿：全网格属性测试（tenant×principal×audience×scope 可见集合恒等于谓词 oracle）、DB CHECK 拒非法写入（user 缺 owner / org 带 owner）、DM 门闭洞、跨 tenant 硬隔离。
- M3：新建 `nanoscope/memory/remember_tool.py`（`MemoryRememberTool` —— 个人/组织记忆唯一显式写入口）。schema 仅暴露 `content` + `scope`（enum user/org），`owner_id/tenant` 由运行时从 `RequestContext` 注入的安全字段构造 `SecurityContext` 后经 `Repository.add` 落盘——模型即使硬塞 `owner_id/tenant_id` kwarg 也被忽略。缺 `tenant_id/principal_id`（未解析 principal）→ fail-closed 拒写；org 写入需 `allow_org_write`（MVP 默认关，仅允许 user）。`nanobot/agent/tools/context.py` 的 `RequestContext` 补 `tenant_id/principal_id/audience_type`；`loop.py` 在 `multi_user.enabled` 时于 `workspace/memory/nanoscope.db` 建 `Repository`、`_register_default_tools` 手动注册 `memory_remember`、`_request_context_for_turn` 注入安全字段。测试 `tests/scope/test_m3_remember.py` 6 项全绿（schema 不含 owner、注入正确 owner、fail-closed、org 默认拒/放行、模型无法伪造 owner）；`tests/scope/` 累计 23 绿。修复初始循环 import（`remember_tool` 改从 `repository` 子模块直接导入）。
- M4：`agent/context.py` 的 `build_system_prompt`/`build_messages` 新增 `scoped_memory` 参数——非 None（含空串）即多用户模式，用 `Repository.search_visible` 的可见集合替换基线全局 `MEMORY.md` 全量注入，空串=无可见项也**绝不回退**全局（隔离墙硬过滤）；None 保持单用户基线行为。`loop.py` 新增 `_scoped_memory_for_message`（multi_user 下 `resolve_security_context` → `search_visible` → 拼注入串），`_build_initial_messages` 传入。测试 `tests/scope/test_m4_injection.py` 4 项全绿（群聊排除他人个人记忆、org 正常注入、本人私聊召回个人记忆、多用户不回退全局、单用户仍读全局），`tests/scope/` 累计 27 绿；`agent/` context 相关回归 85 绿。
- M5：闭合 Dream 后门（PRD §7）。`MemoryStore`/`ContextBuilder` 加 `multi_user_isolation` 开关，由 `AgentLoop.__init__` 在 `multi_user.enabled` 时置位。隔离下：① `build_dream_tools` 的 `editable_files` 清空——Dream 只能读全仓 + 写 skills，无法把私聊事实蒸馏进 `MEMORY/USER/SOUL`；② `ContextBuilder._bootstrap_files` 移除 `USER.md`（个人记忆改由 owner-aware SQLite 承载），`SOUL.md/AGENTS.md` 仍只读注入。测试 `tests/scope/test_m5_dream_backdoor.py` 5 项全绿（隔离下三文件写入被拒、skills 仍可写、基线三文件仍可写、USER.md 隔离停注/SOUL 仍注入、基线 USER.md 注入）；`tests/scope/` 累计 32 绿，dream/context 回归 109 绿。
- M6：隔离验收（P0 收口，PRD §8 五层证据 / §11-M6）。新建 `tests/scope/test_m6_isolation_acceptance.py`（白盒确定性端到端验收）：把冻结攻击集私聊个人事实经 user-scope 写入口落 SQLite（owner=本人，DM 语境）→ 对每条攻击 query 用攻击者 `SecurityContext` 走 `search_visible` 拼注入串 → 经隔离态 `ContextBuilder`（`multi_user_isolation=True`）构造攻击者 prompt → 断言无任何他人 canary（`forbidden_prompt_exposure=0`，M0 的 >0 翻转，构成五层证据第 4/5 层：属性/攻击集 Benchmark + Trace 审计）。含两条反向对照：本人私聊仍召回本人 fact（不误伤），本人在群里问被 DM 门挡下。3 测试全绿，`tests/scope/` 累计 35 绿。**P0 隔离内核（M0-M6）交付完成。**
- M6 收口 · 飞书真实链路 e2e 演示（PRD §11-M6「录一版私聊→群 e2e 演示，风险展示」/ §4.1 受众越权）。环境：repo-local `.nanobot/config.json`（绕过沙箱 `~/.nanobot` 不可写，已 gitignore），deepseek-v4-flash，`multiUser.enabled=true` tenant=orgA，飞书自建应用「Lai77的nanobot」WebSocket 长连接，`allowFrom=["*"]`（配对码 `BGXN-8JA7` 已审批放行）。三步实测：
  1. **私聊落秘密**：本人私聊 bot 说「下个月要离职，保密」→ bot 调 `memory_remember({scope:"user"})` → SQLite 落库 `scope=user, owner_id=orgA:feishu:ou_36415c7d…（本人 principal）, tenant=orgA`（DB 直查确认）。
  2. **群里越权问**（受众越权硬证据）：本人+小号群里 @bot 问「下个月有什么安排」→ bot 答「没有任何已安排的日程」，`search_visible` 群语境命中 0（离职 fact 被 DM 门挡在召回前，不泄露）。
  3. **私聊自查**（不误伤）：本人私聊问同一问题 → bot 主动召回「离职」这件事。
  白盒交叉验证同一 principal + 同一 DB：`search_visible` group 命中 0 / dm 命中 1，与真实链路完全一致。DM 门在真实飞书渠道上确定性生效。
- M7 前段：FTS5/BM25 + 规模曲线（PRD §14 / P1-A）。
  - **repository BM25 排序**：`nanoscope/memory/repository.py` 加 FTS5 trigram 影子倒排表 `memories_fts`（`tokenize='trigram'` 对中文 CJK 友好，`id UNINDEXED` 只做 JOIN 键）；`add` 同步写倒排；`_build_trigram_match` 把 query 拆 3-gram OR 匹配式（<3 字返回 None）；`search_visible(query=…)` 给 query 时走「主表授权 WHERE JOIN 影子倒排 MATCH ORDER BY bm25()」，无 query/FTS 不可用/无 trigram 回退时间序。**红线**：可见性由主表 WHERE 确定性强制，FTS 仅排序——即便倒排混入他人条目也被主表 WHERE 过滤（测试 `does_not_break_dm_gate`/`does_not_leak_across_principal` 双证）。本机 SQLite 未编译 FTS5 时 `try/except OperationalError` 降级为时间序，隔离正确性不受影响。
  - **评测底座**：新建 `nanoscope/eval/`——`metrics.py`（Recall@K/MRR/nDCG@K 纯函数）、`retrieval.py`（`GrepRetriever` 新近序 + `Bm25Retriever` 相关性序，二者共用同一 trigram 候选集，唯一变量=排序方式；`rrf_fuse` 倒数秩融合）、`evaluator.py`（多 query 等权平均）、`scale_curve.py`（多规模跑 grep vs bm25 + `find_crossover` 定位拐点 + `format_curve` 文本表）、`scale_dataset.py`（程序合成冻结集，`_BURY_AT` 分阶段掩埋让 grep 阶梯下滑）。
  - **规模曲线结论**（冻结集 `data/frozen_retrieval_v1.json`，8 条 gold）：grep Recall@5 随 N=20→5000 阶梯下滑 0.750→0.625→0.500→…→0.000；BM25 全程守住 0.875（1 条 gold 因 trigram 语义盲区漏召回，正是后段 GLM 向量补的动机）；拐点 **M₀≈N=20**（grep 首次跌破 SLA 0.8）。
  - 测试 `tests/scope/test_m7_retrieval.py` 14 项全绿（metrics 取值/空 gold 约定、grep vs bm25 排序差异、RRF 融合、repository BM25 命中在前、DM 门/跨 principal 红线、无 query 回退、规模曲线阶梯下滑+拐点存在）；`tests/scope/` 累计 49 绿。
- M7 后段：向量召回 + RRF 融合（PRD §14 / P1-A.2）。
  - 新建 `nanoscope/eval/embedding.py`（`Embedder` Protocol + `GlmEmbedder`——标准库 urllib 直连 GLM `embedding-3`，凭证从**环境变量** `GLM_API_KEY`/`GLM_BASE_URL`/`GLM_EMBED_MODEL` 注入，绝不入库；支持 `dimensions` 降维，实测 1024 维）。
  - `retrieval.py` 加 `VectorRetriever`（GLM embedding + 纯 Python 余弦，不引 numpy）与 `RrfRetriever`（多路 RRF 融合，本身也是 Retriever）；`scale_curve.py` 的 `run_curve` 加可选 `embedder` 参数，传入时额外评测 vector/rrf，`format_curve` 追加两列。
  - **四路曲线结论**（真实 GLM embedding-3, 1024 维）：`grep 0.750→0.250`（阶梯失效）、`bm25 0.875`（守住，1 条词序盲区漏召）、`vector 1.000`（补盲区，如 query「橘猫豆豆」↔ 记忆「豆豆的橘猫」无共同 3-gram 但向量近邻）、`rrf 1.000`（融合取两者之长）。构成「grep 失效→上 BM25→BM25 盲区→上向量→RRF 融合」完整可证伪升级链。
  - 测试新增 4 项（`VectorRetriever` 补盲区、`RrfRetriever` 融合、`run_curve(embedder=…)`），用离线确定性 `_KeywordEmbedder`，不依赖网络/密钥、不耗额度；`tests/scope/` 累计 52 绿。**M7 交付完成。**
- M8：owner-aware Dream candidate（PRD §7 P1 / §11-M8）。恢复 Dream 蒸馏但确定性归属 owner，闭合「Dream 把 A 的私聊事实蒸馏后混入 B」的后门。新建 `nanoscope/memory/dream_candidate.py`：
  - `group_by_principal(entries)` 把 `read_unprocessed_history` 的历史按 `principal_id` **分批**（`PrincipalBatch`，保持首次出现序），**进 LLM 前就切开不混人**；无 `principal_id` 的条目 fail-closed 丢弃（不产候选）。
  - `Distiller` Protocol 是唯一 LLM 边界：`distill(principal_id, entries) -> list[str]`，每 principal 单独调（独立 prompt 不混人），**只能输出候选文本、拿不到也无权决定 owner**。
  - `distill_candidates` 产出的 `MemoryCandidate` 的 `owner` **由批次的 principal 确定性继承**（非 LLM 指定）——即便 distiller 恶意在文本里塞 `owner=<他人>` 也被忽略。
  - `commit_candidates` 用 DM 语境 `SecurityContext`（`session_key=f"dream:{principal}"`, `audience_type='dm'`）经 `Repository.add(scope='user', source_type='dream')` 落库，候选**仍受 DM 门约束**（他人查不到、本人 group 查不到、仅本人私聊召回）。`run_dream_candidates` 端到端串联。
  - 测试 `tests/scope/test_m8_dream_candidate.py` 6 项全绿：分批不混人 + 丢弃无 owner、distiller 只见单 principal 历史、owner 批次继承非 LLM 指定（`_MaliciousDistiller` 塞他人 owner 仍继承本人）、候选受 DM 门、无 owner 历史不产候选、空白输出跳过；`tests/scope/` 累计 58 绿。**M8 交付完成。**
- M9：并发有界公平准入 + 压测（PRD §15 / P1-B）。不加机器、不拆多进程、不上外部 MQ，只在单进程 asyncio 内做应用层调度优化，逐一转化基线三缺陷（①`_dispatch` 全局 FIFO `Semaphore(3)` 队头阻塞 ②无 per-principal 配额 ③inbound bus 无界无背压）。
  - **调度器**：新建 `nanoscope/concurrency/admission.py`——`BaselineGate`（改前基线，裸 FIFO Semaphore，永不拒绝）与 `FairAdmissionController`（改后，`AdmissionController` Protocol 实现）。后者三机制：全局有界 admission 队列（等待者超 `max_queue` → 抛 `AdmissionRejectedError` 优雅拒绝，解③）；per-principal in-flight 上限（单人最多 `per_principal_limit` 在飞，解②）；**least-in-flight 公平出队**（`_pick_next_waiter` 释放名额时优先唤醒当前占用最少的 principal，同 principal 内 seq FIFO，解①②）。CancelledError 时从等待队列摘除并 `_pump`。
  - **分析器**：新建 `nanoscope/eval/load.py`——`TurnRecord`（queue_wait/service_time 派生属性）、`percentile`（线性插值分位数）、`jain_index`（`(Σx)²/(n·Σx²)`）、`analyze`（出 `LoadReport`：p95/p99/max + Jain 双变量 + rejected + throughput）、`format_ab`（改前 vs 改后对比表）。
  - **压测 harness**：新建 `nanoscope/eval/loadtest.py`——**Mock LLM（确定性 `asyncio.sleep` 隔离网络抖动，PRD §15.5 红线）** + 5 用例 workload（U1 跨 session 爬坡找拐点 / U2 同 session 洪水 / U3★ 慢会话隔离 / U4 混合负载公平 / U5 洪峰过载）+ `collect_records`（返回原始 records 供 per-principal 拆分）+ `run_case_ab`（同 workload 在 BaselineGate 与 FairAdmissionController 下各跑一遍）。
  - **生产接线**：`config/schema.py` 的 `MultiUserConfig` 加旋钮 `perPrincipalLimit`（默认 1）/`admissionMaxQueue`（默认 128）；`agent/loop.py` 在 `multi_user.enabled` 且 `NANOBOT_MAX_CONCURRENT_REQUESTS>0` 时建 `FairAdmissionController` 替换基线 gate，`_dispatch` 用 `principal_id`（跨 session 同一人共享配额）为准入键，捕获 `AdmissionRejectedError` 记 warning 并置 idle；`enabled=false` 行为与基线完全一致。
  - **实测结论（Mock LLM，A/B 单机同 workload）**：U3 快会话 queue_wait p95 **↓42%**（0.427→0.247s，慢会话不再拖垮快会话）；U4 普通用户 p95 **↓56%**（0.438→0.192s，刷屏 hog 被限流）；U5 有界优雅拒绝 165 个、入场 p99 **↓83%**（3.32→0.56s，资源有上限）；U1 实测拐点 **M≈20~24**（SLA p99≤0.3s），改后均质负载无回退。**诚实性洞察**：U3/U4 聚合 p99 因把被限流的霸占者自身等待算进来反而更高，per-principal 拆分才是公平真信号；U4 `Jain(service_time)` 近似不变（调度改时序不改总量），真信号在 per-principal 排队延迟。
  - **测试报告**：新建 [M9_LOADTEST_REPORT.md](./M9_LOADTEST_REPORT.md)（8 节自包含：背景与三缺陷 / 测试环境 / 方法论含 Mock LLM 前提+A/B+指标 / U1-U5 用例 / 真实结果数据 / 结论 / 答辩口径 / 复现步骤）。
  - 测试 `tests/scope/test_m9_admission.py` 12 项全绿（控制器不变量：全局上限不越界、per-principal 防霸占、有界队列满则拒、least-in-flight 公平出队、baseline FIFO 无界；分析器数学：Jain 边界、分位数插值、拒绝计数；U1-U5 A/B 可证伪断言）；`tests/scope/` 累计 **70 绿**。**M9 交付完成。**
- M10：统一 A/B 报告 · 证据平面收口（PRD §11-M10 / §13）。不新增功能，把 P0/P1 三条证据线的"改前 vs 改后"汇成一份可交付的一体化 A/B。新建 `nanoscope/eval/reporter.py`：
  - **只做聚合 + 渲染，不重复实现任何指标逻辑**——三线取数全复用既有模块：隔离用 `dataset` 冻结攻击集 + `ContextBuilder` + `Repository.search_visible`；召回用 M7 `scale_curve.run_curve`；并发用 M9 `loadtest.run_case_ab`（`CASES` 注册表 U1-U5，U5 用 `max_queue=32` 触发有界拒绝）。保证报告数据与 `tests/scope` 各里程碑单测**同源、可复现**。
  - `collect_isolation` 双路径对照：基线把私聊事实写全局 `MEMORY.md` → 基线 `ContextBuilder` 无条件注入 → 每条攻击命中他人 canary；隔离态经 user-scope 落 SQLite + 攻击者 `SecurityContext` 走 `search_visible` → 授权 WHERE + DM 门命中 0。`build_report` 串三线出 `UnifiedReport`；`render_html` 出**自包含 HTML**（内联 CSS 无外部依赖，飞书/邮件/浏览器直接可见，红/绿高亮跌破 SLA/泄露命中）；`generate` 落盘。
  - **实测结论（单次运行）**：隔离 `forbidden_prompt_exposure` 基线 **9 → 隔离态 0**（M0 >0 的最终翻转收口）；召回 grep Recall@5 随 N=50→5000 **0.625→0.000**、BM25 全程守 **0.875**（此冻结检索集拐点 M₀=50，与 M7 合成集 M₀≈20 是不同语料，方向一致）；并发 U5 洪峰入场 p99 **3.32→0.56s** + 有界拒绝 165。**诚实性**：U2/U3/U4 聚合 p95/p99 改后反而更高（被限流的霸占者自身等待算进聚合），公平真信号在 per-principal 拆分（见 M9 报告），本统一报告保留聚合原始数不美化。
  - **报告**：新建 [M10_UNIFIED_REPORT.md](./M10_UNIFIED_REPORT.md)（7 节：背景 / 环境 / 结构与采集方法 / 三线真实数据 / 结论 / 答辩口径 / 复现）+ 产物 `M10_UNIFIED_REPORT.html`。
  - 测试 `tests/scope/test_m10_reporter.py` 5 项全绿（隔离翻转为 0、U1-U5 覆盖 + U5 有界拒绝、三线携带、HTML 自包含含三段小节+翻转结论、写文件）；`tests/scope/` 累计 **75 绿**。**M10 交付完成 —— M0~M10 全部收口，NanoScope 魔改主线完成。**
