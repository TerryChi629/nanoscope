@AGENTS.md

# NanoScope 施工清单 (CLAUDE.md)

> 本文件是**清单式**指导。详细需求见 [PRD.md](./PRD.md)（M0~M10）与
> [PRD_v4_hardening.md](./PRD_v4_hardening.md)（M11~M18 增量硬化）；进展见
> [PROGRESS.md](./PROGRESS.md)（M0~M10）与 [PROGRESS_v4.md](./PROGRESS_v4.md)（M11~M18）。
> 分支：`ljj/scope_v0`。LLM：deepseek v4-flash（已配置）。

## 铁律（每次改动都遵守）
- [ ] **小步快跑**：每完成一个改动立即编译 + 跑测，绿了才进下一步。
  - 编译：`source .venv/bin/activate && python -m compileall -q nanobot`
  - Lint：`python -m ruff check nanobot/`
  - 测试：`pytest tests/<file>::<test> -v`
- [ ] **隐私 gitignore**：API key / config / 个人数据**绝不入库**。
  - 配置在 `.nanobot/config.json`（已被 `.gitignore` 覆盖）。新增密钥文件先加 `.gitignore` 再写。
  - 提交前 `git status` 确认无 `config.json` / `.env` / 密钥。
- [ ] **文档随版本走**：[PRD.md](./PRD.md) 与 [PROGRESS.md](./PROGRESS.md) 跟随 git 版本更新，每个里程碑收口时同步 PROGRESS.md。
- [ ] **简洁优先**：只做 PRD 当前里程碑要求的改动，不过度设计、不加无关重构。

## 安全红线（PRD §6/§7）
- [ ] 可见性 = 确定性硬过滤（SQL WHERE + DB CHECK），**RAG 只排序、绝不决定能不能看**。
- [ ] 记忆检索唯一入口 `Repository.search_visible(ctx, ...)`，上层禁止自拼 WHERE。
- [ ] `owner_id/scope` 由运行时从 `SecurityContext` 注入，**不进工具 schema**。
- [ ] multi_user 下 Dream 禁写 `MEMORY.md` / `USER.md` / `SOUL.md` 三文件。
- [ ] 个人记忆仅 `audience_type='dm'` 时召回（DM 门）。
- [ ] **两个通道共用同一隔离墙**（PRD_v4 §M11，修 H1）：进入 system prompt 的
      **记忆通道**（`Repository.search_visible`）与 **Recent History 通道**
      （`MemoryStore.read_recent_history_for_prompt`）必须用同一套 principal/audience 谓词。
      - history 隔离谓词 `_history_visible_under_isolation` 一律 fail-closed：
        无 `principal_id` / 未知 `audience_type` 的条目在 `isolation=True` 下丢弃；
        DM 语境只见本人私聊历史；群/话题语境只见同 `audience_id` 群历史且**私聊历史绝不进群**。
      - `isolation=False`（单用户/基线）逐字节走原 session_key 分支，零回归。
- [ ] **有界准入必须端到端**（PRD_v4 §M13，修 H3）：隔离墙之外的**调度层**同样 fail-closed。
      - admission 判定必须在 session lock **之前**（`_dispatch` 用 `async with admission_cm, lock, inner_gate:`，
        admission 最外层）；单用户/无 admission 时 `admission_cm`=`nullcontext()`，等价基线（零回归）。
      - run-loop 建 task 前对**真实用户 inbound** 做 `would_reject` 非阻塞预检，队满则不建 task、直接优雅拒绝。
      - 优雅拒绝回执**只发真实用户**（`_is_real_user_inbound`）：cron / 本地触发器 / internal continuation 不发。
      - `FairAdmissionController.release` 对非法/重复 ticket 校验 + `logger.error` 并忽略（禁止静默破坏账本）；
        `acquire` 用 `get_running_loop()`（禁用已弃用的 `get_event_loop()`）。
      - 生产（`multi_user.enabled=True`）禁止无界 admission 队列：`admissionMaxQueue<=0` 须显式开
        `allowUnboundedAdmissionQueue` 逃生阀，否则 config 校验 fail-closed 拒绝启动。
