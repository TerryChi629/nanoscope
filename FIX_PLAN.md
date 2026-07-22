# NanoScope Review Fix 施工方案

> 目标：修复 2026-07-21 独立代码审查确认的安全、证据可信度与交付完整性问题。
> 本文件是本轮施工的唯一依据；每一项必须先补回归测试，再修改实现并跑专项测试。

## 1. 复审结论与方案纠偏

### 1.1 确认问题

1. `PartitionedSearcher` 从 `all_chunks_unfiltered()` 建全库索引，但分区键没有
   `tenant_id`，同名 org/project 分区可跨租户合并。
2. 用户入口会传播 `SecurityContext`，system/subagent 回注入口不会；多用户模式下
   `scoped_memory=None` 仍会回退全局 `MEMORY.md`。
3. recent-history 的部分持久化/归档链只保存 `session_key` 或 `principal_id`，缺少
   `audience_type/audience_id`。
4. Little's Law 的 `L_predicted` 与 `L_measured` 是同一代数式，误差恒为零。
5. 规模曲线的 seed 只改变无关编号，不改变检索结果，多种子 CI 没有独立信息。
6. 注入指标和 post-filter 报告口径存在过度表述。
7. wheel/sdist 没有包含 `nanoscope` 包。
8. `doc_search_visible` 每次重建向量/BM25，未接入可复用的 partitioned HNSW。

### 1.2 对原方案的修正

- system/subagent 消息不能在末端重新解析身份；必须从父 turn 的不可伪造
  `RequestContext` 沿 spawn、subagent、announce metadata 显式传播安全快照。
- 分区检索返回前不用裸 `assert`（可被 `python -O` 关闭）；使用显式租户校验，
  发现不一致立即抛出安全异常。
- 多种子数据不随机修改 gold 的语义或时间来制造方差；使用基于 seed 的随机
  look-alike 采样与到达时间抖动，保持 Zipf 期望不变。
- C2 不在单次查询里创建 HNSW。正式入口只接受外部构建、可复用的
  `PartitionedSearcher`；未提供时明确走 pre-filter 暴力基线。

## 2. 不可破坏约束

- 单用户模式保持原行为；安全增强只在多用户上下文存在时生效。
- 可见性只由 SQL 授权谓词或租户化分区结构决定，排序不得扩大可见集合。
- `Repository.search_visible` 与文档 RAG 保持平行，不合并、不替换。
- 密钥、真实机密数据、用户配置不得入库。
- 每项修复执行：失败测试/负向用例 -> 实现 -> 专项测试 -> 相关回归 -> 记录进展。
- 不执行 `ruff format`；最终只运行 `ruff check`。
- 未经用户确认不 commit、不 push。

## 3. 施工步骤

### A1. RAG 分区租户化

- 修改 `PartitionedSearcher` 分区键为 `(tenant_id, scope, owner/acl_group)` 的稳定编码。
- `_visible_partition_keys(ctx)` 只生成当前 tenant 的键。
- 搜索结果增加显式 tenant fail-closed 校验。
- 新增双租户同名 org/project/user 负向测试和可证伪对照。

验收：partitioned 暴力与 HNSW 两种模式均 `forbidden_exposure == 0`。

### A2. system/subagent 安全上下文端到端传播

- 扩展 `RequestContext`：增加 `audience_id`、`roles`。
- 父 turn 创建 `RequestContext` 时写入完整 `SecurityContext`。
- `SpawnTool -> SubagentManager.spawn -> _run_subagent -> _announce_result` 原样传递快照。
- subagent 回注的 system message 使用内部 metadata 携带快照；
  `_process_system_message` 仅信任 `sender_id=subagent` 且 `injected_event=subagent_result`
  的内部消息。
- 使用该快照构造 scoped memory 与 recent history。
- `ContextBuilder.multi_user_isolation=True` 且遗漏 `scoped_memory` 时禁止回退全局记忆。

验收：subagent canary 不得看到其他 principal 的记忆/历史；单用户 prompt 不变。

### B1. history 归属字段全链路

- 用户消息持久化与 raw archive 同步保存
  `principal_id/audience_type/audience_id`。
- consolidation/file-cap 回调传播当前安全上下文。
- 缺失归属在 isolation 模式继续 fail-closed。

验收：归档前后本人可见性一致；跨 principal/audience 始终不可见。

### B2. Little's Law 窗口积分与独立采样

