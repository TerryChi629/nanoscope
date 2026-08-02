# NanoScope PRD v5：飞书话题上下文与任务执行工具

> 版本：v1.0（已实施）
> 分支：`ljj/scope_v0`
> 基线 commit：`8007e64a`
> 依赖：NanoScope 多用户安全上下文、权限感知 RAG、Nanobot AgentRunner

## 0. 决策摘要

本需求不新增独立 Agent、不实现第二套 Agent Loop，也不替换 Nanobot 现有
`AgentLoop -> AgentRunner` 主链路。

本期在现有 Tool 扩展点新增两项飞书能力：

1. `feishu_read_thread`：按需读取当前飞书话题中、尚未进入 Agent Session History
   的原始讨论。
2. `feishu_task`：将 Agent 整理出的行动项经过用户显式确认后创建为飞书任务。

目标链路：

```text
飞书用户请求
  -> 现有 AgentRunner 决策
  -> feishu_read_thread（只读）
  -> Observation 回填
  -> 可选 document_search（现有权限感知 RAG）
  -> Agent 生成行动项
  -> feishu_task.prepare（只生成预览）
  -> 用户显式确认
  -> feishu_task.commit（创建飞书任务）
  -> Observation 回填
  -> Agent 返回任务结果
```

本期价值不在于“重新实现 Thought -> Tool Call -> Observation 循环”，而在于让现有
循环获得真实的飞书上下文读取和受控写操作能力。

### 0.1 实施结果

已按 M0 -> M5 完成代码与确定性测试：

- M0：验证 `lark-oapi>=1.5,<2` 生成契约。IM v1
  `GET /open-apis/im/v1/messages` 支持 tenant/user token，但查询容器是 chat，
  因此实现采用“受限时间窗分页 + 当前 root_id 硬过滤”；Task v2
  `POST /open-apis/task/v2/tasks` 支持 tenant/user token，`InputTask.client_token`
  可作为远端幂等键。
- M1：实现 `feishu_read_thread`，chat/root 只从 `RequestContext` 取得，跨话题/
  跨群消息在进入 Observation 前被过滤。
- M2/M3：实现 Pending Operation SQLite、精确确认协议、Principal/Audience/实例绑定、
  TTL、条件状态迁移、重试与 `client_token` 幂等。
- M4：使用真实 `AgentRunner` + Scripted Provider 完成两次 Turn 的
  read -> prepare -> confirm -> commit 轨迹。
- M5：新增 13 条专项测试；`tests/scope` 实测 259 passed、9 skipped；
  飞书/工具加载/请求上下文相关回归 168 passed；Ruff 全绿。
- 真实 E2E：`feishu_read_thread` 成功读取机器人被 @ 前的话题内容；Task v2 成功创建
  测试任务；相同 token 重复 commit 返回同一 task_id 和
  `idempotency_reused: true`，重复副作用为 0。

源码：

- `nanobot/agent/tools/feishu_actions.py`
- `nanoscope/feishu_actions/client.py`
- `nanoscope/feishu_actions/thread.py`
- `nanoscope/feishu_actions/pending_store.py`
- `tests/scope/test_feishu_action_tools.py`

### 0.2 真实 E2E 结果与线上修正

测试应用发布 `im:message.group_msg` 和最小写权限 `task:task:writeonly` 后，真实链路
完成 read -> prepare -> confirm -> commit。创建结果：

- title：`检查缓存穿透`
- assignee：`Lai77`
- due：`2026-08-02 18:00 +08:00`
- task_id：`75671f8b-e38f-42f4-8316-2cfd6178b8b9`
- 重复确认：返回相同 task_id，`idempotency_reused: true`

真实验收发现两项实现约束，并已补齐测试：

1. 机器人必须在原话题内回复预览。`replyToMessage=false` 会让确认进入新话题，
   服务端按设计返回 `AUDIENCE_MISMATCH`；本地实例配置启用
   `replyToMessage=true`。
