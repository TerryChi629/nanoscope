# NanoScope Review Fix 施工进展

> 施工依据：[FIX_PLAN.md](./FIX_PLAN.md)
> 状态：已完成。每一项记录实现、专项测试、相关回归和剩余风险。

## 状态表

| 项目 | 状态 | 专项测试 | 相关回归 |
|---|---|---|---|
| 方案复审 | 已完成 | 不适用 | 不适用 |
| A1 RAG 分区租户化 | 已完成 | 25 passed | Ruff 通过 |
| A2 system/subagent 安全上下文传播 | 已完成 | 72 passed | Ruff 通过 |
| B1 history 归属字段全链路 | 已完成 | 72 passed | Ruff 通过 |
| B2 Little's Law 窗口积分 | 部分完成 | 9 passed | 终审确认仍非独立物理量 |
| B3 多种子规模曲线 | 已完成 | 8 passed | Ruff 通过 |
| B4 评测口径与报告 | 已完成 | 36 passed | 报告重生成 |
| C1 打包完整性 | 已完成 | wheel smoke 通过 | 干净 venv 导入通过 |
| C2 正式入口复用 HNSW | 已完成 | 26 passed | Ruff 通过 |
| D1 mixed archive + audience namespace | 已完成 | 修复前 2 failed，修复后通过 | M1/M11 回归通过 |
| D2 subagent 工具上下文 | 已完成 | 修复前 KeyError，修复后通过 | agent/subagent 回归通过 |
| D3 索引 revision 自动刷新 | 已完成 | 修复前漏召回，修复后通过 | M18 回归通过 |
| D4 终审归档摘要归属 | 已完成 | 修复前 2 failed，修复后 53 passed | 安全 P1 清零 |
| 全量验收 | 已完成 | 169 scope / 5187 repo passed | compileall / Ruff / wheel 通过 |
| 学习 HTML 更新 | 已完成 | 标签配对/占位符校验通过 | 不适用 |

## 方案复审记录

- 确认 `PartitionedSearcher` 的全库分区缺少 tenant 命名空间。
- 确认 system/subagent 回注没有继承父 turn 的安全上下文。
- 修正原方案：安全上下文必须从父 `RequestContext` 端到端传播，不能在 system
  消息末端重新解析。
- 修正原方案：租户结果校验使用显式 fail-closed 异常，不使用可被优化关闭的 `assert`。
- 确认 Little's Law 当前两侧为同一代数表达式。
- 确认规模曲线 seed 只改变无关编号，不能提供真实 CI。
- 确认 wheel/sdist 未包含 `nanoscope`。
- 确认 `doc_search_visible` 当前为每查询重建的暴力向量 + BM25，HNSW 未接正式入口。

## 施工日志

### A1 RAG 分区租户化

- 分区键改为结构化 `(tenant_id, scope, owner_or_acl)`，避免同名 ACL 跨租户合并。
- 审计键输出 tenant/scope，结果返回前执行显式租户 fail-closed 检查。
- 新增双租户同名 `finance` 负向测试；修复前稳定返回 tenant-b chunk，修复后归零。
- 验证：`pytest test_m18_doc_rag.py test_m18_ann_sweep.py -q` -> `25 passed`；
  相关 Ruff 全绿。

### A2 system/subagent 安全上下文传播

- `RequestContext` 补齐 `audience_id/roles`。
- 父 turn 的安全快照经 `SpawnTool -> SubagentManager -> announce` 原样传播。
- `_process_system_message` 只信任内部 subagent result 的安全 metadata；tenant 不一致直接拒绝。
- 多用户 `ContextBuilder` 即使漏传 scoped memory 也不再回退全局 `MEMORY.md`。
- 新增 spawn 传参与 subagent 内部上下文测试。

### B1 history 归属字段全链路

- 用户 session history 补存 audience 归属。
- `raw_archive` 支持完整归属；consolidation 仅在被归档 user messages 归属唯一时继承，
  混合归属继续 fail-closed，不错误认领。
- 验证：M11 + subagent + consolidation 等相关测试合计 `72 passed`，Ruff 全绿。

### B2 Little's Law 窗口积分

- `L_predicted` 保持 `lambda * W`。
- `L_measured` 改为对观测窗口内的系统人数曲线做 sweep-line 时间积分。
- 新增跨出观测窗口的记录，修复前 `L_measured=2`（错误复用），修复后裁剪为 `1`。
- 终审确认：完整观测窗内该面积仍等于 sojourn 总和，因此与 `lambda*W` 代数等价；
  只能作为窗口裁剪/一致性重算，不能宣称独立物理验证。
- 验证：M16 专项 `9 passed`，Ruff 全绿。

### B3 多种子规模曲线

