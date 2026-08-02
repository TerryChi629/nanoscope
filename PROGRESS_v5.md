# NanoScope v5 进展：飞书话题上下文与受控任务执行

> 对应 [PRD_v5_feishu_action_tools.md](./PRD_v5_feishu_action_tools.md)
> 分支：`ljj/scope_v0`
> 基线：`8007e64a`

## 状态

M0~M5 代码、离线确定性验证和真实飞书联网验收均已完成。测试应用已发布
`im:message.group_msg` 与 `task:task:writeonly` 权限；真实话题读取、任务创建和重复
确认幂等复用均通过。

## M0 · API/SDK 契约验证

- 临时安装 `lark-oapi>=1.5,<2` 探查生成代码，不修改项目环境。
- IM v1：`GET /open-apis/im/v1/messages`，按 chat 容器分页，返回
  `message_id/root_id/thread_id/chat_id/sender/body`，支持 tenant/user token。
- Task v2：`POST /open-apis/task/v2/tasks`，`user_id_type=open_id`；
  `InputTask` 支持 `summary/description/due/members/client_token`，支持 tenant/user token。
- 设计落点：受限 chat 分页后按可信 root_id 硬过滤；`client_token` 使用本地稳定幂等键。

## M1 · 话题读取

- 新增 `nanoscope/feishu_actions/client.py`：固定 Feishu/Lark 公网 endpoint、
  tenant token 缓存、SSRF 校验、超时与稳定错误码。
- 新增 `nanoscope/feishu_actions/thread.py`：当前 chat/root 硬过滤、机器人消息默认排除、
  字符/条数截断、控制字符清洗和 `<feishu_thread>` 不可信数据块。
- 新增 `feishu_read_thread`：模型 Schema 不包含 chat_id/root_id/principal_id；
  目标完全来自 `RequestContext`。
- Feishu Channel 只新增 `create_time` 元数据透传，未修改 Agent 执行链。
- 真实环境验证可读取机器人被 @ 前的原始话题内容，包括项目代号和行动项。

## M2/M3 · Prepare、确认、Commit 与幂等

- 新增 `nanoscope/feishu_actions/pending_store.py`：
  - Pending Operation SQLite；
  - token 仅存 SHA-256；
  - Principal/Audience/Session/Channel Instance/Payload 绑定；
  - evidence ledger；
  - `pending -> executing -> succeeded/failed_* / expired`；
  - `BEGIN IMMEDIATE + 条件 UPDATE` 防双提交；
  - operation event 审计。
- 新增 `feishu_task`：
  - `prepare` 只冻结参数、返回预览和 `确认创建 FT-XXXXXXXX`；
  - `commit` 只接收 token，禁止重传任务参数；
  - `RequestContext.original_user_text` 必须精确匹配确认命令；
  - Task v2 `client_token` 使用稳定 idempotency key；
  - 成功后重复提交返回原 task_id，不重复创建。
- Feishu Channel 将用户原始文本独立保存到 `InboundMessage.original_user_text`；
  模型仍读取带 `[Reply to: ...]` 的丰富上下文，安全确认只读取用户实际输入。
- 本地配置启用 `replyToMessage=true`，保证预览和确认停留在发起 prepare 的原话题，
  避免 Audience 绑定被飞书新建回复话题破坏。

## M4 · AgentRunner 真实控制流

专项测试使用真实 `AgentRunner/ToolRegistry/RequestContext` 与 Scripted Provider，分两次
Turn 跑通：

```text
Turn 1:
LLM -> feishu_read_thread -> Observation
LLM -> feishu_task.prepare -> Observation
LLM -> 输出固定确认命令

Turn 2:
用户发送精确确认命令
LLM -> feishu_task.commit -> Observation
LLM -> 返回 task_id
```

`nanobot/agent/runner.py` 零改动。

## M5 · 验证结果

```text
.venv/bin/pytest tests/scope/ -q
259 passed, 9 skipped

相关 Feishu / ToolLoader / RequestContext 回归
168 passed

真实 E2E 修复后最终组合回归
.venv/bin/pytest tests/scope tests/agent/test_loop_tool_context.py \
  tests/channels/test_feishu_reply.py -q
325 passed, 9 skipped

.venv/bin/ruff check nanobot/ nanoscope/ tests/scope/test_feishu_action_tools.py
All checks passed
```

新增专项 13 项覆盖：

- 当前话题召回、跨话题/跨群曝光为 0；
- 不可信 Thread 内容无法闭合 data block；
- 缺 root fail-closed；
- 命名 Feishu Instance 不回退默认实例；
- prepare 远端写调用为 0；
- 精确确认、跨 Principal/Audience 拒绝；
- 未读取 source message 拒绝；
- TTL 过期拒绝；
- 429 后同 client_token 重试；
- 成功后重复 commit 远端创建次数保持 1；
- 真实 AgentRunner 两 Turn 工具序列。

## 真实飞书联网验收

- 权限：`im:message.group_msg`、`task:task:writeonly`，均通过应用版本发布生效。
- 话题读取：成功读取此前未 @ bot 的原始讨论，并提取行动项“检查缓存穿透”。
- 首次 commit：成功创建任务，负责人 `Lai77`，截止时间
  `2026-08-02 18:00 +08:00`。
- 远端任务 ID：`75671f8b-e38f-42f4-8316-2cfd6178b8b9`。
- 重复 commit：返回相同任务 ID 和 `idempotency_reused: true`，未生成第二条任务。
- Duplicate Side Effect Rate：`0`。

真实验收先后暴露并修复两处仅靠 Mock 难以发现的问题：

1. `replyToMessage=false` 会让机器人预览脱离原话题，随后确认被
   Principal/Audience 绑定以 `AUDIENCE_MISMATCH` 拒绝；启用话题内回复后修复。
2. Feishu Channel 会在模型上下文前拼接 `[Reply to: ...]`，若直接把
   `msg.content` 当原始确认文本会触发 `CONFIRMATION_REQUIRED`；现已将
   `original_user_text` 作为独立字段从 Channel 透传到 `RequestContext`。

安全检查：`.nanobot/` 由 `.gitignore` 忽略，`config.json`、日志、运行目录和
`feishu_actions.db` 均未被 Git 跟踪；真实凭证和群聊正文不进入仓库。
