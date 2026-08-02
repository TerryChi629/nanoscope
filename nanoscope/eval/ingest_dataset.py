"""摄取质量数据集 ingest.v2 (RAG 摄取专项)。

回答一个 rag96 系列未覆盖的空白：**同一批长文档，在不同 chunking 策略/参数下摄取，
对下游召回质量与隔离安全有什么影响？**

- rag96 直接 `add_chunk` 播种完整短文档，从不经过 `ingest_document` 切分，
  因此"切分参数 → 召回质量"这条链路零评测覆盖。
- 本数据集专门构造**足够长、含定位事实（needle）的合成文档**，使不同 size/overlap
  切出不同 chunk 边界，从而暴露"事实被切断 → 召回下降"这一摄取质量问题。

红线：
- 合成语料，密钥/隐私零入库；文本是编造的业务事实（非真实用户/生产数据）。
- 三类 ACL：proj_a（可见，gold 来源）/proj_b（越权 near-dup 靶点）/org（共享噪声）。
- gold 不是固定 id，而是**按每种 chunking 配置动态判定**：含 needle 关键词且属 proj_a
  的 chunk 即 gold。这样才能度量"切分是否破坏了事实可召回性"。
"""

from __future__ import annotations

from dataclasses import dataclass

INGEST_DATASET_VERSION = "ingest.v2"

# proj_a 可见文档：每篇一个唯一 needle（含稀有 canary 词，便于按内容判定 gold）。
_VISIBLE_TOPICS = (
    ("北斗调度系统", "canaryA1", "服务等级协议约定季度可用性达到 99.95 个百分点"),
    ("鲲鹏结算平台", "canaryA2", "对账批处理窗口固定在每日凌晨 02 时 30 分启动"),
    ("玄武风控引擎", "canaryA3", "灰度放量阈值设定为总流量的 15 个百分点上限"),
    ("朱雀推荐链路", "canaryA4", "召回候选队列容量硬上限配置为 4096 个条目"),
    ("白虎日志中台", "canaryA5", "冷数据归档保留周期规定为 180 个自然日"),
    ("青龙网关集群", "canaryA6", "单实例连接数熔断线设置为 8000 条并发链接"),
)

_VISIBLE_DECOYS = {
    "canaryA1": "历史内部目标曾按 99.50 个百分点试算，但没有作为对外服务承诺发布",
    "canaryA2": "旧版测试任务曾在凌晨 01 时启动，该调度已经下线",
    "canaryA3": "演练手册用 30 个百分点作为示例，该数字不属于现行灰度上限",
    "canaryA4": "压测草案记录过 2048 个条目，该值只是早期容量假设",
    "canaryA5": "临时排障数据曾保留 90 日，与正式冷数据归档周期无关",
    "canaryA6": "单元测试环境的 4000 连接阈值不能代表生产熔断配置",
}

_QUERY_PARAPHRASES = {
    "canaryA1": "面向客户正式承诺的季度在线率是多少？",
    "canaryA2": "每日正式对账作业从什么时间开始执行？",
    "canaryA3": "现行发布策略允许的最大流量比例是多少？",
    "canaryA4": "当前召回阶段最多可以容纳多少候选？",
    "canaryA5": "正式冷存储政策要求数据保存多长时间？",
    "canaryA6": "生产实例达到多少并发连接时触发熔断？",
}

# proj_b 越权文档：与 proj_a 关键词高度重合的 near-duplicate 机密（语义最近邻靶点）。
_FORBIDDEN_TOPICS = (
    ("北斗机密预算", "canaryB1", "服务等级协议内部真实可用性预算仅 99.50 个百分点"),
    ("鲲鹏机密成本", "canaryB2", "对账批处理真实单次成本高达 12 万元人民币"),
    ("玄武机密名单", "canaryB3", "灰度放量白名单包含三家未公开战略合作方"),
    ("朱雀机密权重", "canaryB4", "召回候选真实排序权重表属于核心不外传资产"),
)

