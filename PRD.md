# NanoScope 项目 PRD:多用户 IM Bot 的 Principal/Audience-aware 记忆隔离改造 0719

> 版本:v3.0(定稿·可执行 PRD) · 编写日期:2026-07-19
> 定位:**本文件是自包含的 PRD**。一个全新的对话框只读这一份,就能从"为什么做 → 做什么 → 怎么一步步做完"完整实施整个项目。三部分:
> - **Part 一(§1~§4):为什么做** —— 真实场景 + 基线缺陷证据(所有 `file:line` 已核实)。
> - **Part 二(§5~§9):做什么** —— 安全模型、记忆数据模型、并发、证据体系、MVP 分层(已决策,不再改)。
> - **Part 三(§10~§13):怎么做** —— 里程碑 M0~M10、每步的改动点/验收标准/代码锚点、文件布局、答辩措辞纪律。
> - **Part 四(§14~§15):两个 P1 技术纵深** —— RAG 混合检索、应用层公平调度。**这两条腿是面试重点成果,单独展开:动机机制 / 方案分级 / 数学 / 评测协议 / 答辩话术。** P0 主线不依赖它们,但它们是简历"RAG + 高并发调度"的核心证据。
>
> 上游锁定 commit:`d45d4ebf`(分支 `ljj/nanobot_v0_init`)。文中所有 `file:line` 均为该 commit 下已核实的真实位置;新会话施工前先 `git rev-parse HEAD` 校验一致。
>
> **一句话主线**:nanobot 是"单人·本地"Agent 框架;把它经飞书/Slack 接入**单个组织的可信团队**(群聊+私聊)时,`identity / session / audience / memory-scope` 从未被统一成一个安全上下文,导致**跨用户长期记忆泄露**。本项目重构一个 **Principal/Audience-aware 的多用户 Agent Runtime**:承重腿是记忆隔离内核(P0,可判定的安全不变量),延伸是 RAG 检索与并发公平准入(P1,用一套旁路评测底座量化)。

---

## 版本演进(为什么走到 v3.0,保留论证链不删)

- **v1 → v2.0**:主场景由"课题组 IT 主动共用一个部署"改为"**团队/社区 IM bot**"。原因:前者的"部署形态"可选(可每人一份、可外包网关),撑不起"必须改框架内核";而 IM bot 的多用户是**入口形态内生**的,泄露发生在框架内部数据流(Dream→全局 Core),外层网关难以完整治理。
- **v2.0 → v2.1**:吸收第一轮 review,把主角从"B1 唯一评测平台柱子"重定位为"**场景承重 · 记忆隔离为 P0 承重腿 · B1 为证据平面**"。落地三条技术硬修正:principal_id 一等字段、MVP 阶段 Dream 禁写个人记忆、scope 过滤收进 Repository + DB CHECK。
- **v2.1 → v3.0(本版,定稿)**:吸收第二轮深度 review(`/Users/bytedance/Desktop/codes/nanobot/gpt.md`),完成三个战略决策 + 一批可证伪性修正:
  1. **场景收窄为"单组织·可信团队成员·群聊+私聊"**。开放社区、恶意用户、企业级多租户 → 明确 P2/范围外(否则工具滥用会变成比记忆泄露更大的 P0,撑爆 MVP)。
  2. **补上 Audience(受众)模型**——这是 v2.1 最大的洞:我们拆开了 principal 与 session,却没拆开"**请求者身份**"与"**回答受众**"。采用**方案一(DM 门 + `{user,org}` 两层 scope)+ 前向兼容钩子**(详见 §6)。
  3. **隔离内核 = 唯一 P0 必交付;RAG 与并发都降 P1**("有时间就上")。
  4. **可证伪性修正**:把"框架已按 sender 隔离 session""必然泄露""网关绝对拦不到""第4个请求即崩""单机覆盖 N 无压力""`forbidden_hit=0` 即零泄露"等**过满断言,全部弱化为有条件、可验证的工程命题**(逐条见 §12 措辞纪律)。

---

# Part 一 · 为什么做

## 1. 背景:使用场景迁移了

### 1.1 原设计:单人 · 本地 · 多设备

nanobot 的每一个默认值都写着"个人助手"。这不是猜测,是代码里的自证:

| 证据 | 位置 | 泄露的假设 |
|---|---|---|
| 全局并发闸门**默认值 3**(非硬编码,`Semaphore(_max)`,`_max` 读环境变量默认 3;`≤0` 退化为无限) | `agent/loop.py:311-315` | 默认给到"我自己 + 偶尔几个后台任务"的量级——官方默认就是个人级 |
| 并发数只是**环境变量**、不是 config 字段 | `NANOBOT_MAX_CONCURRENT_REQUESTS`(`loop.py:312`) | 官方没把它当需要随场景调优的旋钮(config schema 里没有它) |
| `unified_session` 注释写 **"single-user multi-device"** | `config/schema.py:148` | session 隔离的设计目标是"单人多设备",不是多人 |
| workspace **全局唯一一份** | `config/schema.py:122`;`agent/loop.py:373`;`agent/context.py:63` | MEMORY/SOUL/USER 只有一份,默认所有人共享 |
| inbound/outbound **无界队列** | `bus/queue.py:17-18` | 个人用户不会瞬间灌几百条,不需要背压 |
| **无**多租户 / per-user 隔离 / ACL | (全仓搜 tenant/multi_user/per_user/acl 无记忆隔离命中) | 只有一个"我",不存在用户间抢占与隐私边界 |

### 1.2 新场景(收窄定稿):单组织 · 可信团队 · 群聊+私聊

**场景定稿表述(采纳 review 建议,后文所有边界以此为准):**

> 一个 nanobot 实例经飞书/Slack 接入**单个组织**,为经过平台鉴权 + allowlist/pairing 的**可信团队成员**提供群聊 + 私聊 Agent 能力。实例共享组织知识与**受控**工具;**个人长期记忆仅在本人私聊中可用**,群/话题记忆仅在对应会话受众内可见。

**为什么是"IM bot"而不是"多人共用一个部署"**——"多个用户连一个 bot"是 IM bot 的**入口定义**,多用户是形态内生、不是运维选择:

| 反问 | 旧场景(课题组主动共用,撑不住) | 新场景(单组织 IM bot,反问失效) |
|---|---|---|
| "为什么不每人部署一份?" | 答不利索——确实可每人本地各跑一份 | 入口天然多用户:一个群里的成员都在跟同一个 bot 说话,给每人配一个 bot 不现实 |
| "为什么不外面包多租户网关?" | 答不利索——正常就该包网关 | **纯鉴权/路由的外部网关修不了框架内部 Dream/Core 的共享语义**;除非它进一步拆实例/workspace/重写记忆层——那本质上就是在做同类隔离改造(见 §4.1 路径③) |
| "这需求真吗?" | 偏产品(省钱省运维),弱 | nanobot **本就自带 IM 接入 + 群聊 session**(`channels/*`),形态官方支持 |