- `L_predicted = lambda * W` 保持完成记录统计。
- `L_measured` 从 sweep-line 事件流对系统内请求数做时间积分，先修正观测窗裁剪。
- 真正独立验证需从额外 queue/inflight gauge 采样积分，不能与 `lambda/W` 复用同一批生命周期。
- 明确 observation window 与拒绝请求口径。

验收：窗口裁剪案例正确；未接独立 gauge 前只称一致性重算，不宣称独立验证。

### B3. 多种子规模曲线

- 使用 seed 对每个主题的干扰数量做有界随机采样，期望仍为 `lambda_t * N`。
- 随机选择 look-alike frame 与时间顺序，但不改变 gold 内容与判定标准。
- 保持同 seed 可复现、不同 seed 产生真实不同语料/排名。

验收：固定 seed 逐字节一致；多 seed 至少一个规模点 `std > 0`；趋势结论仍成立。

### B4. 评测口径与报告

- “注入成功率/抵抗率”改为“结构化注入构件残留率/中和率”。
- 明确纯自然语言仅为静态探针残留，不代表模型行为成功。
- post-filter 标注为召回后过滤的反面对照，不纳入“召回前授权”正确方案。
- 公平性补充 per-principal queue wait/rejection/goodput 说明。
- 更新统一报告生成器与冻结报告。

验收：报告数字只来自采集函数；关键措辞有测试保护。

### C1. 打包完整性

- Hatch wheel/sdist include `nanoscope`。
- coverage source 同时包含 `nanobot`、`nanoscope`。
- 构建 wheel 后在隔离临时目录验证 `import nanoscope.rag.search`。

验收：源码树外可导入并运行最小 RAG smoke。

### C2. 正式入口复用 HNSW

- 为 `doc_search_visible` 增加可选、预构建的 `PartitionedSearcher` 向量路依赖。
- 未提供时继续使用 pre-filter 暴力基线，并在文档中准确标注。
- HNSW 返回结果必须与 SQL 可见集合求交，形成纵深防御。
- BM25 仍只在 SQL 可见集合中排序；RRF/rerank 不改变可见性。
- 不在 `doc_search_visible` 内管理索引生命周期；由 `PartitionedSearcher` 基于持久 revision
  在查询前检测陈旧状态并原子刷新。

验收：ANN 与暴力路径授权结果一致、0 越权；测试证明预构建索引被复用。

### D1. 最终安全收口：归档与 audience 命名空间

- `_history_ownership` 必须检查所有 user message；任一条缺完整归属则整批无归属。
  DM 要求同一 principal；group/thread 允许多成员，但必须同一 audience。
- 群/话题 audience 使用 `tenant:channel:type:raw_id`，禁止不同渠道裸 chat ID 碰撞。
- DM 可见性仍以 principal 为准，保持单用户与既有私聊语义。

验收：`[legacy user, Alice user]` 不可被 Alice 认领；Feishu/Slack 同名群不可互见。

- Consolidator 的 owner 必须从实际 `summary_messages` 推导，不能只看被删除前缀。

验收：A 前缀 + B 保留后缀的摘要不可归给 A；同群多成员归档仍对该群可见。

### D2. 最终安全收口：subagent 工具上下文

- `_process_system_message` 仅从已通过 `_internal_security_context` 校验的内部快照恢复身份。
- 同一快照同时进入 prompt 构建与 `_run_agent_loop(request_context=...)`。
- `RequestContext` 保留 channel/chat/session/runtime/workspace 与完整安全字段，支持该 turn
  的工具调用和二级 spawn 继续继承。

验收：subagent 独立续跑的工具可见父 tenant/principal/audience/roles；tenant mismatch 拒绝。

### D3. 最终安全收口：索引 revision 自动刷新

- SQLite `rag_metadata.chunks_revision` 持久记录已提交 chunk 版本。
- `add_chunk` 与 revision 自增处于同一事务。
- `PartitionedSearcher` 查询前比较 revision；变化时加锁重建，构建前后版本不一致则重试。
- revision 未变化时只嵌入 query，不重复嵌入语料。

验收：先建 searcher、后 ingestion 的新 chunk 下一次查询可召回；后续稳定查询不重复建图。

## 4. 最终验收

1. 每步专项测试全绿。
2. `pytest tests/scope/ -q` 全绿。
3. 受影响的 agent/config/package 测试全绿。
4. `ruff check nanobot/ nanoscope/ tests/scope/` 全绿。
5. `compileall` 全绿。
6. wheel 安装 smoke 全绿。
7. 统一 HTML 报告重新生成。
8. `NANOSCOPE_LEARNING_GUIDE.html` 更新安全边界、公式与新数据，结构校验通过。