2. 模型上下文与授权原文必须分离。Feishu Channel 为模型添加的
   `[Reply to: ...]` 不能进入精确确认比较；`InboundMessage.original_user_text`
   保存用户实际输入，`content` 继续承载引用上下文。

修复后组合回归
`tests/scope + tests/agent/test_loop_tool_context.py + tests/channels/test_feishu_reply.py`
实测 `325 passed, 9 skipped`，相关 Ruff 检查全绿。

验收 artifact 只记录任务 ID、状态和确定性结果，不保存 access token、App Secret
或群聊正文。

---

## 1. 背景与问题

### 1.1 当前系统已经具备什么

当前 NanoScope 已具备：

- 飞书私聊、群聊、话题消息接收与回复。
- 引用消息内容补充、图片/文件/语音处理。
- CardKit 流式回复与工具调用提示。
- 按飞书话题生成独立 Session Key。
- Agent Session History、上下文压缩和长期记忆。
- `AgentRunner` 的多轮 LLM、Tool Call、Observation 回填与异常恢复。
- Principal/Audience 安全上下文和权限感知 RAG。

源码锚点：

- 飞书入站解析与话题 Session：
  `nanobot/channels/feishu.py::_on_message`
- 当前请求可信上下文：
  `nanobot/agent/tools/context.py::RequestContext`
- 工具发现与注册：
  `nanobot/agent/tools/loader.py::ToolLoader`
- 工具执行循环：
  `nanobot/agent/runner.py::AgentRunner`

### 1.2 当前缺口

#### 缺口 A：Session History 不等于飞书原始话题

Session History 只保存已经进入 Agent 的消息。采用 `group_policy=mention` 时，群成员
未 @ 机器人的讨论不会进入 MessageBus，也不会进入 Session History。

因此，群成员讨论完成后第一次 @ 机器人并说“总结上面的讨论”，Agent 可能只看到：

- 当前 @ 消息；
- 当前消息直接引用的父消息；
- 以前已经触发过机器人的 Session 历史。

它无法保证看到该话题内此前未触发机器人的完整讨论。

#### 缺口 B：Agent 只能建议行动项，不能落为可追踪任务

当前 Agent 可以回复“张三周五前完成缓存修复”，但不能创建带负责人、截止时间和状态
的飞书任务。讨论结论仍停留在聊天文本中，缺少后续追踪载体。

#### 缺口 C：写工具不能直接交给模型自治执行

创建任务属于外部副作用。仅依赖 Prompt 要求模型“先确认”不构成安全边界：

- 模型可能误判用户意图；
- 检索内容可能包含诱导执行的 Prompt Injection；
- 重试或重复 Tool Call 可能创建重复任务；
- 等待确认期间，任务参数或用户权限可能变化。

因此必须在 Tool 服务端实现确定性的确认、身份绑定和幂等校验。

---

## 2. 产品目标与非目标

### 2.1 产品目标

**G1：按需补齐话题上下文**

Agent 仅在需要时调用工具读取当前飞书话题，不默认监听或持久化所有群消息。

**G2：从讨论形成行动项**

Agent 能结合话题消息和现有 `document_search` 生成任务标题、描述、负责人和截止时间。

**G3：确认后执行**

任务创建必须经过“预览 -> 用户显式确认 -> 提交”的两阶段流程。

**G4：安全边界连续**

工具调用必须复用当前 `RequestContext` 中的 channel、principal、audience 和消息元数据；
模型不能指定任意群、话题或调用者身份。

**G5：结果可审计、可复现**

保留 Tool Call、确认、飞书响应、幂等命中和失败原因，并建立确定性测试。

### 2.2 明确非目标

本期不做：

- 新 Agent、Subagent 或第二套 Agent Loop。
- 修改 `nanobot/agent/runner.py` 的 ReAct 执行语义。
- 飞书云文档编辑、日历会议、审批流和多维表格工具。
- 通用 RBAC/通用审批平台。
- 自动监听并存储群内全部消息。
- Agent 无确认批量创建任务。
- 自动修改或删除既有飞书任务。
- 用 LLM 判断用户是否完成授权。

---