> ⚠️ **措辞纪律(采纳 review #1/#9)**:不要写"给每人配一个 bot 荒谬""网关**绝对**拦不到"。准确说法是上表右列——"入口天然多用户 + 外部网关**难以完整治理**框架内部记忆流,除非它自己也去拆实例/重写记忆层"。这样更难被反驳。

### 1.3 关键区分:共用什么 / 隔离什么

| 维度 | 应该共用? | 说明 |
|---|---|---|
| **入口 / 实例**(一个 bot 服务一群人) | ✅ 共用 | IM bot 的定义 |
| **群聊里的对话消息**(谁在群里说了什么) | ✅ 群成员可见 | IM 平台的物理事实,与 nanobot 无关 |
| **组织知识**(规范/文档/FAQ) | ✅ 共用 | `scope=org`,全员受益 |
| **个人长期记忆 / 画像 / 私聊事实** | ❌ **必须隔离** | bot 沉淀的"关于某人的结论"不该注入给别人;私聊内容更不该被蒸馏进全群可见的 Core |

> **直觉陷阱澄清**:"群里所有人都能看到消息,还谈什么隐私?"——**"对话消息互相可见"(IM 物理事实)和"bot 把你的话沉淀成长期画像后共享给所有人"(nanobot 记忆层行为)是两回事**。前者天经地义,后者是泄露。而且私聊(1:1)内容根本不在群里,却会被 Dream 汇入全局 Core——详见 §4.1。

---

## 2. 这个背景暴露的风险(按"风险类型 + 最小触发条件",非时间序列)

> ⚠️ **措辞纪律(采纳 review #11)**:上一版写成"失效顺序 ①②③④⑤"像必然时间序列,不准确——②~⑤ 的触发依赖 workload、provider 延迟、群活跃度、配置和工具耗时。改为"**风险类型 + 最小触发条件**",且**排队 ≠ 崩**(排队是正常并发控制,只有 SLA 被破坏才算问题)。

| 风险 | 类型 | 最小触发条件 | 现象 / 判据 |
|---|---|---|---|
| A. 跨用户记忆可见 | **安全硬伤** | **启用长期记忆/Dream 且写入个人事实后,只要 ≥2 用户** | A 的个人事实进入全局 Core → 跨用户进入他人 prompt(见 §4.1)。**注意:不是"必然输出",是"缺 owner 边界导致跨用户可见风险"** |
| B. 记忆容量/信噪比恶化 | 容量(随规模) | 记忆随时间累积 | 几十用户画像挤进一份 MEMORY.md 每轮全量注入;**拐点由 B1 实测,不硬拍** |
| C. 全局 gate 排队 | 性能(随规模) | 并发 turn 数 > gate(默认 3) | 第 4 个 turn 起受 gate 排队。**是否构成问题由 `queue_wait_p95/p99` SLA + workload 实测决定,不能直接叫"崩"** |
| D. 慢会话拖慢其他 | 公平性 | 有人跑长任务 | 长任务占 gate 名额;判据是 `max_principal_queue_wait` / `Jain fairness`,不写"是否饿死" |
| E. 洪峰堆积 | 过载 | 群里刷屏 | 无全局 admission 背压时 task/RSS 增长(注意:per-session 有 maxsize=20,见 §3.2) |

> 风险 A 是"≥2 用户即触发"的**安全类**;B/C/D/E 是随规模恶化的**性能/容量类**。两类性质不同、解法不同、优先级不同(A=P0,其余=P1)。

---

## 3. 基线缺陷证据(两类真问题)

### 3.1 记忆系统:缺长期记忆 owner 边界(承重腿 · P0)

**根因一句话**:Core 记忆(MEMORY.md)是**全局唯一一份 + 每轮全量注入**(`agent/context.py:86-88`;`agent/memory.py:233-235`),SOUL/USER 同理(`agent/context.py:80-82, 166-177`)。多用户共用一个 bot 时,基线**缺少 non-interference(互不干扰)保证**。

#### ★ 关键工程动机:身份/会话/记忆从未统一成一个安全上下文

> ⚠️ **措辞纪律(采纳 review #3,这是最重要的一处修正)**:上一版的"王牌"写成"**框架已按 sender 隔离 session,只是长期记忆没跟上**"——**这个论据不准确,已核实源码推翻**:
> - 飞书 `topic_isolation` 是按**群话题**切 session,不是按发送者(`channels/feishu.py:2258-2264`,`chat_type=="group"` 时 `session_key=feishu:{chat_id}:{root_id}`)。
> - Slack 按 **thread** 切(`channels/slack.py:410-411`,`session_key=slack:{chat_id}:{thread_ts}`)。
> - 钉钉**只有显式开** `group_user_isolation` 才按 sender 切(`channels/dingtalk.py:698-699`)。
> - 默认 `session_key` 仍是 `channel:chat_id`(`bus/events.py:32-35`),**不含发送者**。
>
> **准确且依然很强的论据(定稿版)**:
> > nanobot 在入站层保留了 `sender_id`(`bus/events.py:24`),但各 Channel 的 Session 主要按 chat/thread/topic 建模;**身份、会话、受众与长期记忆从未形成统一的安全上下文**。本项目把"已有但未贯通"的 sender identity 升格为一等的 **Principal**,并显式区分 **Principal(谁问)/ Session(哪些消息算一段)/ Audience(答案给谁)/ Memory Scope(记忆给谁看)** 四个正交维度。
>
> 这比"补全一半"更准确,工程动机反而更高级——从"填坑"变成"我建立了缺失的安全上下文抽象"。

**隐私泄露——三条路径,一条比一条隐蔽:**

| 路径 | 机制 | 触发条件 | 证据 |
|---|---|---|---|
| ① Core 文件全局注入 | MEMORY/SOUL/USER 无条件全量注入每个用户每轮 | 默认单 workspace 下,只要 Core 里有个人事实 | `context.py:86-88, 166-177` |
| ② 群聊 / unified 历史串读 | 群未开隔离 / `unified_session=True` 时读到他人 session 历史 | 群未开 `topic_isolation`/`group_user_isolation`,或 `unified_session=True` | `memory.py:394-399`;`session/keys.py:5,8-11`;`channels/feishu.py:357`;`channels/dingtalk.py:698` |
| ③ Dream 无过滤汇入 Core(最隐蔽,连私聊都涉及) | Dream 读历史调 `read_unprocessed_history(since_cursor)`——**签名无 `session_key/principal_id` 参数**,只按 cursor 切,**所有用户(含 1:1 私聊)历史进同一个 Dream prompt**,蒸馏后写全局 MEMORY.md | 即使群聊 session 隔离全开、`unified_session=False` 也发生 | `memory.py:367-369`(签名无 session_key)、`memory.py:490`(Dream 调用点)、`memory.py:482-504`(prompt 拼装) |

> **路径③ 为什么最关键**:群聊 session 隔离**只管当轮对话可见性,管不到 Dream**。用户私聊(1:1)跟 bot 说的话群里根本没有,却会被 Dream 蒸馏进全群可见的 Core。这是"连私密渠道都涉及"的风险,也是纯路由网关难以治理的(它在框架内部数据流上)。
>
> **路径③ 措辞纪律**:不要说"读全量再由 LLM 筛"(弱版本,会被"LLM 可以筛掉啊"化解)。**准确:`read_unprocessed_history` 在数据可见性层面就不带 `session_key/principal_id` 过滤(`memory.py:367-369` 签名可证),所有用户历史在进 LLM 之前就已混入同一个 prompt——风险发生在"喂进去"这一步,不依赖 LLM 是否愿意筛。**
>
> ⚠️ **措辞纪律(采纳 review #8)**:Dream 的 restricted tools 里 `editable_files = [memory_file, soul_file, user_file]`(`memory.py:521`)——**SOUL.md 也是 Dream 的全局写入口**,所以 §7 的"禁写"必须覆盖 MEMORY/USER/**SOUL 三个文件**,不能只堵两个。

**容量爆炸(拐点不硬拍,交给 B1 实测)**:长期运行的 bot,几十用户的偏好/事实全挤进一份 MEMORY.md,每人每轮全量注入 → token 随时间线性上涨 + 信噪比下降。**不断言"到 X 条就必须 RAG"**;容量拐点由 B1 规模曲线实验测出(不同规模下画 grep vs BM25 vs RRF 的 Recall@K,找 grep 开始失效的交叉点)。

> 👉 **本节只列容量缺陷。检索这条腿的完整技术纵深(基线"摘要压缩 + 全量注入"为什么随规模失效 / 混合检索方案分级 / RRF 数学 / chunking / 规模曲线评测协议 / 答辩话术)见 §14——它是面试重点成果,单独展开。**

### 3.2 并发调度:个人级模型撑不住多人

**当前并发模型**(已核实):单消费者主循环(`agent/loop.py:873-882`)→ 每条消息 `asyncio.create_task(self._dispatch(msg))` 起独立协程(`loop.py:952`,故**跨 session 天然并发**)→ 每个 `_dispatch` 内 `async with lock, gate:`(`loop.py:974`)**先拿 per-session Lock、再拿全局 Semaphore,且两者持有到整个 turn 结束**(`_process_message` 全程,含所有 LLM+工具调用)→ per-session pending Queue(**maxsize=20**,`loop.py:977`)。全局 Semaphore **默认 3**(`NANOBOT_MAX_CONCURRENT_REQUESTS`,`≤0` 无限,`loop.py:311-315`);inbound/outbound bus **无界**(`bus/queue.py:17-18`)。

> ⚠️ **措辞纪律(采纳 review #12)**:不要写"零背压"。准确:**无全局有界 admission / overload rejection;仅有 per-session、maxsize=20 的 mid-turn pending 队列,满后仍会回退到 task 排队,无法限制全局积压**。
>
> **澄清常见误解**:它不是"单线程一次处理一个 turn"。asyncio 单线程事件循环可"同时在飞"多个 turn(默认最多 3);turn 是 IO 密集(等 LLM/工具),`await` 时切给别的 turn。真正的并发上限是**人为默认 gate=3**,不是线程。
>
> **措辞纪律**:不要说"3 是写死的"(会被"环境变量能改"驳回)。准确:**官方默认 3、藏在环境变量而非 config、且没有分级/公平/背压——`3` 本身能调,但"没有调度策略"这件事调不掉**,这才是并发改进的靶子。

> 👉 **本节只列缺陷证据。并发这条腿的完整技术纵深(应用层调度定位 / FIFO 队头阻塞机制 / 分级方案 / 5 个压测用例 / Jain 数学 / 答辩话术)见 §15——它是面试重点成果,单独展开。**

---

## 4. 两个必须现在澄清的安全洞(v3.0 新增,来自第二轮 review)

### 4.1 洞一:请求者身份 ≠ 回答受众(P0,方案一堵死)

v2.1 正确拆开了 `principal_id` 和 `session_key`,却把"**谁问**"和"**答案给谁看**"当成一回事。仅有 owner-based WHERE 挡不住下面的泄露:

```
1. A 私聊 bot:"我要离职。"        → 存成 scope=user, owner=A
2. A 后来在【群里】问:"总结下我最近的安排。"
3. 系统以 A 的 principal_id 合法召回该私人记忆   ← owner_id==principal_id 通过,DB 没越权
4. bot 把答案发到【群里】,所有成员看见 A 要离职   ← 输出受众越权!
```

**数据库没越权,但输出受众越权了。** 根因:授权谓词少了一个自变量——它只关于 `principal`,不含 `audience`。正确的谓词必须是 `f(resource ; principal, audience)`。

> ✅ audience 是**确定性可得**的(不是概率判断,符合"安全边界必须确定性"的红线):渠道层本就知道私聊还是群——飞书 `chat_type=="group"` + `reply_to = chat_id if group else sender_id`(`channels/feishu.py:2258-2267`),钉钉 `is_group` + `chat_id`(`channels/dingtalk.py:695-696`)。

**定稿解法 = 方案一(DM 门)**:见 §6。个人 `user` 记忆**只有 `audience_type='dm'`(本人私聊)时才召回**,群聊一律不召回个人记忆。

### 4.2 洞二:多用户下的工具授权(靠场景收窄化解)

nanobot 不只是聊天+记忆,还有 Shell、文件读写、MCP、消息发送、Cron、Subagent、CLI Apps、Skill。现有 Channel `allow_from` 只判断"能不能用 bot"(`channels/base.py:182-229`);用户一旦获准,`ToolRegistry` 主要只做名称/参数校验,**不知道当前 principal/tenant/audience**,没有基于风险分级的工具授权。对开放社区,这是比记忆泄露更大的 P0。

**定稿解法 = 场景收窄(Q1 决策)+ MVP 高风险工具默认禁用**:场景锁死"单组织·可信团队",MVP 禁用 Shell / 任意文件写 / 高风险 MCP,只开放只读知识工具 + 记忆工具。完整的 `ToolAuthorizer` 风险分级 RBAC 列 **P2**(见 §9)。这样把"开放社区恶意用户"这个开放式难题移出 MVP,不撑爆范围。

---

# Part 二 · 做什么(已决策,不再改)

## 5. 安全上下文模型:从 owner-based 升级为多元谓词

一次请求解析出一个 **`SecurityContext`**(冻结不可变),贯穿检索/写入/工具/输出四道谓词。MVP 落 5 个字段生效,`roles`/`audience_id` 为**前向兼容钩子**(采集但 MVP 谓词暂不使用):

| 字段 | 回答的问题 | MVP 取值 / 状态 |
|---|---|---|
| `tenant_id` | 属于哪个组织 | 平台 workspace/team/tenant/app 实体;缺失则必须显式配置。**同一 workspace 检测到多个平台 tenant → fail-closed 拒写长期记忆**(采纳 review #5/P0-3) |
| `principal_id` | 谁发起请求 | **`tenant_id + ":" + channel + ":" + platform_user_id`(含 tenant,防跨租户碰撞)**。只能来自渠道验签/鉴权后的稳定字段,**禁止从用户消息 / 模型输出 / 普通 metadata 取**(采纳 review #5) |
| `session_key` | 哪些消息算一段连续对话 | 沿用框架 `channel:chat_id`(`bus/events.py:32-35`) |
| `audience_type` | 回答是私聊还是公开 | `dm` / `group` / `thread`,从渠道 `chat_type` 确定性读出。**MVP 谓词的关键自变量** |
| `audience_id` | 回答最终发给哪个会话 | 采集(= chat_id/conversation_id/topic_id),**MVP 谓词暂不启用**(前向兼容 P1 conversation scope) |
| `roles` | 他能做什么 | 采集(默认 `member`),MVP 工具授权用"高风险默认禁用"兜底,细粒度 RBAC 列 P2 |

**四道安全谓词(review #15 的架构升华,MVP 至少实现前两道 + can_emit 的 DM 门):**

```text
can_read_memory(ctx, MemoryResource)      # §6 记忆检索
can_write_memory(ctx, requested_scope)     # §6 记忆写入
can_execute_tool(ctx, ToolCall)            # §4.2 MVP=高风险默认禁用
can_emit_response(ctx, retrieved_resources)# 输出受众校验(方案一由 read 端 DM 门前置保证)
```

**身份解析链路**:`InboundMessage → IdentityResolver(渠道验签后) → SecurityContext → {MemoryScope, ToolGate}`。`IdentityResolver` 契约:**只有 channel 层鉴权通过的稳定平台用户 ID 才能生成 `principal_id`**;`principal_id` 与 `session_key` 解耦、各存各的、不互相推导。

> ⚠️ **P0 前置**:`principal_id` 必须在**写 history 时就持久化**(基线 `append_history` 只可选存 `session_key`、不存发送者,`memory.py:239-290`),否则 Dream/审计事后无从追溯 owner。

## 6. 记忆模型:{user, org} + DM 门 + fail-closed(方案一定稿)

**数据模型(SQLite,唯一动态长期记忆写入口):**

```sql
CREATE TABLE memories (
  id           TEXT PRIMARY KEY,
  tenant_id    TEXT NOT NULL,
  scope        TEXT NOT NULL CHECK (scope IN ('user','org')),  -- TEXT+CHECK,非枚举硬编码;将来加 'conversation' 只是一次 migration
  owner_id     TEXT,           -- 仅 scope=user;= principal_id
  audience_id  TEXT,           -- 前向兼容钩子:MVP 恒为 NULL,不参与谓词
  content      TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'active',
  source_type  TEXT NOT NULL,  -- 'tool' | 'import' | ...(MVP 不含 'dream')
  source_ref   TEXT,
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL,
  -- fail-closed:非法组合直接写不进
  CHECK ((scope='user' AND owner_id IS NOT NULL) OR (scope='org' AND owner_id IS NULL))
);
```

**检索授权谓词(Repository 内部强制,上层禁自己拼 WHERE):**

```sql
-- Repository.search_visible(ctx, query, top_k) 内部:
WHERE tenant_id = :tenant_id
  AND (
        scope = 'org'
     OR (scope = 'user' AND owner_id = :principal_id AND :audience_type = 'dm')  -- DM 门:个人记忆仅本人私聊召回
      )
-- 然后在可见集合内做相关性排序(MVP 可全量注入本人可见项,BM25 排序列 P1)
```

**攻击链验证(闭洞证明)**:A 私聊存 `user/owner=A` → A 在群里问,`audience_type='group'` → 谓词 `:audience_type='dm'` 为 false → A 的个人记忆整条被挡在**召回之前** → 不进 prompt → 不可能输出到群。✅ 闭。

**scope 分层(MVP 两层)**:

| scope | 内容 | 可见性 | 注入方式 |
|---|---|---|---|
| `user` | 个人记忆/偏好/私聊事实 | `owner_id` 本人 **且仅其私聊(DM)** | 硬过滤 + 注入 |
| `org` | 团队规范、文档、FAQ | 同 `tenant` 内全员共享 | 硬过滤 + 检索 |

**隔离层的三条职责(缺一条,墙就有后门):**
1. **召回前过滤**:所有检索(含 org 知识)走 `Repository.search_visible(ctx, query, top_k)`,由它**内部**强制上面的 WHERE;上层拿不到拼 WHERE 的机会,不是召回后再隐藏。
2. **写入时打标**:任何写进结构化语料的记忆都带 `tenant_id + scope + owner_id`;缺字段被 DB CHECK 拒绝(fail-closed)。owner 由运行时从 `SecurityContext` 注入,**不进工具 schema、模型无法伪造**。
3. **闭合 Dream 后门**:见 §7。

> **红线**:RAG 是概率性语义相似度,**用它决定可见范围 = 算错一次 = 泄露一次**。安全边界只能是确定性硬过滤 + Repository 单入口 + DB CHECK。scope 过滤必须发生在**召回前**,不是召回后再隐藏。

> **前向兼容(不做但留口)**:`SecurityContext` 已带 `audience_id`、`scope` 用 TEXT+CHECK,将来上 `scope='conversation'`(群内共享不出群)只是"加一条 OR + 一次 migration",不是重构。conversation 记忆的生命周期(退群/解散/topic 可见性)列 P2。

## 7. Dream 后门闭合:三文件禁写(fail-closed,不让 LLM 判 owner)

- **问题**:基线 Dream 走 `read_unprocessed_history`(无 owner 过滤)→ 多人历史拼成一个 prompt(`memory.py:482-504`)→ 蒸馏 → 写全局文件,绕过隔离墙。
- **MVP 做法(确定性,fail-closed)**:`multi_user.enabled=true` 时**禁止 Dream 写全部三个全局文件**——`MEMORY.md`、`USER.md`、`SOUL.md`(证据:`editable_files` 三者俱在,`memory.py:521`)。个人记忆只经显式 `memory_remember` 工具写入 SQLite(带职责2 打标)。单用户模式保持原行为不变。
- **三个全局文件语义分开(采纳 review #8)**:

| 文件 | multi_user 模式下的语义 |
|---|---|
| `SOUL.md` | tenant 级**只读** Agent 人格/行为准则,**可继续注入**(它不是个人数据) |
| `USER.md` | 单用户画像,**多用户模式禁用**(不全局注入) |
| `MEMORY.md` | 只允许**管理员维护的 tenant 级静态内容**(只读);动态个人记忆全部迁 SQLite |
| SQLite `memories` | **唯一动态长期记忆写入口** |

- **为什么 MVP 不做"按 owner 蒸馏"**:那需要 LLM 从混着多人的 prompt 里判断每条事实归谁——又是概率性边界,违反红线。
- **P1 做法(owner-aware,仍确定性)**:history 持久化 `principal_id` → 按 principal **分批**构造独立 Dream prompt(进 LLM 前就切开)→ Dream 只输出 `MemoryCandidate`、**确定性继承该批次的 principal_id**(不由 LLM 指定 owner)→ 后台事务合并入库。

## 8. 证据体系:安全不变量 + 五层证据(不写"实现零泄露")

> ⚠️ **措辞纪律(采纳 review #7)**:有限 Benchmark 中 `forbidden_hit=0` 只能说明"在当前数据集和攻击查询下没有观察到泄露",**不能证明全局"零泄露"**。定稿改为**安全不变量 + 五层证据**:

**安全不变量(写进代码注释与文档)**:*任何返回给 (Principal, Audience) 的记忆,必须满足授权谓词 `can_read_memory`;`memory_remember` 写入必须满足 `can_write_memory`。*

**五层证据(从强到经验):**
1. **DB CHECK**:非法状态(user 缺 owner / org 带 owner)写不进——结构性证明。
2. **Repository 单入口**:所有检索必经 `search_visible`,上层无法绕过过滤——架构性证明。
3. **属性测试(property-based)**:遍历 `tenant × principal × audience × scope` 组合,断言可见集合恒满足谓词。
4. **冻结攻击集 Benchmark**:在冻结的 forbidden-fact 集 + 攻击 query 模板上 `forbidden_prompt_exposure = 0`。
5. **Trace 审计**:运行时 trace 中无越权访问记录。

**安全指标分级(采纳 review #6,最关键是第三个)**:

```text
forbidden_storage_hit      # 秘密是否被写进 Core/SQLite
forbidden_retrieval_hit    # 秘密是否被检索命中
forbidden_prompt_exposure  # ★秘密是否进入了他人的 system prompt —— 进了就算失败,不管模型说没说
forbidden_output_hit       # 模型最终是否说出来
```

**泄露演示拆两级(采纳 review #6)**:
- **白盒确定性证明(验收用)**:FakeProvider 控制 Dream 输出 → 强制归档带 principal A 的历史 → 执行 Dream/写入 → 检查 Core/SQLite → 构造 B(或群)的 prompt → **断言 B 的 system prompt 中不出现 A 的 forbidden fact**(`forbidden_prompt_exposure=0`)。
- **端到端攻击演示(风险展示,非唯一验收)**:真实模型跑"私聊→群"攻击,可录屏,作为直观 demo。

## 9. B1 证据平面:四模块拆分(不把算法塞进 Hook)

> ⚠️ **措辞纪律(采纳 review #15)**:Hook 采集**事实**,不计算 Recall/MRR/Jain。且既然要改 `loop.py` 埋点,就**不能宣称"绝不侵入主循环"**——改为"旁路为主 + loop.py 少量结构化埋点,采集与存储解耦"。

```text
TraceCollector   采集 run / iteration / tool / scheduler / retrieval facts(JSONL)
Evaluator        结合数据集 + gold label 计算 forbidden-hit / Recall / MRR / nDCG
LoadAnalyzer     根据请求完成情况计算 P99 / throughput / Jain fairness
Reporter         生成改前 vs 改后 A/B 的 HTML 报告
```

- **采集点差异**:质量数据经 `Nanobot.run(hooks=)` 挂在 AgentRunner 生命周期(`nanobot.py:136/156`);并发指标(锁等待/队列深度/gate 饱和)发生在 `AgentLoop._dispatch`(`loop.py:964-978`,Hook 够不到),需在 loop.py 另埋结构化事件。二者共用 JSONL 落盘 + Reporter。
- **并发指标(采纳 review #13/#14)**:`queue_wait_p50/p95/p99`、`completion_latency_p95/p99`、`throughput`、`rejected_total`、`event_loop_lag`、`max_principal_queue_wait`、`consumed_service_time`,以及 **Jain index(必须标明算的是哪个变量**,如 `Jain(completed_turns)` vs `Jain(admitted_service_time)`——因不同用户任务成本差异大,只比 completed turns 会偏向短任务用户)。**压测负载模型**要写清:注册用户数 / 日活 / 峰值到达率 / 同时在途 turn 数 / 短中长任务比例 / provider 用 mock 还是实连。

---

# Part 三 · 怎么做(施工路线图)

## 10. MVP 分层(Q3 决策:隔离内核 = 唯一 P0 必交付)

| 层级 | 内容 |
|---|---|
| **P0(隔离内核 · 唯一必交付)** | 单 tenant 可信团队 · `SecurityContext`(principal + audience)· `{user,org}` + DM 门 · `Repository.search_visible` 强制谓词 · DB CHECK + 事务 · Dream 禁写三文件 · `USER.md` 禁用 · 高风险工具默认禁用 · **`forbidden_prompt_exposure` 白盒测试** · 最小 Trace JSONL |
| **P1(有时间就上)** | FTS5/BM25 检索 + grep/BM25/RRF 规模曲线 · owner-aware Dream candidate · Retrieval Benchmark · 全局有界 admission + per-principal in-flight 限制 · 并发压测 A/B |
| **P2(未来扩展)** | Vector/RRF · `scope=conversation` 全生命周期 · round-robin 公平调度 · 共享文档 RAG(chunking 重工程)· 完整 Tool RBAC(`ToolAuthorizer` 风险分级)· 跨渠道 identity 合并 |

> **主线只有一条**:场景逼出的**记忆隔离内核**是承重腿(P0,可判定的安全不变量);RAG 与并发是同一上游逼出的 P1 数据点;B1 是把收益变成数据的证据平面。这不是三线并行,是"一个场景 → 一条承重腿 + 两个 P1 数据点 → 一套证据"。

## 11. 里程碑 M0~M10(每步:目标 / 改哪 / 验收 / 代码锚点)

> 新会话按顺序执行;每个 M 完成后跑该 M 的"验收"再进下一个。**M0~M6 = P0 必交付,M7~M10 = P1**。

### M0 · 冻结基线 + 评测骨架(P0 前置)
- **目标**:锁定 commit、建可复现的测试底座,让后续每步都能"改前 vs 改后"对比。
- **改哪**:`git rev-parse HEAD` 校验 = `d45d4ebf`;新建 `nanoscope/eval/`(冻结 forbidden-fact 集 + 攻击 query 模板 + 数据集加载器);最小 `TraceCollector` 写 JSONL。
- **验收**:能对基线跑一次白盒泄露复现,记录到 `forbidden_prompt_exposure > 0`(证明洞真实存在)。
- **锚点**:`agent/memory.py:367-369`(Dream 后门)、`agent/context.py:86-88`(全局注入)。

### M1 · 身份与安全上下文(P0)
- **目标**:落 `SecurityContext`,`principal_id` 含 tenant 且持久化。
- **改哪**:新增 `IdentityResolver`(渠道验签后生成 `principal_id = tenant:channel:platform_user_id`);`InboundMessage` 补 `principal_id`/`audience_type`/`audience_id`(从各 channel 的 `chat_type`/`sender_id`/`chat_id` 确定性填);`append_history` 持久化 `principal_id`。
- **验收**:私聊/群聊各发一条,断言 history 里记到正确的 `principal_id` 与 `audience_type`;多平台 tenant 共用 workspace 时 fail-closed 拒写。
- **锚点**:`bus/events.py:24,32-35`;`channels/feishu.py:2258-2267`;`channels/dingtalk.py:695-699`;`channels/slack.py:410-411`;`agent/memory.py:239-290`。

### M2 · 记忆数据模型 + Repository 单入口(P0)
- **目标**:建 SQLite `memories` 表 + DB CHECK + `Repository.search_visible` 强制谓词(含 DM 门)。
- **改哪**:新建 `nanoscope/memory/repository.py`;表结构见 §6;`search_visible(ctx, query, top_k)` 内部拼 WHERE,**不暴露任何让上层传 owner/绕过的入口**。
- **验收**:属性测试遍历 `tenant×principal×audience×scope`,断言可见集合恒满足谓词;非法写入(user 缺 owner)被 CHECK 拒绝。
- **锚点**:§6 SQL;`agent/context.py:60-64`(workspace 取文件方式,作对比)。

### M3 · 记忆写入工具 memory_remember(P0)
- **目标**:个人记忆唯一显式写入口,运行时注入 owner。
- **改哪**:`memory_remember` 工具;`scope/owner_id` 由 `SecurityContext` 注入,**不进工具 schema**;org 写入需 `roles` 含 admin(MVP 可先配置常量放行)。
- **验收**:模型无法通过参数伪造 owner/scope;写入的记录带正确标签。

### M4 · 自动检索注入替换全量注入(P0)
- **目标**:用 `search_visible` 的结果注入 prompt,替换基线的 Core 全量注入。
- **改哪**:改 `agent/context.py` 的记忆注入路径,multi_user 模式下走 Repository;MVP 可全量注入"本人可见项"(BM25 排序留 M7)。
- **验收**:群聊里 A 的个人记忆不出现在 prompt(白盒);org 知识正常注入。
- **锚点**:`agent/context.py:86-88, 166-177`。

### M5 · 闭合 Dream 后门(P0)
- **目标**:multi_user 下 Dream 禁写 MEMORY/USER/SOUL 三文件。
- **改哪**:`build_dream_tools` 的 `editable_files` 在 multi_user 下清空(或移除三文件);USER.md 停止注入;SOUL.md 保持只读注入。
- **验收**:触发一次 Dream,断言三个全局文件均未被改写、SQLite 未被 Dream 写入。
- **锚点**:`agent/memory.py:506-522`(`editable_files`,`memory.py:521`)、`memory.py:482-504`(prompt)。

### M6 · 隔离验收(P0 收口)
- **目标**:五层证据齐全,`forbidden_prompt_exposure=0`。
- **改哪**:补齐 §8 的 DB CHECK / Repository 单入口 / 属性测试 / 冻结攻击集 / Trace 审计五件;白盒复现从 M0 的 `>0` 变 `=0`。
- **验收**:整套安全测试绿;录一版"私聊→群"e2e 演示(风险展示)。

### M7 · FTS5/BM25 + 规模曲线(P1)
- **目标**:可见集合内做检索排序;画 grep vs BM25 vs RRF 的 Recall@K 规模曲线,界定"从什么规模该上 RAG"。
- **改哪**:SQLite FTS5;`Evaluator` 算 Recall@K/MRR/nDCG;`Reporter` 出规模曲线。
- **验收**:给出交叉点数据(哪个规模 grep 开始失效)。
- **👉 完整方案(方案分级 / RRF 数学 / chunking / 评测协议 / 答辩话术)见 §14。**

### M8 · owner-aware Dream candidate(P1)
- **目标**:恢复 Dream 蒸馏但确定性归属 owner(§7 P1 做法)。
- **验收**:分批 prompt 不混 principal;candidate 的 owner 由批次确定性继承,非 LLM 指定。

### M9 · 并发有界公平准入 + 压测(P1)
- **目标**:全局有界 admission 队列 + per-principal in-flight 限制(MVP-0);round-robin/retry-after 留 P1 后段。
- **改哪**:`loop.py` 调度层埋点 + admission;`LoadAnalyzer` 算 P99/Jain。
- **验收**:压测负载模型下 `queue_wait_p99` 收敛、`Jain(admitted_service_time)` 提升、`rejected_total` 可控;给出实测拐点 M(不预设"单机覆盖 N 无压力",而是**验证能否满足冻结 SLA**)。
- **锚点**:`loop.py:311-315, 964-978`。
- **👉 完整方案(应用层调度定位 / 三缺陷机制 / 方案分级 / Jain 数学 / 5 个压测用例 / 答辩话术)见 §15。**

### M10 · 统一 A/B 报告(P1 收口)
- **目标**:`Reporter` 出改前 vs 改后一体化 HTML(隔离安全 + 召回质量 + 并发公平),飞书演示可见。

## 12. 措辞纪律总表(答辩/简历口径,全文替换)

| ❌ 过满(旧) | ✅ 可证伪(定稿) |
|---|---|
| "框架已按 sender 隔离 session,只是记忆没跟上" | 框架入站层保留 `sender_id`,但 session 按 chat/thread/topic 建模,**身份/会话/受众/记忆从未统一成安全上下文**;我建立了这个缺失的抽象 |
| "网关**绝对**拦不到" | 纯鉴权/路由网关**难以完整治理**框架内部 Dream/Core 共享语义,除非它自己也去拆实例/重写记忆层 |
| "个人长期记忆**必然**泄露" | 基线缺长期记忆 owner 边界(缺 non-interference);个人事实一旦进全局 Core 即**跨用户可见风险**,Dream 全局输入+写入显著放大 |
| "第 4 个请求即崩" | 第 4 个 turn 起受 gate 排队;**是否是问题由 `queue_wait` SLA + workload 实测决定** |
| "零背压" | 无全局 admission 背压;仅 per-session maxsize=20 mid-turn 队列,满后回退排 task |
| "一台机器覆盖 N 无压力" | **验证**单进程在 N=20~30 能否满足冻结 SLA(不预设结论);实测拐点 M |
| "`forbidden_hit=0` 实现了零泄露" | 安全不变量 + 五层证据;冻结攻击集上 `forbidden_prompt_exposure=0`(不外推为全局零泄露) |
| "给每人配 bot 荒谬" | 入口天然多用户 |

## 13. 最终一句话叙事(简历/答辩定调,数据出来后补 X%)

> 基于开源 Agent 框架 nanobot,针对其"单用户假设"被用于**单组织多用户 IM Bot**(飞书/Slack,群+私聊)时暴露的安全缺陷做魔改,重构为一个 **Principal/Audience-aware 的多用户 Agent Runtime**。
> **核心(承重·P0)**:识别出 `identity/session/audience/memory-scope` 从未统一成安全上下文,导致跨用户长期记忆泄露(含 Dream 把私聊事实蒸馏进全局 Core);建立 `SecurityContext` 抽象,把记忆授权从 owner-based 升级为 **`f(principal, audience)` 谓词**(个人记忆仅本人私聊可召回),scope 过滤收在 Repository 单入口 + DB CHECK + fail-closed,以**安全不变量 + 五层证据**(DB 约束 / 单入口 / 属性测试 / 冻结攻击集 `forbidden_prompt_exposure=0` / Trace 审计)防御跨用户泄露。
> **延伸(P1)**:以 SQLite 检索替代全量注入解容量,用规模曲线界定 RAG 介入拐点;对单进程 asyncio 调度做**有界公平准入**,缓解慢会话拖累与洪峰堆积。
> **证据(P1)**:自建旁路 **Trace + Benchmark 底座**(TraceCollector/Evaluator/LoadAnalyzer/Reporter),对改造前后做可复现 A/B(Recall@K / MRR / forbidden-prompt-exposure / queue-wait P99 / Jain fairness)。

---

# Part 四 · 两个 P1 技术纵深(面试重点成果)

> 说明:P0 隔离内核不依赖这两节。但**简历里"RAG 混合检索"和"高并发公平调度"这两个高光点,证据全在这里**。两节结构一致:①动机(基线为什么随规模失效,带 `file:line`)②方案分级(MVP-0 / P1 / P2,不一次上满)③关键数学 ④评测协议(怎么用 B1 出可证伪数据)⑤答辩话术(可能被追问的点 + 可防守回答)。

## 14. P1-A:记忆检索 —— 从"摘要压缩 + 全量注入"到混合检索

### 14.1 动机:基线两条腿都会随规模失效(已核实)

nanobot 基线**根本没有检索层**(全仓 `grep -i "embedding|bm25|fts5|vector|rerank"` = **0 命中**)。它靠两个机制维持"记得住":

| 基线机制 | 实现 | 随规模失效的方式 |
|---|---|---|
| **摘要压缩(有损)** | `Consolidator` 按 token 预算把驱逐的旧消息**摘要**进 history(`memory.py:638-664`;`consolidation_ratio=0.5`,`_ARCHIVE_SUMMARY_MAX_CHARS=8000`,`memory.py:634`) | 摘要是**有损压缩**:细节(具体数字、边角结论)被抹平。要精确召回"三个月前那个配置值"时,它已被摘掉 |
| **近期窗口注入** | prompt 只读 `read_recent_history_for_prompt`(未处理游标之后的近期条目,`memory.py:380-399`)+ 全量 Core 文件 | **按时间近邻,不按相关性**。老而相关的事实排在窗口外,进不了 prompt |
| **Core 全量注入** | MEMORY/SOUL/USER 每轮整份塞进 system prompt(`context.py:86-88,166-177`) | 多用户/长期运行下,记忆越多注入越大 → **token 天花板 + 信噪比下降**;检索职责被"外包"给 LLM 从一大坨里自己找 |

**一句话动机(可防守)**:基线把"检索"退化成"**把所有东西塞进上下文,让模型自己翻**"。用户少、记忆少时这没问题(还更简单、更确定);但在**单组织多用户 IM bot**下,记忆随人数×时间累积,基线会同时撞上**三堵墙**——① token 天花板(全量注入放不下)② 语义盲区(近邻窗口 + 关键词错过换了说法的相关事实)③ 细节丢失(摘要有损)。检索层的价值就是:**在被 §6 隔离墙硬过滤出的"本 principal 可见集合"内,按相关性精准召回 top-k**,而不是全塞。

> ⚠️ **红线复述(与 §6 一致)**:检索**只在隔离墙之后的可见集合内做相关性排序**。RAG 决定"给哪几条",**永远不决定"能不能看"**。可见性是 `Repository.search_visible` 的确定性 WHERE,检索是它内部可见集合上的 ranking。二者顺序不能反。

### 14.2 方案分级(不一次上满)

| 级别 | 检索方案 | 落点 | 说明 |
|---|---|---|---|
| **MVP-0**(P0 阶段先用) | **无排序,全量注入"本人可见项"** | M4 | 隔离正确性优先;可见集合小的时候直接全注入即可,先不引入检索复杂度 |
| **P1-A.1** | **BM25 / SQLite FTS5**(词频精确匹配) | M7 | 确定性、零外部依赖、可解释;先证明"相关性排序 > 近邻窗口" |
| **P1-A.2** | **向量召回 + RRF 融合**(BM25 ∪ 向量,倒数秩融合) | M7 后段 | 补语义盲区(换了说法也能召回);RRF 免调分数量纲 |
| **P2** | **向量 + Cross-Encoder 重排** / chunk 优化 / 多路召回 | P2 | 精度上限,工程量大,列未来 |

**为什么按这个顺序**:BM25 先行是因为它**确定性、可解释、可作为向量的对照基线**——规模曲线要证明的是"检索 > 全量注入",以及"向量在什么规模开始比 BM25 多召回",没有 BM25 这条对照线,"上向量"就没有可证伪的依据。

### 14.3 关键数学:RRF(Reciprocal Rank Fusion)

融合多路召回(BM25 排序、向量排序)时,**不加权分数**(不同量纲无法直接加),而按**排名倒数**融合:

```
RRF_score(d) = Σ_r  1 / (k + rank_r(d))
```

- `r` 遍历每一路召回器(BM25、向量、…);`rank_r(d)` 是文档 d 在第 r 路里的名次(1 起);`k` 是平滑常数(经验值 60)。
- **为什么用它**:① 只依赖**名次**不依赖**分数**,天然免掉"BM25 分数 vs cosine 相似度"的量纲对齐;② 某路没召回到 d,该项为 0,自动降权;③ 实现简单、无需训练。**面试若问"为什么不直接加权求和",答:分数量纲不可比、且权重需调参/训练,RRF 用秩规避了这两个问题。**

### 14.4 chunking(P2 才重工程,但要知道它是召回质量的上游)

对 `org` 共享知识库(规范/文档/FAQ),**切块质量直接决定召回质量上限**:切太大→一个 chunk 混多主题、稀释相关性;切太小→上下文断裂、指代丢失。MVP 的个人记忆是**短句/短事实**(memory_remember 写入的原子条目),天然是好 chunk,**不需要复杂 chunking**;文档级 RAG(需要按语义/标题切、带重叠窗口)是 **P2**,不进 MVP,避免撑爆范围。

### 14.5 评测协议:用 B1 画规模曲线,找"该上 RAG 的拐点"

这是**这条腿的可证伪核心**——不喊"RAG 更好",而是**用数据界定"从什么规模开始,检索的增量收益压过它的复杂度成本"**。

- **数据集(冻结)**:构造 N 条带 gold label 的 (query, 应召回的记忆) 对;N 取多档(如 50 / 200 / 1000 / 5000)模拟规模增长。**测试集一旦冻结不再改**(否则曲线不可复现)。
- **对照组**:`grep/近邻窗口`(基线代理)vs `BM25` vs `BM25+向量 RRF`,**同一隔离可见集合上**跑。
- **指标**:`Recall@5` / `MRR` / `nDCG@10` / 端到端答案正确率;X 轴 = 记忆规模 N,Y 轴 = 指标。
- **产出结论(示例形态,数字待实测)**:"N < M₀ 时三者接近(此时基线全量注入更简单,不该上 RAG);N > M₀ 后 grep/窗口的 Recall 掉出 SLA,BM25 领先 Δ₁,向量在 N > M₁ 再领先 Δ₂"——**M₀ 就是"该上 RAG 的拐点",这是数据驱动的立项依据,不是拍脑袋。**
- **B1 落点**:`Evaluator` 算 Recall/MRR/nDCG(§9),`Reporter` 出规模曲线 HTML。

### 14.6 答辩话术(可能被追问 → 可防守回答)

| 追问 | 可防守回答 |
|---|---|
| "记忆没多少,上 RAG 是不是过度设计?" | 对小规模确实是——所以我**用规模曲线找拐点 M₀**,MVP-0 阶段可见集合小就是全量注入,不强上;RAG 的必要性是**实测**出来的,不是预设的 |
| "为什么不直接用向量/大模型 embedding?" | 先上 BM25 是要一条**确定性、可解释的对照基线**;没有它,"向量更好"无法证伪。而且向量有额外依赖和延迟,该不该上由曲线说话 |
| "RAG 会不会把别人的记忆检索出来?" | **不会**——检索只在 §6 隔离墙硬过滤出的可见集合内做 ranking;可见性是确定性 SQL WHERE,RAG 永远不决定"能不能看"。这是我的红线 |
| "RRF 的 k=60 怎么来的?" | 经验常数,对结果不敏感;我会在评测里做一次 k 的 sensitivity 小实验佐证 |

---

## 15. P1-B:并发 —— 应用层公平调度(不加机器,只改调度)

### 15.1 立论根基:应用层调度 vs 部署层水平扩展(这条腿"为什么值得做")

> **核心命题(简历定调,之前口头定过、必须写进 PRD)**:**不做部署层水平扩展(不加机器、不拆多进程/多实例、不上外部消息队列),只在单进程 asyncio 内做应用层调度优化**,把 nanobot 从"个人助手并发模型"提升为"团队级共享服务"能扛的调度。

为什么锁死在"应用层"——因为面试官一定会问"你为什么不直接加机器 / 上 K8s / 加 Kafka":

- **场景规模撑得住单进程**:目标是单组织可信团队(§1.2),并发量级是**几十并发 turn**,不是万级 QPS。这个量级下,瓶颈**不是 CPU/机器数,而是缺调度策略**(gate/公平/背压),加机器解决不了"慢会话拖垮快会话"。
- **改调度是"同一份部署内的确定性收益"**:水平扩展是运维动作(且要解决 session 亲和、记忆一致性),而**FIFO 队头阻塞、无公平、无背压是框架内部的算法缺陷**,只能在应用层改。这跟 §1.2"外部网关修不了框架内部记忆流"是同一种论证结构——**问题在内部,解法就得在内部**。
- **可证伪**:改造前后在**同一台机器、同一 workload** 下 A/B,收益完全归因于调度算法,不被"加了机器"污染。

### 15.2 三个机制缺陷(已核实,`file:line`)

| # | 缺陷 | 机制(已核实) | 后果 |
|---|---|---|---|
| ① | **全局 FIFO Semaphore 队头阻塞** | `_dispatch` 里 `async with lock, gate:`(`loop.py:974`),gate=`Semaphore(3)`(`loop.py:313`)。Semaphore 的等待者是 **FIFO** 唤醒;且 gate **持有到整个 turn 结束**(`_process_message` 全程) | 3 个慢 turn(长工具链/长输出)占满 gate 后,**第 4 个不管多快都得等**;慢会话直接拖垮快会话 |
| ② | **无 per-principal 配额 / 无优先级** | gate 是**全局单一计数**,不区分 principal;无任何优先级字段 | 一个刷屏的用户可占满全部 3 个名额,**其他人集体饿等**;无法保证"每人至少能进 1 个" |
| ③ | **无全局有界 admission 背压** | inbound bus 是无界 `asyncio.Queue()`(`bus/queue.py:17-18`);per-session pending 虽 `maxsize=20`(`loop.py:977`)但满了**回退去排 task**,不拒绝 | 洪峰下 task/内存无上限增长,**过载即雪崩**,没有"优雅拒绝 + retry-after"的机会 |

> ⚠️ **措辞纪律**:per-session 有 maxsize=20 的 mid-turn 队列,所以说"零背压"不准确——准确是"**无全局 admission 背压**"(缺陷③)。缺陷①的"队头阻塞"也不要说成"崩",而是"**gate 名额被慢 turn 长期占用导致快 turn 排队,是否违反 SLA 由 `queue_wait_p99` 实测判定**"。

### 15.3 方案分级(不一次上满)

| 级别 | 方案 | 落点 | 解决哪个缺陷 |
|---|---|---|---|
| **MVP-0** | **全局有界 admission 队列 + per-principal in-flight 上限(1 或 2)** | M9 | ③(有界背压)+ ②(每人配额,防单人霸占) |
| **P1-B.1** | **per-principal 公平出队(round-robin / 加权轮询)** | M9 后段 | ①②(把 FIFO 换成公平,慢会话不再拖垮全局) |
| **P1-B.2** | **过载优雅拒绝 + retry-after**(队列超阈值返回"稍后再试"而非无限堆) | M9 后段 | ③(过载可控) |
| **P2** | **优先级队列**(交互式 turn > 后台 Dream/Cron)/ 抢占 / 老化防饿死 | P2 | 更细的 QoS 分层 |

**为什么先做有界 admission + per-principal in-flight**:它是**性价比最高、风险最低**的一步——不动 turn 内部逻辑,只在入口加"每人最多同时 in-flight k 个 + 全局队列有上限",就能同时缓解"单人霸占"(②)和"洪峰雪崩"(③),且**易于 A/B**。round-robin 公平出队是第二步,收益更大但改动更深。

### 15.4 关键数学:Jain's Fairness Index

量化"资源分配是否公平",**取代口头判断"有没有饿死"**:

```
J(x₁,…,xₙ) = ( Σ xᵢ )² / ( n · Σ xᵢ² )       取值 (1/n, 1]
```

- `xᵢ` = 第 i 个 principal 拿到的资源量;`J=1` 完全公平,`J→1/n` 极度不均(一人独占)。
- ⚠️ **必须标明 `xᵢ` 算的是哪个变量**(采纳 review #14):`Jain(completed_turns)`(完成 turn 数)会**偏向短任务用户**(短任务多、自然完成得多,看着"公平"其实是欺负长任务);`Jain(admitted_service_time)`(占用的服务时间)更能反映"gate 名额时间"的分配公平。**报告里两个都出,并说明差异**,这是"数据驱动、可证伪"的体现。
- 配套指标:`max_principal_queue_wait`(最坏排队时间,看饿死)、per-principal `queue_wait_p99`、`consumed_service_time`。

### 15.5 评测协议:5 个压测用例(之前定过、必须写进 PRD)

> **前提(方法论,必须写死)**:**Provider 用 Mock LLM(可控固定/可配延迟),隔离网络抖动**——否则测的是 OpenAI 的延迟,不是我的调度层。只有 mock 掉 LLM,`queue_wait`/`Jain` 才纯粹反映**调度算法**的表现,收益才可归因。

| # | 用例 | 负载构造 | 验证什么 | 关键指标 |
|---|---|---|---|---|
| **U1** | **跨 session 并发爬坡** | 从 N=1 逐步加到 50 个不同 principal 各发 1 turn | gate 饱和点、吞吐拐点、`queue_wait` 随 N 的劣化曲线 → **找实测拐点 M** | `throughput` / `queue_wait_p95/p99` / `event_loop_lag` |
| **U2** | **同 session 洪水** | 单 session 连发大量消息 | per-session Lock 竞争 + mid-turn 队列(maxsize=20)满后行为 | pending 溢出次数 / 该 session 完成延迟 |
| **U3** | **慢会话隔离(★核心)** | 少数 principal 跑长 turn(mock 长延迟)+ 多数跑短 turn | **慢会话是否拖垮快会话**(缺陷①);改造前后对比 | 快 turn 的 `queue_wait_p99`(改前应恶化、改后应稳定)/ `Jain` |
| **U4** | **混合负载 + 公平** | 多 principal,任务成本长/中/短混合,持续一段时间 | per-principal 资源分配是否公平(缺陷②) | `Jain(completed_turns)` vs `Jain(admitted_service_time)` / `max_principal_queue_wait` |
| **U5** | **洪峰过载** | 瞬时灌入远超 gate 承载的请求 | 有界 admission 是否稳住(缺陷③);改前 task/RSS 无上限增长 vs 改后有界 + 优雅拒绝 | `rejected_total` / RSS / task 数上限 |

**A/B 方式**:每个用例在"基线(改前)"与"NanoScope 调度(改后)"下各跑一遍,同 workload、同 mock、同机器,由 `LoadAnalyzer`(§9)算指标、`Reporter` 出对比 HTML。

### 15.6 答辩话术(可能被追问 → 可防守回答)

| 追问 | 可防守回答 |
|---|---|
| "为什么不加机器 / 水平扩展?" | 见 §15.1——目标规模(几十并发)瓶颈不是机器数而是**缺调度策略**;FIFO 队头阻塞/无公平是**框架内部算法缺陷**,加机器解决不了。且单进程 A/B 能把收益纯归因于调度 |
| "gate=3 改大点不就行了?" | gate 只是**并发上限旋钮**,调大它治不了**结构问题**:没有公平(单人仍可霸占)、没有背压(洪峰仍雪崩)、慢会话仍队头阻塞。我改的是**调度策略**,不是那个数字 |
| "怎么证明你的调度更好,不是随机波动?" | 冻结 workload + **Mock LLM 隔离网络** + 同机器 A/B + 多次取分布(p95/p99 非均值)+ Jain 双变量。收益可复现、可证伪 |
| "asyncio 单线程能算高并发吗?" | 这里的并发是 **IO 密集的 turn 并发**(等 LLM/工具),单线程事件循环足够"同时在飞"多个;瓶颈从来不是 CPU 线程,而是**gate + 调度策略**。这正是应用层调度的用武之地 |
| "Jain 为什么要两个变量?" | 只看完成 turn 数会**偏向短任务用户**、掩盖对长任务的不公;加 `admitted_service_time` 才看得到"gate 时间"的真实分配。两个一起出才诚实 |

---

*本文件为自包含 PRD;《方案文档》《需求文档》《开发指导》若与本文件冲突,以本文件 v3.0 为准。新会话施工从 §11 M0 开始;两个 P1 的技术纵深见 §14(RAG)/§15(并发)。*


