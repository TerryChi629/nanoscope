@AGENTS.md

# NanoScope 施工清单 (CLAUDE.md)

> 本文件是**清单式**指导。详细需求见 [PRD.md](./PRD.md)，进展见 [PROGRESS.md](./PROGRESS.md)。
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

## 新会话开工前
1. `git rev-parse --abbrev-ref HEAD` 确认在 `ljj/scope_v0`。
2. 读 [PROGRESS.md](./PROGRESS.md) 找到当前里程碑，从那里继续。
