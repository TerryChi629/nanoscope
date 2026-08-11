# LegalBench-RAG 切片质量测评

该专项使用切片前已经标注的字符区间作为 gold evidence，补充 `ingest.v2` 无法直接衡量的
边界完整性。现有 `ingest.v2` 继续负责摄取参数、下游召回和权限隔离回归；两套结果不混算。

## 数据准备

从 LegalBench-RAG 官方仓库链接下载数据，并解压为：

```text
data/
├── benchmarks/
│   ├── contractnli.json
│   ├── cuad.json
│   ├── maud.json
│   └── privacy_qa.json
└── corpus/
    ├── contractnli/
    ├── cuad/
    ├── maud/
    └── privacy_qa/
```

本项目不分发上游语料。使用前需自行确认 LegalBench-RAG 及 ContractNLI、CUAD、MAUD、
PrivacyQA 的数据条款。加载器会拒绝路径越界、空证据、字符区间越界和 gold answer 与原文
不一致的数据。

Mini 数据集从四个子集各选择 25 条，共 100 条。选择顺序由 query、文件路径和 gold span
的 SHA256 决定，不依赖 Python 随机数；摘要中同时记录所选原文及标注的整体指纹。

## 运行档位

### L0 离线基线

```bash
python -m nanoscope.eval.chunk_quality_bench \
  --data-dir /path/to/LegalBench-RAG/data \
  --cases-per-benchmark 25 \
  --level L0
```

`L0` 使用 `HashingEmbedder + StubReranker`，不需要 API Key。它用于验证数据、切片区间和
指标链路是否可复现，不代表英文法律语义检索的实际能力，不应仅凭其 Recall 选择生产策略。

### L1 真实 embedding

```bash
export GLM_API_KEY='...'
python -m nanoscope.eval.chunk_quality_bench \
  --data-dir /path/to/LegalBench-RAG/data \
  --level L1 \
  --requests-per-second 0.5 \
  --embedding-batch-size 32
```

`L1` 使用 GLM `embedding-3 + StubReranker`。密钥只从环境变量读取，不写入报告、checkpoint
或配置文件。成功向量默认写入 `.nanobot/cache/chunk_quality_embeddings.sqlite`；缓存只含
模型 namespace、文本 SHA-256 和向量，不含法律原文及凭证，失败后可续跑。

### L2 真实重排

```bash
export GLM_API_KEY='...'
export SILICONFLOW_API_KEY='...'
python -m nanoscope.eval.chunk_quality_bench \
  --data-dir /path/to/LegalBench-RAG/data \
  --level L2
```

`L2` 使用 GLM `embedding-3 + BAAI/bge-reranker-v2-m3`。Reranker 只调整 embedding
召回的 `fanout` 候选顺序，不改变候选集合，并复用 L1 的持久化向量。

可以重复传入 `--config strategy:size:overlap` 运行子网格，例如：

```bash
python -m nanoscope.eval.chunk_quality_bench \
  --data-dir /path/to/LegalBench-RAG/data \
  --cases-per-benchmark 5 \
  --level L1 \
  --config fixed:800:0 \
  --config fixed:800:160
```

## 输出

默认生成：

```text
reports/baselines/CHUNK_QUALITY_LEGALBENCH_V1.json
reports/checkpoints/chunk_quality/legalbenchrag-mini.v1.jsonl
```

摘要包含数据指纹、模型档位、各切片配置、分子集结果和帕累托前沿；checkpoint 保留
`config × case` 明细。

## 指标口径

- `evidence_recall`：检索区间与 gold 区间交集字符数除以 gold 字符数。
- `evidence_precision`：交集字符数除以检索结果覆盖的唯一字符数。
- `evidence_iou`：检索区间与 gold 区间的字符级交并比。
- `boundary_intrusion_rate`：内部存在至少一个 chunk 边界的 gold span 比例。
- `complete_evidence_rate`：至少有一个 chunk 完整包含 gold span 的比例。
- `mean_min_cover_chunks`：完整覆盖一个 gold span 所需的最少 chunk 数均值。
- `corpus_duplication_ratio`：overlap 导致的重复字符占全部输出字符的比例。
- `mean_retrieved_char_count`：每条查询 top-k 结果覆盖的唯一字符数均值。

所有覆盖指标先对区间取并集，再计算交集，因此多个 overlap chunk 不会重复累计同一段
gold，Recall 不会因重叠超过 1。

## 20-case Live Pilot

L0/L1/L2 使用同一批 20 cases、18 documents、六配置、`top_k=5` 和 `fanout=20`。主要结果：

| 配置 | L1 Recall | L2 Recall | L2 IoU | L2 Precision | L2 上下文字符 |
|---|---:|---:|---:|---:|---:|
| fixed 400 / 0 | 0.061 | 0.248 | 0.048 | 0.057 | 2,000 |
| fixed 400 / 80 | 0.058 | 0.211 | 0.038 | 0.044 | 1,939 |
| fixed 800 / 0 | 0.147 | 0.317 | 0.043 | 0.045 | 3,957 |
| fixed 800 / 160 | 0.217 | 0.449 | 0.059 | 0.062 | 3,821 |
| fixed 1600 / 160 | 0.262 | 0.594 | 0.036 | 0.037 | 7,717 |
| structure 800 / 0 | 0.230 | 0.354 | 0.084 | 0.089 | 2,936 |

L2 在六个配置上均提高 Recall。`fixed1600/160` 召回最高但上下文成本最大；
`structure800/0` 的 Precision 和 IoU 最高；`fixed800/160` 位于两者之间。20 cases 只用于
验证方向与组件增量，v1 仍不设置经验性质量门槛；应先扩充样本，再基于多次运行分布确定门禁。
