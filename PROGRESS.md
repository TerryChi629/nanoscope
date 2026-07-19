# NanoScope 进展与待办（PROGRESS）

> 本文件跟踪 NanoScope 魔改项目的进展与 todo，随开发持续更新，与代码一起提交。
> 需求见 [`prd/nanoscope.md`](prd/nanoscope.md)，规范见 [`CLAUDE.md`](CLAUDE.md)。
> 主线：**B2 记忆隔离(P0 承重腿) · B3 并发调度(第二数据点) · B1 评测底座(证据平面)**。

## 状态图例

- [ ] 待办  ·  [~] 进行中  ·  [x] 已完成

---

## 里程碑总览

| 阶段 | 目标 | 状态 |
|---|---|---|
| M0 | 项目规范就位（CLAUDE.md / PROGRESS.md / .gitignore） | [~] 进行中 |
| M1 | B2 隔离墙 MVP：principal 身份 + scope 硬过滤 + DB CHECK + Dream 后门闭合 | [ ] 待办 |
| M2 | B1 证据平面 MVP：Trace 采集 + `forbidden_hit` / Recall@K 评测 | [ ] 待办 |
| M3 | B2 检索层：BM25 / 向量 / RRF 混合检索 + 规模曲线 | [ ] 待办 |
| M4 | B3 有界公平准入 + loop 层埋点 + Jain fairness 压测 | [ ] 待办 |
| P1/P2 | owner-aware Dream 蒸馏 · scope=org 共享知识 RAG · gate 分级/令牌桶 | [ ] 待办 |

---

## 当前任务清单

### M0 · 项目规范就位
- [~] 编写 `CLAUDE.md`（定位 / 主线 / 红线 / 基线代码位置 / 工作约定）
- [~] 建立 `PROGRESS.md` 进展与 todo 跟踪
- [~] 更新 `.gitignore`：屏蔽密钥、真实用户记忆、trace/benchmark 产物
- [ ] 首次提交（CLAUDE.md + PROGRESS.md + .gitignore）

### M1 · B2 隔离墙 MVP（P0 承重腿）
- [ ] 引入 `PrincipalContext(tenant_id, principal_id)`：`IdentityResolver` 从 `InboundMessage.sender_id` 派生 `principal_id = channel + ":" + sender_id`
- [ ] `append_history` 持久化 `principal_id`（闭合 Dream 后门的前置）
- [ ] 记忆结构化语料迁到 DB，加 `tenant_id / scope / owner_id` 字段 + CHECK 约束（fail-closed）
- [ ] `Repository.search_visible(principal, query, top_k)`：内部强制 scope WHERE，上层不拼 WHERE
- [ ] 写入打标：owner 由运行时从 `PrincipalContext` 注入，不进工具 schema
- [ ] Dream 后门闭合（MVP fail-closed）：`multi_user.enabled=true` 时禁 Dream 写个人记忆 / 改全局 MEMORY·USER
- [ ] `memory_remember` 显式写入工具（带打标）
- [ ] 配置项 `multi_user.enabled`（单用户模式保持原行为）

### M2 · B1 证据平面 MVP（证据）
- [ ] `TraceHook` 挂 `AgentRunner` 生命周期，采质量指标（token / 工具调用 / 三粒度延迟）
- [ ] JSONL 落盘 + 最小 HTML 报表
- [ ] `forbidden_hit` 评测：复现 PRD §3.1 跨用户/跨渠道泄露脚本，验证隔离后 `forbidden_hit == 0`
- [ ] Recall@K / MRR / nDCG 评测脚手架

### M3 · B2 检索层
- [ ] BM25 检索
- [ ] 向量检索
- [ ] RRF 混合排序
- [ ] 规模曲线实验（grep vs BM25 vs RRF 交叉点，界定"从什么规模上 RAG"）

### M4 · B3 有界公平准入（第二数据点）
- [ ] 全局有界 admission 队列 + per-principal 最大在途 + principal round-robin + 队列满快速失败
- [ ] `loop.py._dispatch` 埋点：queue_wait / P99 / event_loop_lag / Jain fairness / rejected_total
- [ ] 压测脚本 + 改前 vs 改后 A/B，回填目标规模 N=20~30 与实测拐点 M

---

## 变更日志

| 日期 | 内容 |
|---|---|
| 2026-07-19 | 建立项目规范：CLAUDE.md / PROGRESS.md / .gitignore（M0） |