## 3. 用户场景

### 3.1 场景一：话题总结

群成员在一个飞书话题中讨论故障，但此前没有 @ 机器人。讨论结束后发送：

> @NanoScope 总结这个话题的结论和未解决问题。

预期：

1. Agent 判断当前上下文不足。
2. 调用 `feishu_read_thread`。
3. 工具从可信 RequestContext 取得当前 `chat_id/root_id`，读取原始话题。
4. 原始消息作为不可信 Observation 返回。
5. Agent 输出带消息引用的总结。

### 3.2 场景二：讨论转任务

用户发送：

> @NanoScope 总结行动项，并为张三创建周五截止的修复任务。

预期：

1. Agent 调用 `feishu_read_thread` 获取讨论。
2. 必要时调用现有 `document_search` 补充设计依据。
3. Agent 调用 `feishu_task(action="prepare", ...)`。
4. 工具返回任务预览和一次性 `confirmation_token`，不产生外部副作用。
5. 用户发送精确确认文本，例如：`确认创建 FT-7K2M`。
6. Agent 调用 `feishu_task(action="commit", confirmation_token="FT-7K2M")`。
7. 工具验证原始用户消息、principal、audience、参数摘要、有效期和执行状态。
8. 验证通过后创建飞书任务，返回 task_id/task_url。

### 3.3 场景三：拒绝执行

以下情况必须拒绝创建：

- 用户只说“看起来可以”，但未携带确认令牌。
- B 用户尝试确认 A 用户生成的计划。
- 在另一个群或私聊中确认原群的计划。
- 确认令牌已过期。
- prepare 后任务参数发生变化。
- 相同计划已经成功执行。
- 当前请求不是飞书渠道或缺少可信身份。

---

## 4. 功能需求

## FR1：`feishu_read_thread`

### FR1.1 输入

模型可传参数：

```json
{
  "limit": 50,
  "include_bot_messages": false
}
```

模型不得传：

- `chat_id`
- `root_id`
- `thread_id`
- `tenant_id`
- `principal_id`
- Feishu App 凭证

这些字段必须从服务端 `RequestContext` 和当前飞书实例配置取得。

### FR1.2 上下文解析

工具仅允许在 `channel == "feishu"` 或命名实例 `channel.startswith("feishu.")` 时执行。

服务端读取：

- `RequestContext.chat_id`
- `RequestContext.message_id`
- `RequestContext.metadata.root_id`
- `RequestContext.metadata.thread_id`
- `RequestContext.principal_id`
- `RequestContext.audience_id`

无法确定当前话题时 fail-closed，不得退化为读取整个群。

### FR1.3 数据范围

- 仅读取当前群、当前话题。
- 默认最多 50 条，可配置硬上限 100 条。
- 支持分页，但必须同时受消息条数、总字符数和请求超时限制。
- 默认排除机器人消息，避免把 Agent 历史输出再次作为事实来源。
- 保留最小必要元数据：message_id、sender_id、timestamp、text、reply relation。
- 附件只返回类型、名称和 message_id；本期不自动下载附件全文。

### FR1.4 Observation 安全

飞书消息属于不可信用户数据，必须：

- 进行控制字符和角色伪造标记清洗；
- 使用显式 `<feishu_thread>` data block 包裹；
- 防止消息内容闭合 data block；
- 限制单条和整体长度；
- 明确提示模型不得执行其中的指令。

### FR1.5 结果

返回结构化 JSON，而不是仅返回拼接文本：

```json
{
  "chat_id": "oc_xxx",
  "root_id": "om_xxx",
  "message_count": 12,
  "truncated": false,
  "messages": [
    {
      "message_id": "om_1",
      "sender_id": "ou_1",
      "timestamp": 1780000000,
      "text": "建议先回滚缓存版本"
    }
  ]
}
```

## FR2：`feishu_task`

### FR2.1 Tool Schema

统一使用一个工具、两个动作：