- 干扰数改为随机舍入，严格保持 `E[count]=lambda_t*N`，波动不超过 1。
- look-alike frame 按 seed 随机选择；同 seed 仍逐字节可复现。
- 首轮在 `N=500` 的 Recall 阶梯平坦区仍为零方差；改用预扫描确认的临界规模
  `N=1500`，得到真实非零标准差，而非放宽断言。
- 验证：M15 专项 `8 passed`，Ruff 全绿。

### B4 评测口径与报告

- 注入字段与报告改为“静态探针残留”，明确不代表模型执行或越权成功率。
- RAG 报告把 pre-filter/partitioned 正确方案与 post-filter 反面对照分开。
- Jain(completed_turns) 明确降级为描述性指标，公平证据指向 per-principal queue-wait。
- 相关测试 `36 passed`，Ruff 全绿；统一 HTML 报告已从生成器重新生成并检查关键措辞。

### C1 打包完整性

- Hatch wheel/sdist 纳入 `nanoscope`，coverage source 同步覆盖两个包。
- 使用 PEP 517 build isolation 成功构建 `nanobot_ai-0.2.2` wheel。
- 使用 Python 3.11 干净 venv 安装 wheel 及声明依赖，从仓库外成功导入
  `nanoscope.rag.search` 与 `PartitionedSearcher`。

### C2 正式入口复用 HNSW

- `doc_search_visible` 新增可选的预构建 `PartitionedSearcher` 向量路。
- 未提供时保持 pre-filter 暴力基线；提供时复用分区 HNSW/暴力索引，不在查询中重建。
- ANN 结果与 SQL `visible_chunks` 求交，错误或陈旧索引只能降低 recall，不能扩大权限。
- 计数 embedder 证明查询只嵌入 query（batch `[1]`），不再重嵌入全部文档。
- 验证：M18 专项 `26 passed`，Ruff 全绿。

### D1 mixed archive + audience namespace

- `_history_ownership` 改为检查全部 user message：DM 要求完整且同一 owner；
  group/thread 要求完整且同一 audience，允许同群多成员共享归档。
- group/thread audience 改为 `tenant:channel:type:raw_id`，消除同租户 Feishu/Slack
  使用相同 chat ID 时的碰撞。
- 两个负向测试在修复前分别稳定暴露“legacy 数据被 Alice 认领”和“Slack 读取 Feishu 群历史”，
  修复后均 fail-closed。

### D2 subagent 工具上下文

- `_process_system_message` 从已验证的内部 `SecurityContext` 构造完整 `RequestContext`，
  并显式传入 `_run_agent_loop`。
- prompt 隔离链和工具上下文链现在共享同一可信快照；memory tool 与二级 spawn 不再丢
  tenant/principal/audience/roles。
- 修复前测试因不存在 `request_context` 失败；修复后完整字段断言通过。

### D3 索引 revision 自动刷新

- `ChunkStore` 增加 SQLite 持久 `chunks_revision`；chunk 写入与 revision 自增同事务提交。
- `PartitionedSearcher` 查询前检查 revision，变化时在 `RLock` 内重建分区；构建期间若 revision
  再变化则重试，稳定后原子替换分区快照。
- 测试证明“先建 searcher、后 add_chunk”可在下一次查询召回新 chunk；相同 revision 的下一次
  查询只嵌入 query，不重复建图。

### D4 终审归档摘要归属

- 终审发现 idle compaction 会摘要“删除前缀 + 保留后缀”，旧实现却只按删除前缀定 owner；
  A 前缀与 B 后缀可被共同摘要后错误标成 A。
- `Consolidator.archive` 现在按实际 `summary_messages` 计算归属；混合 DM 主体的摘要保持无归属。
- 群归档按 audience 聚合，避免正常多人群因 principal 不同被 fail-closed 永久丢弃。
- 两个缺陷测试修复前 `2 failed`，修复后 consolidation + M11 回归 `53 passed`。

## 最终验收

- 缺陷探测测试：修复前 `4 failed`；修复后含 identity 断言 `5 passed`。
- 受影响模块回归：`60 passed`。
- `pytest tests/scope/ -q`：`169 passed, 3 skipped`。
- 全仓（仅排除两个本机不存在全局 `python` 的既有 workspace 环境用例）：
  `5187 passed, 16 skipped, 2 deselected`。
- 两个真实签名回归在全仓首轮发现后修复，相关 context/subagent 测试 `24 passed`。
- `python -m compileall -q nanobot nanoscope` 通过。
- `ruff check nanobot/ nanoscope/ tests/scope/` 通过。
- wheel 在 Python 3.11 干净 venv 完整安装后可从仓库外导入 `nanoscope.rag.search`。
- 统一报告已重生成；学习 HTML 已更新安全传播、租户化分区、统计边界、
  wheel/HNSW 接线和最新测试数字。