- [ ] **召回记忆是不可信用户数据**（PRD_v4 §M12，修 H2）：`memory_remember` 可写入任意自然语言，
      召回后进 system prompt，必须两道防线，且诚实标注"结构化包裹只能显著降低、不能数学上消除注入"。
      - 写入侧清洗 `sanitize_memory_content`：去零宽/控制符、剥 `<|...|>` 特殊 token、剥行首
        `system:/assistant:/user:/tool:` 角色伪造、折叠连续换行、512 字上限；清洗后为空 fail-closed 拒写。
      - 读取侧 `wrap_untrusted_memory`：召回项经 `escape_memory_item`（转义 `&/</>`、去零宽/控制符、
        折叠空白）后包成 `<memory><item id="…">…</item></memory>`，令内容无法闭合数据块。
      - context 多用户 `scoped_memory` 分支套 `MEMORY_UNTRUSTED_HEADER` + `MEMORY_UNTRUSTED_CONSTRAINT`
        （明示记忆为用户数据、内含指令不得执行）；**单用户 `scoped_memory=None` 分支逐字节不变**（零回归）。
      - 抵抗率评测（`nanoscope/eval/injection.py`）改前/改后用**同一套越权探针判据** `_injection_succeeds`，
        禁止"改判据造收益"；结构型注入预期改后 0 残留，纯自然语言注入诚实标注非零残留。

## 里程碑进度（P0=M0~M6 必交付，P1=M7~M10）
详见 PRD.md §11。当前状态见 [PROGRESS.md](./PROGRESS.md)。

- [x] M0 冻结基线 + 评测骨架（白盒复现泄露）
- [x] M1 SecurityContext + principal_id 持久化
- [x] M2 SQLite memories 表 + Repository 单入口
- [x] M3 memory_remember 写入工具
- [x] M4 检索注入替换全量注入
- [x] M5 闭合 Dream 后门
- [x] M6 隔离验收（forbidden_prompt_exposure=0）
- [x] M7 FTS5/BM25 + 向量(GLM) + RRF + 规模曲线（拐点 M₀≈20，四路曲线；GLM 凭证走环境变量 GLM_API_KEY）
- [x] M8 owner-aware Dream candidate（按 principal 分批独立 prompt 不混人；owner 批次确定性继承非 LLM 指定；候选仍受 DM 门）
- [x] M9 并发有界公平准入 + 压测（FairAdmissionController：有界 admission + per-principal 配额 + least-in-flight 公平出队；U1-U5 A/B 实测三缺陷可证伪；压测报告 M9_LOADTEST_REPORT.md；累计 70 测试绿）
- [x] M10 统一 A/B 报告（Reporter 聚合三线改前 vs 改后一体化自包含 HTML；exposure 9→0 / grep→0 bm25 守 0.875 / U5 p99 3.32→0.56s；报告 M10_UNIFIED_REPORT.md；累计 75 测试绿 —— M0~M10 全部完成）

### v4 增量硬化（PRD_v4，M11~M18；进展见 [PROGRESS_v4.md](./PROGRESS_v4.md)）
- [x] M11 Recent History 的 principal/audience 隔离（修 H1，P0-blocker）：history 通道加
      fail-closed 隔离谓词 `_history_visible_under_isolation`；`append_history` 补存
      `audience_type/audience_id`；context/loop 透传 SecurityContext；`collect_isolation`
      扩展为记忆+history 双通道 A/B（history exposure 改前 9→改后 0）；累计 82 测试绿。
      代码：`nanobot/agent/memory.py`、`nanobot/agent/context.py`、`nanobot/agent/loop.py`、
      `nanoscope/eval/reporter.py`；测试：`tests/scope/test_m11_recent_history_isolation.py`。
- [x] M13 端到端有界准入（修 H3，P1）：admission 前移到 session lock 之前（`async with
      admission_cm, lock, inner_gate:`）；入口 `would_reject` 非阻塞预检对真实用户满则不建 task；
      `_publish_admission_reject_notice` 只回真实用户（cron/触发器/续跑不误伤），含 `retry_after`；
      `release` 校验非法/重复 ticket、`acquire` 用 `get_running_loop()`；config 护栏拒生产无界队列。
      累计 96 测试绿（scope）。代码：`nanoscope/concurrency/admission.py`、`nanobot/agent/loop.py`、
      `nanobot/agent/automation_turns.py`、`nanobot/config/schema.py`；测试：
      `tests/scope/test_m13_e2e_backpressure.py` + `tests/scope/test_m9_admission.py`（扩展 6 项）。