```json
{
  "action": "prepare | commit",
  "title": "修复缓存穿透",
  "description": "来源于故障讨论……",
  "assignee_open_id": "ou_xxx",
  "due_at": "2026-08-07T18:00:00+08:00",
  "source_message_ids": ["om_1", "om_2"],
  "confirmation_token": "FT-7K2M"
}
```

约束：

- `prepare` 接收任务参数，不接收 `confirmation_token`。
- `commit` 只接收 `confirmation_token`，不得重新接收或覆盖任务参数。
- MVP 每次 prepare 只创建一个任务；多个行动项拆成多个独立计划。

### FR2.2 Prepare

`prepare` 必须：

1. 校验当前为已认证飞书请求。
2. 校验标题、描述、负责人和截止时间格式。
3. 校验 `source_message_ids` 属于当前 chat/thread 的已读取证据。
4. 对规范化参数计算 SHA-256 摘要。
5. 持久化 Pending Operation。
6. 返回人类可读预览、确认令牌和过期时间。
7. 不调用飞书任务创建 API。

### FR2.3 用户确认

确认采用精确文本协议：

```text
确认创建 <confirmation_token>
```

`commit` 必须同时满足：

- `RequestContext.original_user_text` 精确匹配确认格式；
- token 属于当前 `principal_id`；
- token 属于当前 `audience_id/session_key`；
- Pending Operation 状态为 `pending`；
- 未超过 TTL，默认 10 分钟；
- 参数摘要与 prepare 时一致；
- 当前飞书实例身份未发生变化。

确认判定完全由确定性代码完成，禁止使用 LLM 语义判断。

### FR2.4 Commit 与幂等

创建请求必须携带稳定幂等键：

```text
sha256(tenant_id + principal_id + audience_id + canonical_task_payload)
```

Pending Operation 至少具有以下状态：

```text
pending -> executing -> succeeded
                    \-> failed_retryable
                    \-> failed_terminal
pending -> expired
```

规则：

- 同一 token 重复 commit：成功过则返回原 task_id，不重复创建。
- 网络超时且无法确认飞书是否已创建：标记 `failed_retryable`，重试前先按幂等信息查询。
- 参数、身份或 audience 不匹配：`failed_terminal` 或直接拒绝，不能自动修正。
- MVP 不做自动补偿删除。

### FR2.5 结果

成功返回：

```json
{
  "status": "succeeded",
  "task_id": "xxx",
  "task_url": "https://...",
  "idempotency_reused": false
}
```

失败返回结构化错误码，例如：

- `FEISHU_CONTEXT_REQUIRED`
- `THREAD_CONTEXT_MISSING`
- `CONFIRMATION_REQUIRED`
- `CONFIRMATION_EXPIRED`
- `PRINCIPAL_MISMATCH`
- `AUDIENCE_MISMATCH`
- `PAYLOAD_MISMATCH`
- `FEISHU_PERMISSION_DENIED`
- `FEISHU_API_UNAVAILABLE`

---

## 5. 安全模型

### 5.1 信任边界

可信：

- Channel 鉴权后的 `sender_id`
- NanoScope 生成的 `principal_id/audience_id`
- `RequestContext.original_user_text`
- 服务端保存的 Pending Operation

不可信：

- 用户消息正文
- 飞书话题历史正文
- RAG 召回内容
- LLM 生成的工具参数
- LLM 声称“用户已经确认”

### 5.2 必须保持的安全不变量

**S1：模型不能选择读取目标**

话题工具的 chat/root 必须来自当前请求上下文。

**S2：读取范围不越过当前 audience**

当前群/话题之外的消息不得进入 Observation。

**S3：个人记忆不因话题读取进入群**

本功能不改变现有 Principal/Audience 记忆谓词。

**S4：无显式确认不产生副作用**

任意 Prompt、RAG 文档或 Thread 消息都不能代替用户确认。

**S5：确认不可转移**

token 与 principal、audience、channel instance 和 payload digest 绑定。

**S6：重复执行至多产生一个任务**

重试、模型重复调用和进程恢复不得重复创建。

**S7：缺字段和异常 fail-closed**

