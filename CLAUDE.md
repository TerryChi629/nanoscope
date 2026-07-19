@AGENTS.md

---

# NanoScope — 项目改造指南（AI Agent 必读）

> 本文件是 **NanoScope** 二次魔改项目对 AI 编码助手的顶层指南。基线框架知识见上方 `@AGENTS.md`；本节只讲 **本项目在改什么、必须守住哪些红线**。
> 需求全文见 [`prd/nanoscope.md`](prd/nanoscope.md)（顶层背景文档，讲 why）。**任何实现决策都以 PRD 为准；本文件与 PRD 冲突时以 PRD 为准。**
> 进展与待办见 [`PROGRESS.md`](PROGRESS.md)，每完成一个任务必须同步更新。

## 一句话定位

在开源 Agent 框架 nanobot 上做魔改：**让一个实例经 IM(飞书/Slack)接入、安全地服务一个团队/社区的多用户(群 + 私聊)**。单用户框架进多用户时暴露两类内生缺陷，由一套旁路评测底座量化验证改进有效。

## 主线与优先级（不可动摇，见 PRD §0.2）

| 代号 | 内容 | 角色 |
|---|---|---|
| **B2** | 记忆系统重构：`scope` 硬隔离 + RAG 混合检索 | **承重腿 · P0（核心价值）** |
| **B3** | 单进程 asyncio 应用层并发调度：有界公平准入 | **第二数据点** |
| **B1** | 不侵入主循环的 Trace + 评测底座 | **证据平面（非主角）** |

因果链：**多用户场景 →(隐私)B2 +(争抢)B3 →(证明)B1**。这不是三个并列功能，是一条主线。承重命题是可判定布尔值 **`forbidden_hit == 0`**（跨用户零泄露）。

## 红线（违反即项目塌腰，务必守住）

1. **隔离与检索是两个正交层，绝不能用 RAG 做权限。** RAG 是概率性相关性排序（决定"注入哪些"）；scope 是确定性安全边界（决定"能不能看"）。用 RAG 决定可见范围 = 泄露灾难。
2. **scope 过滤必须发生在召回前，且收在 Repository 内部。** 统一入口 `Repository.search_visible(principal, query, top_k)`，内部强制
   `WHERE tenant_id=? AND (scope='org' OR (scope='user' AND owner_id=?))`。上层不许自己拼 WHERE，避免任一处漏拼穿墙。
3. **DB CHECK 兜底 + fail-closed。** `scope='user'` 必须有 `owner_id`；`scope='org'` 必须无 `owner_id`。缺字段写入直接失败，不留"默认可见"缝隙。
4. **四种 ID 拆成互不推导的独立字段：** `tenant_id`（配置常量）/ `principal_id`（= `channel + ":" + sender_id`，**一等字段、真正的 owner**）/ `session_key`（对话连续性，沿用现有）/ `trace_id·run_id`（B1 观测）。**绝不能拿 `session_key` 反推 owner**（群聊一个 session_key 可能对应多个 owner）。
5. **`principal_id` 必须在写 history 时就持久化**（基线 `append_history` 不存发送者），否则 Dream 事后无从追溯 owner。
6. **MVP 阶段闭合 Dream 后门（fail-closed，确定性）：** `multi_user.enabled=true` 时**禁止 Dream 写个人记忆 / 禁止改全局 `MEMORY.md`·`USER.md`**；个人记忆只经显式 `memory_remember` 工具写入（带打标）。单用户模式保持原行为不变。**MVP 不做 owner-aware 蒸馏**（那需 LLM 判 owner，违反红线 1），列 P1。
7. **owner 由运行时从 `PrincipalContext` 注入，不进工具 schema，模型无法伪造。**
8. **B1 不侵入主循环。** 质量指标经 `TraceHook` 挂 `AgentRunner` 生命周期采集；并发指标在 `AgentLoop._dispatch`（`loop.py`）另埋点（Hook 够不到 loop 层）。两路汇入同一套 trace + 报表。
9. **不做的（见 PRD §6）：** 部署层水平扩展 / 企业级多租户平台 / 跨渠道 identity 合并 / 用 RAG 做权限 / MVP 的 owner-aware Dream / 把 B3 做成生产级调度器 / 把 B1 平台化。

## 关键基线代码位置（上游 commit `d45d4ebf`，均已核实）

| 关注点 | 位置 |
|---|---|
| 全局并发闸门（默认 3，环境变量 `NANOBOT_MAX_CONCURRENT_REQUESTS`） | [loop.py:311-315](nanobot/agent/loop.py) |
| 消费者主循环 / per-session Lock / pending Queue | [loop.py:873-882, 301, 974, 305, 977](nanobot/agent/loop.py) |
| `_dispatch`（B3 埋点处） | [loop.py:964-978](nanobot/agent/loop.py) |
| Core（MEMORY/SOUL/USER）无条件全量注入 | [context.py:80-88, 166-177](nanobot/agent/context.py) |
| Dream 无 session_key 过滤（后门） | [memory.py:367-369, 482-504, 490](nanobot/agent/memory.py) |
| `append_history`（不存发送者） | [memory.py:239-290](nanobot/agent/memory.py) |
| 入站消息 `sender_id` / 默认 session_key | [bus/events.py:24-35](nanobot/bus/events.py) |
| 群 per-sender 隔离（框架"只做了一半"的证据） | [dingtalk.py:698-699](nanobot/channels/dingtalk.py) · [feishu.py:357](nanobot/channels/feishu.py) |
| Hook 注入点 | [nanobot.py:136,156](nanobot/nanobot.py) |

## 工作方式约定

- **分步骤、模块化推进，优先保证 MVP 核心链路（B2 隔离）稳定交付。** 每一步改动都应可被 B1 用"改前 vs 改后"数据证伪。
- 改动前先读相关基线代码，理解现有逻辑再动手。避免过度工程。
- 每完成一个任务，**立即更新 [`PROGRESS.md`](PROGRESS.md)**（进展 + todo），并与 CLAUDE.md 一起提交。
- **密钥、真实用户记忆、个人隐私数据绝不入库**（见 `.gitignore`）。

## 代码风格

Python 3.11+，asyncio；行长 100；`ruff`（E/F/I/N/W，忽略 E501）；`pytest`（`asyncio_mode = "auto"`）。测试镜像 `nanobot/` 包结构。
