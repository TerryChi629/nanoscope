"""M0 评测底座：冻结数据集 + 攻击 query 模板 + 最小 Trace 采集。

对应 PRD §8/§9/§11-M0。本包只负责"采集事实 + 提供冻结数据集"，
不计算 Recall/MRR/Jain（那是 P1 的 Evaluator/LoadAnalyzer 职责）。
"""

from nanoscope.eval.dataset import (
    AttackQuery,
    ForbiddenFact,
    load_attack_queries,
    load_forbidden_facts,
)
from nanoscope.eval.trace import TraceCollector

__all__ = [
    "AttackQuery",
    "ForbiddenFact",
    "TraceCollector",
    "load_attack_queries",
    "load_forbidden_facts",
]