缺身份、缺话题、状态未知或权限变化时拒绝，而不是扩大读取或直接创建。

---

## 6. 技术方案

### 6.1 架构边界

```text
nanobot/channels/feishu.py
  只负责消息收发和可信元数据采集

nanobot/agent/tools/feishu.py
  Tool Schema、RequestContext 校验、错误映射

nanoscope/feishu/client.py
  Feishu OpenAPI 适配、token 缓存、超时与重试

nanoscope/feishu/thread.py
  话题读取、范围过滤、内容清洗

nanoscope/feishu/pending_store.py
  Pending Operation、确认、状态机、幂等账本

nanoscope/feishu/task.py
  任务 prepare/commit 领域逻辑
```

`agent/loop.py` 和 `agent/runner.py` 原则上零改动。只有在现有 RequestContext 缺失
必要的、可由 Channel 确定性提供的字段时，才允许增加最小透传字段。

### 6.2 配置

在 `ToolsConfig` 增加显式配置：

```json
{
  "tools": {
    "feishu": {
      "enabled": false,
      "readThreadEnabled": true,
      "taskEnabled": false,
      "threadMessageLimit": 50,
      "threadMaxChars": 30000,
      "approvalTtlSeconds": 600,
      "requestTimeoutSeconds": 10
    }
  }
}
```

规则：

- 默认整体关闭。
- 只读工具和写工具分别开关。
- 凭证复用当前请求对应的飞书 Channel Instance，不新增第二份 App Secret 配置。
- 命名实例（如 `feishu.product`）必须选择同名实例，禁止回退到默认实例。

### 6.3 OpenAPI 前置验证

M0 Spike 已完成，结论如下：

1. IM v1 只能按 `container_id_type=chat` 分页；返回消息含 `root_id/thread_id`，
   因此可在服务端按当前 `root_id` 硬过滤。本实现限定 lookback、最大页数、消息数和字符数。
2. IM v1 与 Task v2 的 SDK 请求契约均声明支持 tenant/user token；MVP 复用当前
   Feishu App 的 tenant token。
3. Task v2 使用 `user_id_type=open_id`，负责人写入
   `members=[{id,type:"user",role:"assignee"}]`，响应返回 task `guid/url`。
4. Task v2 `InputTask.client_token` 是原生客户端幂等字段；实现传入持久化
   `idempotency_key`，429/5xx 重试保持相同值。
5. 国内飞书使用 `https://open.feishu.cn`，Lark 使用
   `https://open.larksuite.com`，由当前命名 Channel Instance 的 domain 决定。

边界：SDK 契约证明 tenant token 可调用，不等于任意部署都已获批所需 OpenAPI
scope。本次测试应用已通过版本发布获得消息读取与最小任务写权限；其他部署权限不足时
工具仍稳定返回 `FEISHU_PERMISSION_DENIED`，不得退化为伪造用户身份。

---

## 7. 数据模型

Pending Operation 建议使用 SQLite：

```sql
CREATE TABLE feishu_pending_operations (
  id                  TEXT PRIMARY KEY,
  token_hash          TEXT NOT NULL UNIQUE,
  tenant_id           TEXT NOT NULL,
  principal_id        TEXT NOT NULL,
  audience_id         TEXT NOT NULL,
  channel_instance    TEXT NOT NULL,
  operation_type      TEXT NOT NULL CHECK (operation_type = 'create_task'),
  payload_json        TEXT NOT NULL,
  payload_digest      TEXT NOT NULL,
  idempotency_key     TEXT NOT NULL UNIQUE,
  status              TEXT NOT NULL,
  remote_task_id      TEXT,
  remote_task_url     TEXT,
  error_code          TEXT,
  created_at          INTEGER NOT NULL,
  expires_at          INTEGER NOT NULL,
  updated_at          INTEGER NOT NULL
);
```

要求：

- token 只保存 hash，不明文落库。
- `payload_json` 使用 canonical JSON。
- 数据库写入采用事务；状态迁移使用条件更新，防并发双提交。
- 日志不得输出 App Secret、access token 或完整 confirmation token。

