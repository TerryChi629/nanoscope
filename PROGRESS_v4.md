# NanoScope v4 增量硬化进展 (PROGRESS_v4.md)

> 承接基线：M0~M10 已交付（见 [PROGRESS.md](./PROGRESS.md)）。
> 本文件只记录 **M11~M18**（PRD_v4，增量硬化与闭环，修 H1~H6 + 可选纵深）。
> 需求：[PRD_v4_hardening.md](./PRD_v4_hardening.md)。顶层红线：[CLAUDE.md](./CLAUDE.md)。
> 分支：`ljj/scope_v0`。

## 里程碑状态表

| 里程碑 | 主题（修复项） | 优先级 | 状态 |
|---|---|---|---|
| M11 | Recent History 的 principal/audience 隔离（修 H1） | **P0** | ✅ 已完成 |
| M13 | 端到端有界准入：admission 前移 + bus 背压 + 拒绝回执（修 H3） | P1 | ⬜ 待办 |
| M12 | 不可信记忆 data-block 包裹 + 防注入（修 H2） | P1 | ⬜ 待办 |
| M14 | 检索质量硬化：min_score + tie-break + abstention（修 H5） | P1 | ⬜ 待办 |
| M15 | 规模曲线重做：Zipf 自然增长 + 多种子 CI（修 H4） | P1 | ⬜ 待办 |
| M16 | 压测闭环 v2：端到端背压证据 + 排队论 + 多目标 | P1 | ⬜ 待办 |
| M17 | 项目门面与可复现闭环 + 统一报告 v2（修 H6） | P2 | ⬜ 待办 |
| M18 | 权限感知机密文档 RAG（可选纵深，独立分支） | P2·可选 | ⬜ 待办 |

> 施工顺序（不得擅自调整）：M11 → M13 → M12 → M14 → M15 → M16 → M17，最后 M18（独立分支，不 blocking 主线）。

## TODO 清单

- [x] M11 · Recent History principal/audience 隔离（P0-blocker）
- [ ] M13 · 端到端有界准入（admission 前移 + bus 背压 + 优雅拒绝回执）
- [ ] M12 · 不可信记忆 data-block 包裹 + 防注入
- [ ] M14 · 检索质量硬化（min_score + 确定性 tie-break + abstention）
- [ ] M15 · 规模曲线重做（Zipf 自然增长 + 多种子置信区间）
- [ ] M16 · 压测闭环 v2（端到端背压证据 + 排队论 + 多目标）
- [ ] M17 · 项目门面与可复现闭环 + 统一报告 v2
- [ ] M18 · 权限感知机密文档 RAG（可选纵深，独立分支）

---

## M11 · Recent History 的 principal/audience 隔离（修 H1，P0-blocker）✅

**动机**：M4/M6 已堵死「记忆」通道（`Repository.search_visible`），但进入 system prompt 的
第二条通道——「最近对话历史」（`read_recent_history_for_prompt`）此前仅按 `session_key` 过滤，
且 `unified_session=True` 时引入跨 session 历史，是 M6 验收盲区：A 的私聊敏感发言可经 Recent
History 进入 B 的 prompt，绕过隔离墙。承重命题 `forbidden_prompt_exposure == 0` 之前只覆盖记忆
通道，未覆盖 history 通道。

**关键改动**：
- `nanobot/agent/memory.py`：
  - `append_history` 新增 `audience_type` / `audience_id` 可选参数，有值才补存（写入侧 audience 归属）。
  - `read_recent_history_for_prompt` 新增 `principal_id` / `audience_type` / `audience_id` / `isolation=False`；
    `isolation=True` 时走新谓词，`isolation=False` 逐字节走原 session_key / unified 分支。
  - 新增静态方法 `_history_visible_under_isolation`（fail-closed）：无 `principal_id` 丢弃；
    DM 语境仅本人私聊；群/话题语境仅同 `audience_id` 群历史且排除所有私聊；未知 audience_type 丢弃。
- `nanobot/agent/context.py`：`build_system_prompt` / `build_messages` 新增
  `memory_isolation` / `history_principal_id` / `history_audience_type` / `history_audience_id` 透传参数。
- `nanobot/agent/loop.py`：`_build_initial_messages` 复用 `resolve_security_context`，把
  SecurityContext 的 principal/audience 透传给 history 通道（与 M4 `_scoped_memory_for_message` 同源）。
- `nanoscope/eval/reporter.py`：`IsolationReport` 扩展 `history_baseline_exposure` /
  `history_isolated_exposure`；新增 `_collect_history_isolation`，`collect_isolation` 采集升级为
  「记忆通道 + history 通道」双通道 A/B；HTML 隔离段渲染双通道行（PRD_v4 §M11.4 完成信号）。

**验收数字**（来自 `tests/scope` 同源可复现代码，`nanoscope.eval.reporter.collect_isolation`）：

| 通道 | 攻击条数 | 改前 baseline exposure | 改后 nanoscope exposure |
|---|---|---|---|
| 记忆通道（M4/M6） | 3 | 9 | 0 |
| Recent History 通道（M11） | 3 | 9 | 0 |

- 验收测试 `tests/scope/test_m11_recent_history_isolation.py`：R1~R6 共 7 项全绿。
  - R6 复现→修复→翻转闭环：`test_r6_history_channel_leaks_before_fix`（改前 isolation=False，exposure>0，可证伪）
    + `test_r6_history_channel_isolation_flips_to_zero`（改后经 ContextBuilder prompt，exposure==0）。
- 单用户零回归：`isolation=False` 走原分支，`tests/agent/test_memory_store.py` 全绿。
- `tests/scope` 累计：75 → **82 passed**；`ruff check` 相关文件全绿。

**评估记录（未扩大范围）**：`loop.py:_process_system_message`（system/subagent 消息路径）的第二个
`build_messages` 调用未加 isolation 参数——该路径处理 system/subagent 消息（非用户轮次），不注入
scoped_memory，且 M11 改动点明确限定在 `_build_initial_messages`（PRD_v4 §M11.2 第 4 条）。故保持现状，
不在此里程碑扩大范围。

**完成信号**：R1~R6 全绿；`collect_isolation` 双通道采集；报告双通道 exposure 均为 0；单用户零回归。
