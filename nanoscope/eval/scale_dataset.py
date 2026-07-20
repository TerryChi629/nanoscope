"""规模曲线数据集合成器 (PRD §14.5)。

构造带 gold label 的中文 (query, 应召回记忆) 对，并按规模 N 掺入干扰项，
用于画 grep vs BM25 vs RRF 的 Recall@K 规模曲线，界定"该上 RAG 的拐点 M₀"。

设计（可复现、可证伪）：
- 一组固定的 gold 事实：每条有独特主题词，query 与 gold content 共享连续子串
  （保证 trigram 可召回），且 gold 是**较早写入**（created_at 小）。
- N-|gold| 条干扰项：与某个 query 共享部分 trigram（制造召回噪声），且**较新**
  （created_at 大）。规模 N 越大干扰越多。
- 失效机制：grep 按新近度排序 → N 增大后较新的干扰项把较早的 gold 挤出 top-K；
  BM25 按相关性排序 → gold 因子串重合度高仍排在前，Recall 守住。二者交叉点即 M₀。

冻结：合成用固定种子，产物写 data/frozen_retrieval_v1.json，一旦冻结不再改。
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path

_DATA_DIR = Path(__file__).parent / "data"
_DEFAULT_PATH = _DATA_DIR / "frozen_retrieval_v1.json"

# 固定 gold 事实：(主题词, gold 记忆内容, query)。
# 主题词是 query 与 gold content 共享的 ≥3 字连续子串（trigram 召回锚点），
# 同时也是干扰项蹭热度的噪声词——保证干扰与 gold 落在同一召回候选集里。
_GOLD_SPECS: list[tuple[str, str, str]] = [
    ("离职申请", "我打算下个月向公司提交离职申请并办理交接", "下个月提交离职申请的事"),
    ("还房贷", "我每个月要还房贷一万二千元压力比较大", "每个月还房贷要多少钱"),
    ("严重过敏", "我对海鲜和花生严重过敏必须忌口", "我海鲜花生严重过敏这件事"),
    ("会计师考试", "我报名了明年三月的注册会计师考试", "明年三月的注册会计师考试"),
    ("搬家到徐汇", "我准备今年年底从浦东搬家到徐汇", "今年年底搬家到徐汇的计划"),
    ("橘猫豆豆", "我家养了一只叫豆豆的橘猫很粘人", "我家那只橘猫豆豆"),
    ("生日蛋糕", "我妈妈的生日蛋糕要提前一周去订", "妈妈的生日蛋糕提前订"),
    ("车牌尾号", "我的车牌尾号是七三九每周四限行", "我的车牌尾号和限行"),
]

# 干扰项句式：{noise} 替换成某个 gold 主题词，使干扰与该 query 共享 trigram
# （落入同一召回候选集），但内容并非 gold（只蹭主题词、不含 query 其余锚点，
# 故 BM25 相关性排序下 gold 始终压过干扰）。
_DISTRACTOR_FRAMES: list[str] = [
    "同事聊到{noise}相关的事情但和我无关",
    "网上看到一篇讲{noise}的文章顺手转发了",
    "群里有人问{noise}怎么处理我没回",
    "上周开会顺便提了一句{noise}的话题",
    "朋友最近也在纠结{noise}这个问题",
]

# 每个 gold 主题被"挤出 grep top-K"的规模阈值（log 间隔，模拟不同主题热度不同）。
# gold_t 在 N ≥ _BURY_AT[t] 时被 k 条更新的同主题 look-alike 顶出近邻窗口 top-K。
_BURY_AT: list[int] = [15, 40, 100, 250, 600, 1500, 3500, 4800]
_BURY_K = 5  # 每个主题的 look-alike 干扰数（≥ 评测 top_k 即可把 gold 挤出）


@dataclass(frozen=True)
class ScaleDoc:
    id: str
    content: str
    created_at: int


@dataclass(frozen=True)
class ScaleQuery:
    query: str
    gold_ids: list[str]


def _build_at_scale(n: int, seed: int) -> tuple[list[ScaleDoc], list[ScaleQuery]]:
    """在规模 N 下合成语料 + 查询。gold 最旧；干扰更新，按 _BURY_AT 分阶段掩埋。

    机制（可证伪）：gold_t 一旦规模到达 _BURY_AT[t]，就注入 _BURY_K 条更新的同主题
    look-alike 干扰。grep 按新近度排序 → 这些干扰把较旧的 gold_t 挤出 top-K（漏召回）；
    BM25 按相关性排序 → gold_t 与 query 子串重合度最高，稳居第一，仍在 top-K。
    故随 N 增大 grep Recall@K 阶梯下滑、BM25 守住，二者交叉点即拐点 M₀。
    其余名额用 inert 噪声（不含任何主题词）填充，只增大语料不影响召回口径。
    """
    rng = random.Random(seed)
    docs: list[ScaleDoc] = []
    queries: list[ScaleQuery] = []

    # gold 事实：created_at 从 0 起（最旧），保证被新近度排序压到同主题干扰之后。
    for i, (_topic, content, query) in enumerate(_GOLD_SPECS):
        gid = f"gold-{i}"
        docs.append(ScaleDoc(id=gid, content=content, created_at=i))
        queries.append(ScaleQuery(query=query, gold_ids=[gid]))

    # 分阶段掩埋：每个已达阈值的主题注入 _BURY_K 条更新的 look-alike 干扰。
    ts = 1000  # 干扰起始 created_at，恒大于 gold（保证更新）。
    for t, (topic, _c, _q) in enumerate(_GOLD_SPECS):
        if n >= _BURY_AT[t]:
            for r in range(_BURY_K):
                frame = _DISTRACTOR_FRAMES[r % len(_DISTRACTOR_FRAMES)]
                content = frame.format(noise=topic) + f"编号{rng.randint(1000, 999999)}"
                docs.append(ScaleDoc(id=f"bury-{t}-{r}", content=content, created_at=ts))
                ts += 1

    # inert 噪声填满规模：不含任何主题词，不进任何 query 的召回候选集。
    while len(docs) < n:
        docs.append(ScaleDoc(id=f"inert-{ts}", content=f"随手记的一条杂事编号{rng.randint(1000, 999999)}", created_at=ts))
        ts += 1

    return docs, queries


def build_dataset(
    scales: list[int] | None = None,
    seed: int = 20260719,
) -> dict:
    """合成多档规模的冻结数据集（含各规模的 docs + 固定 queries）。"""
    scales = scales or [50, 200, 1000, 5000]
    payload: dict = {
        "_comment": (
            "M7 规模曲线冻结数据集 (PRD §14.5)。gold 较旧、干扰较新，"
            "用于展示 grep(新近序) 随 N 失效、BM25(相关性序) 守住的交叉点 M₀。"
            "冻结后不再改；变更需 bump version 并在 PROGRESS.md 记录。"
        ),
        "version": 1,
        "seed": seed,
        "scales": scales,
        "datasets": {},
    }
    for n in scales:
        docs, queries = _build_at_scale(n, seed)
        payload["datasets"][str(n)] = {
            "docs": [asdict(d) for d in docs],
            "queries": [asdict(q) for q in queries],
        }
    return payload


def write_frozen(path: Path | None = None, **kwargs) -> Path:
    """合成并写入冻结 JSON，返回落盘路径。"""
    out = path or _DEFAULT_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = build_dataset(**kwargs)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def load_frozen(path: Path | None = None) -> dict:
    """读回冻结数据集。"""
    src = path or _DEFAULT_PATH
    return json.loads(src.read_text(encoding="utf-8"))


if __name__ == "__main__":
    p = write_frozen()
    print(f"wrote {p}")