---

## 8. 里程碑

### M0：Feishu API Spike

交付：

- 话题读取与任务创建 API 可行性记录。
- 权限清单和 token 类型。
- 国内飞书/Lark 差异。
- Task API 不可行时的降级决策。

完成标准：

- 使用测试应用完成最小 API 调用，保留脱敏请求/响应样例。
- PRD 中所有 API 假设被源码或真实响应替换。

### M1：只读话题工具

交付：

- Feishu API Client。
- `feishu_read_thread`。
- RequestContext 范围绑定、分页、截断和不可信数据包裹。

完成标准：

- 单元测试与 Mock OpenAPI 集成测试通过。
- 在真实飞书话题中完成一次“未提前 @ 机器人 -> 最后 @ 总结”的演示。

### M2：任务 Prepare 与 Pending Store

交付：

- `feishu_task(action="prepare")`。
- Pending Operation SQLite。
- 参数规范化、摘要、token、TTL 和预览。

完成标准：

- prepare 过程对飞书零写调用。
- 重启后 pending 状态可恢复。

### M3：确认、Commit 与幂等

交付：

- 精确确认协议。
- principal/audience/payload 校验。
- 飞书任务创建。
- 条件状态迁移和重复提交复用。

完成标准：

- 未确认创建数为 0。
- 同一操作重复提交创建数为 1。
- 跨用户、跨群、过期 token 全部拒绝。

### M4：Agent 链路与提示契约

交付：

- 收紧工具 description，明确何时读取话题、何时 prepare/commit。
- 使用 Stub Provider 完成真实 `AgentRunner` 多轮工具轨迹：

```text
LLM -> feishu_read_thread -> Observation
LLM -> document_search（可选）-> Observation
LLM -> feishu_task.prepare -> Observation
用户确认
LLM -> feishu_task.commit -> Observation
LLM -> 最终回答
```

完成标准：

- 不修改 AgentRunner 也能完成全链路。
- Trace 中 Tool Call、Observation 与最终任务 ID 对齐。

### M5：评测、文档与真实演示

交付：

- 确定性评测脚本与 artifacts。
- 配置和权限文档。
- 脱敏飞书演示记录。
- `PROGRESS_v5.md`。

完成标准：

- §9 验收矩阵全部通过。
- 全量 `tests/scope` 和相关 Agent/Feishu 回归通过。
- 真实飞书 read/prepare/commit 成功，重复 commit 不创建第二条任务。

---

## 9. 验收矩阵

### 9.1 话题读取

| 编号 | 用例 | 预期 |
|---|---|---|
| R1 | 当前话题 12 条消息，只有最后一条 @ bot | 返回此前 11 条有效消息 |
| R2 | 模型参数伪造 chat_id/root_id | Schema 不接收或服务端忽略并拒绝 |
| R3 | 当前请求无 root/thread | fail-closed，不读取整个群 |
| R4 | 话题包含 Prompt Injection | 作为不可信数据返回，不改变工具权限 |
| R5 | 超过条数/字符上限 | 确定性截断并标记 `truncated=true` |
| R6 | 命名飞书实例 | 使用对应实例凭证，不回退默认实例 |

### 9.2 任务执行

| 编号 | 用例 | 预期 |
|---|---|---|
| W1 | prepare | 仅生成预览和 token，飞书写调用为 0 |
| W2 | 无 token commit | 拒绝 |
| W3 | 原始消息未精确确认 | 拒绝 |
| W4 | B 确认 A 的 token | 拒绝 |
| W5 | 另一个群确认 | 拒绝 |
| W6 | token 过期 | 拒绝 |
| W7 | payload 被篡改 | 拒绝 |
| W8 | 同 token 并发提交两次 | 远端任务创建至多一次 |
| W9 | 成功后重复提交 | 返回原 task_id，标记幂等复用 |
| W10 | Feishu 403/429/5xx | 映射稳定错误码，按策略重试或终止 |

### 9.3 回归

