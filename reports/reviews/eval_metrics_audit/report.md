# 代码评审报告

- 仓库：nanoscope
- 检测模式：评估指标可信度审查
- 检测范围：nanoscope/eval full-file + reports/baselines artifacts
- 生成时间：2026-08-01 21:34
- 检查文件：18
- 变更行数：8266

## 缺陷统计

- P0：0
- P1：5
- P2：0
- 合计：5

## 缺陷详情

### 1. [P1][业务语义问题] RAG 的 96 条样本仅有 24 个语义且主题级 gold 使 MRR 必然饱和

- 位置：`nanoscope/eval/rag_bench.py:193-220`
- 置信度：10/10

**问题描述**

g(subj) 把同主题全部 chunk 都标为相关，例如询问训练卡数时 checkpoint、混合精度和故障恢复也全部算 gold；随后 24 个基础问题又被四个固定前缀扩成 96 条。现有真实运行中全部 64 个有效 answerable 样本的 MRR 都为 1，指标没有 rank=2/3 的困难样本，Recall、nDCG、TSR 也被重复样本和过宽 gold 系统性抬高。

**修复建议**

改为事实级 point-wise gold；每个问题只标真正回答该问题的 chunk。使用独立语义改写、同主题硬负例和按语义簇 bootstrap 的 95% CI，禁止把固定前缀变体当独立样本。

---

### 2. [P1][业务语义问题] 注入 ASR 的 baseline 已包含完整防御指令，A/B 对照无法归因

- 位置：`nanoscope/eval/injection_asr_bench.py:176-185`
- 置信度：10/10

**问题描述**

baseline 与 hardened 共用同一个 system prompt，其中明确写了禁止 exfiltrate/send_email/delete_index、禁止泄露机密标记、不得执行外部指令。唯一差异只是 user 内容是否包裹，导致当前 baseline ASR=0、hardened ASR=0、reduction=0。该结果不能证明加固有效，只能证明模型遵守了 system 中直接给出的答案。

**修复建议**

baseline 只保留正常任务契约，不声明外部数据不可信、不点名禁止诱饵工具、不禁止泄露 canary；hardened 再增加不可信数据边界与约束。若 baseline ASR 仍为 0，应报告攻击集对当前模型无效并停止计算 reduction。

---

### 3. [P1][业务语义问题] 拒答 faithful 策略直接读取答案键，Precision/Recall/F1=1 属于构造恒等式

- 位置：`nanoscope/eval/refusal_bench.py:52-113`
- 置信度：10/10

**问题描述**

faithful 策略由 answerable_groups 直接构造 answer_key，query 命中键就回答，否则拒答，因此其 true answer/true refuse 标签在推理前已经泄露给策略。当前 Precision=Recall=F1=1 和 over-refusal=0 是实现定义的必然结果，不是系统拒答能力的实测值。

**修复建议**

用真实生成模型或待测拒答策略盲跑冻结问题；加入部分可答、证据冲突、相邻主题无答案和诱导猜测样本。策略不得访问 answerable 分组或答案键，阈值应在开发集选择、测试集只评一次。

---

### 4. [P1][业务语义问题] 离线 Agent provider 直接返回期望工具和答案，全部 1.0 仅验证控制流

- 位置：`nanoscope/eval/agent_runner_bench.py:23-50`
- 置信度：10/10

**问题描述**

ScriptedProvider 持有完整 AgentTask，第一轮逐字返回 expected_tools 和 expected_arguments，第二轮直接返回 expected_answer。因此 strict TSR、工具 P/R、参数准确率、上下文保真度全部为 1 是测试桩的定义，不代表模型规划或工具使用能力。把该结果并入 UNIFIED_V1 overall gate 会制造能力指标完美的假象。

**修复建议**

将 AGENT_V1 明确重命名为 runner contract test，不进入能力总榜；面试与统一报告以 AGENT_OPEN_V1 的真实模型结果为主，并完成盲化人工复核。

---

### 5. [P1][逻辑错误] 统一安全看板把静态注入探针硬编码为攻击失败

- 位置：`nanoscope/eval/security_cases.py:77-91`
- 置信度：10/10

**问题描述**

_injection_case 无论 baseline_probe_survival 和 hardened_probe_survival 为何，都将 attack_succeeded 固定为 False。聚合器随后把八个 facet 各一条记录平均，得到 attack_success_rate=0 和每个 facet ASR=0。该实现使 prompt_injection 指标无法因静态探针残留而失败，并把非行为测试错误包装成攻击成功率。

**修复建议**

静态探针应单独报告为 sanitation coverage，不进入 attack_success_rate；行为 ASR 必须接入真实模型结果。每个安全 facet 增加多条攻击并分别报告样本数和 Wilson 区间。

---
