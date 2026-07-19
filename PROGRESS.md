# NanoScope 进展 (PROGRESS.md)

> 随 git 版本更新。每个里程碑收口时在此打勾并记录关键改动/验收结果。
> 详细需求见 [PRD.md](./PRD.md)，施工清单见 [CLAUDE.md](./CLAUDE.md)。

- 分支：`ljj/scope_v0`
- LLM：deepseek v4-flash（已配置，配置文件已 gitignore）
- 上游锁定 commit：`d45d4ebf`（PRD 基线）

## 当前状态
**M6 完成（P0 收口）+ 飞书真实链路 e2e 演示通过。隔离内核五层证据齐全，`forbidden_prompt_exposure` 从基线 >0 翻转为 =0；DM 门在真实飞书渠道上验证（受众越权被挡下、本人不误伤）。下一步 M7（P1）**

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
| M7 FTS5/BM25 + 规模曲线 | P1 | ⬜ 未开始 | |
| M8 owner-aware Dream candidate | P1 | ⬜ 未开始 | |
| M9 并发有界公平准入 + 压测 | P1 | ⬜ 未开始 | |
| M10 统一 A/B 报告 | P1 | ⬜ 未开始 | |

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