- `multi_user.enabled=false` 时既有工具行为不变。
- 未启用 `tools.feishu` 时不注册新工具。
- 非飞书 Channel 不暴露或无法执行飞书工具。
- 现有 Feishu 收发、CardKit streaming、topic isolation 测试不回归。
- 现有 RAG、记忆隔离和 Admission 测试不回归。

---

## 10. 评测指标

采用确定性判分，不使用 LLM-as-judge 作为主裁判：

| 指标 | 定义 | 目标 |
|---|---|---|
| Thread Message Recall | 返回的目标消息数 / Fixture 中目标消息数 | 1.0（未触发截断时） |
| Cross-Thread Exposure | 返回的其他话题消息数 | 0 |
| Unauthorized Commit Rate | 未满足确认协议却成功创建的比例 | 0 |
| Duplicate Side Effect Rate | 重试导致的重复任务数 / 执行数 | 0 |
| Audit Completeness | 具有 prepare/confirm/commit/remote_id 完整记录的执行占比 | 1.0 |
| Agent Tool Sequence Pass | Stub Provider 预期工具序列通过率 | 1.0 |

Artifacts 至少包含：

- 固定话题 Fixture。
- Tool Call/Observation JSONL。
- Pending Operation 状态迁移记录。
- Mock Feishu API 调用计数。
- 脱敏真实 API 请求/响应。

---

## 11. 风险与降级

| 风险 | 影响 | 处理 |
|---|---|---|
| Task API 需要用户 OAuth | 写工具无法直接复用 App Token | M0 验证；写工具降 P1，只交付读取 |
| 话题 API 不支持 root 直接查询 | 需要拉群消息后过滤，成本上升 | 严格时间窗、分页和条数上限 |
| 模型不会稳定遵循确认格式 | 用户体验下降 | 返回固定确认文案；安全性不降级 |
| API 超时导致结果未知 | 可能重复创建 | 状态记为 unknown/retryable，先查询再重试 |
| 群消息包含敏感内容 | Observation 泄露风险 | 只读当前 audience、限制 Trace 内容、日志脱敏 |
| Tool 范围膨胀 | 延期且削弱主线 | MVP 只保留 read_thread 与 create_task |

---

## 12. 简历与答辩边界

完成后可以表述：

> 基于 Nanobot 现有 ReAct 执行循环，新增飞书话题读取与任务创建工具，使 Agent 能按需补齐
> 未进入短期记忆的群聊上下文，并通过服务端两阶段确认、Principal/Audience 绑定、幂等账本
> 和审计 Trace 安全执行外部副作用。

不能表述：

- “独立实现 Nanobot Agent Loop”
- “从零实现 Thought -> Tool Call -> Observation”
- “新增独立飞书 Agent”
- “实现通用企业审批系统”

本功能能证明：

- 理解 AgentRunner 的完整工具调用流程；
- 能设计 Tool Schema 和 Observation；
- 能把不可信模型决策与确定性安全控制分层；
- 能处理外部 API、副作用、幂等、状态恢复和评测。

但它不等价于独立实现一套通用 Agent Loop，应保持措辞诚实。

---

## 13. 评审决策点

进入开发前需要确认：

1. **MVP 是否同时包含任务创建？**
   - 推荐：M0 验证可行后纳入。
   - 降级：只做 `feishu_read_thread`，工作量更小但项目亮点较弱。
2. **确认交互是否接受固定文本 `确认创建 <token>`？**
   - 推荐先用固定文本，避免首期引入交互卡片回调状态机。
3. **MVP 是否限制一次只创建一个任务？**
   - 推荐是，批量任务留到 P1。
4. **Pending Operation 是否使用独立 SQLite？**
   - 推荐是，避免与记忆/RAG 数据职责混合。
5. **真实演示是否使用测试飞书组织和测试账号？**
   - 推荐是，避免真实团队数据进入 artifacts。

以上决策已按推荐项实施：任务创建进入 MVP；确认采用固定文本；每次仅一个任务；
Pending Operation 使用独立 SQLite；真实联网演示仍应使用测试组织和测试账号。