- [x] M12 不可信记忆 data-block 包裹 + 防注入（修 H2，P1）：新增 `nanoscope/memory/sanitize.py`
      （写入清洗 `sanitize_memory_content` + 读取转义 `escape_memory_item` + `wrap_untrusted_memory`）；
      remember_tool 写入侧接线、loop `_scoped_memory_for_message` 召回即包裹、context 套不可信头+约束
      （单用户分支零回归）；新增 `nanoscope/eval/injection.py` A/B 抵抗率评测（改前 21/100% → 改后
      5/23.8%，结构型 0 残留、纯 NL 5 残留诚实标注）。累计 108 测试绿（scope）。测试：
      `tests/scope/test_m12_memory_injection.py`（I1~I4 共 12 项）。
- [x] M14 检索质量硬化（修 H5，P1）：`VectorRetriever.search` 加 `min_score`（过滤零/负分，
      abstention）；`rrf_fuse` 确定性 tie-break `(RRF_score desc, hit_count desc, best_rank asc,
      doc_id asc)`（消除 dict 插入顺序依赖）；`GrepRetriever._trigrams` 用 `casefold()`（大小写
      不敏感）；`Bm25Retriever` 默认 `":memory:"`（除 tempfile.mktemp 泄漏）。累计 118 测试绿（scope）。
      代码：`nanoscope/eval/retrieval.py`；测试：`tests/scope/test_m14_retrieval_quality.py`（Q1~Q6 共 10 项）。
- [x] M15 规模曲线重做（修 H4，P1）：新建 `nanoscope/eval/scale_dataset_v2.py`（保留 v1 对照），
      `zipf_lambda` 主题热度衰减 + `build_at_scale_v2` 干扰数 `round(λ_t·N)` 连续增长（消除手工
      `_BURY_AT` 阈值）；`SeedStat` 多种子 mean/std/ci95；`run_curve_v2` 聚合 + gold rank 分布；
      `format_curve_v2` 带 CI 诚实标注"合成集/不写单点绝对值"。累计 125 测试绿（scope）。
      测试：`tests/scope/test_m15_scale_v2.py`（S1~S5 共 7 项）。外部脱敏验证集接入点预留待补。
- [x] M16 压测闭环 v2（PRD_v4 §M16，P1）：`load.py` 新增 `rejection_ratio`/`effective_goodput`/
      `group_percentiles`（per-principal 尾延迟拆分）/`jain_queue_wait`/`peak_concurrency`（sweep-line
      峰值在飞/排队）/`littles_law`（L=λ·W 校验）；`loadtest.py` 新增 `workload_u6_poisson`（泊松变到达）/
      `workload_u7_sustained_overload`/`RoundStat`（mean/std/ci95）/`run_case_ab_rounds`（多轮 A/B 聚合，
      同步入口内部 asyncio.run）。验收：U7 peak_queue 117(无界)→32(=max_queue 有界)、Little's Law rel_err≈0。
      累计 133 测试绿（scope）。测试：`tests/scope/test_m16_loadtest_v2.py`（L1~L5 共 8 项）；报告 §9 in `reports/M9_LOADTEST_REPORT.md`。
- [x] M17 项目门面 + 统一报告 v2（修 H6，P2）：`reporter.py` 的 `UnifiedReport` 扩为六字段
      （新增 `injection`/`backpressure`/`retrieval_v2`），`build_report` 采集六线（背压用
      `await asyncio.to_thread(collect_backpressure)` 避免嵌套 loop），新增三个渲染函数
      `_render_injection`/`_render_backpressure`/`_render_retrieval_v2`；新建 `nanoscope/eval/report.py`
      一键入口（`python -m nanoscope.eval.report`，默认写 `reports/NANOSCOPE_UNIFIED_REPORT.html`）；
      README 顶部加「🔭 NanoScope」门面区块；报告产物 `git mv` 进 `reports/`；`.gitignore` 加
      `NANOSCOPE_PROJECT_OVERVIEW.html`（用户学习文件不入库）。累计 134 测试绿（scope）。
      测试：`tests/scope/test_m10_reporter.py`（+1，共 6 项）。**红线：报告数字全部来自 tests/scope 同源代码，禁止手填。**

## 新会话开工前
1. `git rev-parse --abbrev-ref HEAD` 确认在 `ljj/scope_v0`。
2. M0~M10 读 [PROGRESS.md](./PROGRESS.md)；M11~M18 读 [PROGRESS_v4.md](./PROGRESS_v4.md)，从当前里程碑继续。
