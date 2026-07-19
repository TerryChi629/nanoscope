"""冻结数据集加载器 (PRD §8/M0)。

- ForbiddenFact: 一条不应跨用户泄露的私聊个人事实（含 canary，便于在 prompt 里字符串命中判定）。
- AttackQuery: 一条试图诱导泄露的攻击请求（含攻击者身份与受众）。

数据集是冻结的 JSON（data/frozen_v1.json），保证评测可复现。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_DATA_DIR = Path(__file__).parent / "data"
_DEFAULT_DATASET = _DATA_DIR / "frozen_v1.json"


@dataclass(frozen=True)
class ForbiddenFact:
    """一条私聊个人事实，不应出现在他人（或群聊）的 prompt/输出里。"""

    id: str
    tenant_id: str
    owner_principal: str
    audience_type: str
    secret: str
    canary: str
    note: str = ""


@dataclass(frozen=True)
class AttackQuery:
    """一条诱导泄露的攻击请求。"""

    id: str
    target_fact: str
    attacker_principal: str
    audience_type: str
    query: str
    note: str = ""


def _load_raw(path: Path | None = None) -> dict:
    dataset = path or _DEFAULT_DATASET
    with open(dataset, "r", encoding="utf-8") as f:
        return json.load(f)


def load_forbidden_facts(path: Path | None = None) -> list[ForbiddenFact]:
    raw = _load_raw(path)
    return [
        ForbiddenFact(
            id=f["id"],
            tenant_id=f["tenant_id"],
            owner_principal=f["owner_principal"],
            audience_type=f["audience_type"],
            secret=f["secret"],
            canary=f["canary"],
            note=f.get("note", ""),
        )
        for f in raw["forbidden_facts"]
    ]


def load_attack_queries(path: Path | None = None) -> list[AttackQuery]:
    raw = _load_raw(path)
    return [
        AttackQuery(
            id=q["id"],
            target_fact=q["target_fact"],
            attacker_principal=q["attacker_principal"],
            audience_type=q["audience_type"],
            query=q["query"],
            note=q.get("note", ""),
        )
        for q in raw["attack_queries"]
    ]