# org 共享噪声：撑起候选规模，验证切分不误伤共享文档。
_ORG_TOPICS = (
    ("公司公开手册", "orgN1", "员工餐厅每日营业时间为上午 11 时至下午 14 时"),
    ("公司公开公告", "orgN2", "年度全员大会定于第四季度线上线下同步举行"),
)


@dataclass(frozen=True)
class IngestDoc:
    """一篇待摄取文档（切分前的完整文本）。"""

    doc_key: str
    title: str
    scope: str  # 'project' | 'org'
    acl_group: str | None  # proj_a / proj_b / None(org)
    needle: str  # 唯一 canary 关键词，用于按内容动态判定 gold/forbidden
    text: str  # 长文本（含 needle 事实句）


@dataclass(frozen=True)
class IngestQuery:
    """一条针对某可见文档 needle 的查询。"""

    query_id: str
    doc_key: str  # 对应 proj_a 文档
    needle: str  # 该 query 期望命中的关键词（gold 判定依据）
    query: str


def _long_text(topic: str, needle: str, fact: str, decoy: str = "") -> str:
    """合成一篇长文档：前后填充无关段落，中段嵌入含 needle 的事实句。

    段落之间用空行分隔，既能被 fixed 窗口按字符切，也能被 structure 按段落切。
    文本足够长（> 800 字符），保证在 size=100/200/400 下切出多个 chunk。
    """
    filler = (
        f"{topic}是本部门长期建设的核心系统之一，覆盖需求评审、架构设计、"
        "研发实现、灰度上线与稳定性保障等多个阶段，团队在多个季度里持续投入。"
    )
    tail = (
        f"关于{topic}的后续规划，团队将在下一阶段围绕可观测性、成本治理与"
        "容量预估继续演进，并同步完善配套的值班与应急预案流程文档。"
    )
    # needle 句独立成段，置于文档中段，便于观察其是否被切分边界破坏。
    # 句内同时含 topic 与 "重要基线"，使查询能定位到该段而非填充段。
    needle_para = f"{topic}重要基线（{needle}）：{fact}，该数值为当前唯一权威口径。"
    decoy_para = f"{topic}历史记录：{decoy}。" if decoy else filler
    return "\n\n".join([filler, decoy_para, filler, needle_para, tail, tail])


def visible_documents() -> list[IngestDoc]:
    docs: list[IngestDoc] = []
    for topic, needle, fact in _VISIBLE_TOPICS:
        docs.append(
            IngestDoc(
                doc_key=f"A::{needle}",
                title=topic,
                scope="project",
                acl_group="proj_a",
                needle=needle,
                text=_long_text(topic, needle, fact, _VISIBLE_DECOYS[needle]),
            )
        )
    return docs


def forbidden_documents() -> list[IngestDoc]:
    docs: list[IngestDoc] = []
    for topic, needle, fact in _FORBIDDEN_TOPICS:
        docs.append(
            IngestDoc(
                doc_key=f"B::{needle}",
                title=topic,
                scope="project",
                acl_group="proj_b",
                needle=needle,
                text=_long_text(topic, needle, fact),
            )
        )
    return docs


def org_documents() -> list[IngestDoc]:
    docs: list[IngestDoc] = []
    for topic, needle, fact in _ORG_TOPICS:
        docs.append(
            IngestDoc(
                doc_key=f"O::{needle}",
                title=topic,
                scope="org",
                acl_group=None,
                needle=needle,
                text=_long_text(topic, needle, fact),
            )
        )
    return docs


def load_ingest_queries() -> list[IngestQuery]:
    """每篇文档两条独立问法：主题锚点问法 + 去主题词语义改写。"""
    queries: list[IngestQuery] = []
    for topic, needle, _fact in _VISIBLE_TOPICS:
        queries.extend(
            (
                IngestQuery(
                    query_id=f"IQ::{needle}::direct",
                    doc_key=f"A::{needle}",
                    needle=needle,
                    query=f"{topic}当前正式生效的重要基线数值是多少？",
                ),
                IngestQuery(
                    query_id=f"IQ::{needle}::semantic",
                    doc_key=f"A::{needle}",
                    needle=needle,
                    query=_QUERY_PARAPHRASES[needle],
                ),
            )
        )
    return queries
