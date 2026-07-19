"""M6 隔离验收：forbidden_prompt_exposure = 0 (PRD §8 五层证据 / §11-M6 收口)。

白盒确定性证明（PRD §8）：
- 把冻结攻击集里的私聊个人事实经 `memory_remember` 路径写入 SQLite（user scope，owner=本人）。
- 对每条攻击 query，用攻击者的 SecurityContext 走 `search_visible` → 拼注入串 →
  经隔离态 ContextBuilder 构造 *攻击者的* system prompt。
- 断言任意攻击者 prompt 中都不出现他人私聊 fact 的 canary（forbidden_prompt_exposure=0）。
- Trace 审计：全程 exposure 事件恒为 0。

这是 M0 白盒复现（`> 0`）在隔离内核就位后的翻转（`= 0`），构成五层证据的第 4/5 层。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.context import ContextBuilder
from nanoscope.eval import TraceCollector, load_attack_queries, load_forbidden_facts
from nanoscope.identity import AUDIENCE_DM, SecurityContext
from nanoscope.memory import SCOPE_USER, Repository


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    r = Repository(tmp_path / "mem.db")
    yield r
    r.close()


def _ctx(tenant: str, principal: str, audience: str) -> SecurityContext:
    return SecurityContext(
        tenant_id=tenant,
        principal_id=principal,
        session_key="feishu:x",
        audience_type=audience,
    )


def _scoped_memory(repo: Repository, ctx: SecurityContext) -> str:
    """复刻 loop._scoped_memory_for_message 的注入串构造（隔离态）。"""
    records = repo.search_visible(ctx)
    if not records:
        return ""
    return "\n".join(f"- {r.content}" for r in records)


def _count_prompt_exposures(prompt: str) -> tuple[int, list[str]]:
    hits = [f.id for f in load_forbidden_facts() if f.canary in prompt]
    return len(hits), hits


def _seed_forbidden_facts(repo: Repository) -> None:
    """经 user-scope 写入口把私聊个人事实落 SQLite（owner=本人，DM 语境）。"""
    for fact in load_forbidden_facts():
        owner_ctx = _ctx(fact.tenant_id, fact.owner_principal, AUDIENCE_DM)
        repo.add(
            owner_ctx,
            content=f"{fact.secret} ({fact.canary})",
            scope=SCOPE_USER,
            source_type="tool",
        )


def test_isolated_prompt_exposure_is_zero(repo: Repository, tmp_path: Path):
    """冻结攻击集上，任意攻击者 prompt 均不含他人私聊 canary。"""
    _seed_forbidden_facts(repo)
    trace = TraceCollector(tmp_path / "trace" / "m6_isolated.jsonl")

    builder = ContextBuilder(tmp_path / "ws")
    builder.multi_user_isolation = True

    total_exposure = 0
    for attack in load_attack_queries():
        fact = next(f for f in load_forbidden_facts() if f.id == attack.target_fact)
        attacker_ctx = _ctx(fact.tenant_id, attack.attacker_principal, attack.audience_type)
        scoped = _scoped_memory(repo, attacker_ctx)
        prompt = builder.build_system_prompt(channel="feishu", scoped_memory=scoped)

        exposure, hit_ids = _count_prompt_exposures(prompt)
        total_exposure += exposure
        trace.record(
            "forbidden_prompt_exposure",
            attack_id=attack.id,
            attacker_principal=attack.attacker_principal,
            audience_type=attack.audience_type,
            exposure=exposure,
            hit_ids=hit_ids,
        )
        assert exposure == 0, (
            f"隔离失效：攻击 {attack.id} 的 prompt 命中 {hit_ids}"
        )

    assert total_exposure == 0

    # Trace 审计（第 5 层证据）：无任何 exposure > 0 事件。
    events = trace.read_all()
    assert events, "应记录到攻击评测事件"
    assert all(e["exposure"] == 0 for e in events if e["event"] == "forbidden_prompt_exposure")


def test_owner_in_dm_can_recall_own_fact(repo: Repository):
    """反向对照：本人私聊仍能召回本人 fact（隔离不误伤本人可见性）。"""
    _seed_forbidden_facts(repo)
    alice = next(f for f in load_forbidden_facts() if f.owner_principal == "orgA:feishu:alice")
    alice_dm = _ctx(alice.tenant_id, alice.owner_principal, AUDIENCE_DM)
    scoped = _scoped_memory(repo, alice_dm)
    assert alice.canary in scoped


def test_owner_in_group_is_gated(repo: Repository):
    """PRD §4.1：本人在群里问，DB 不越权但受众越权——个人 fact 被 DM 门挡下。"""
    _seed_forbidden_facts(repo)
    alice = next(f for f in load_forbidden_facts() if f.owner_principal == "orgA:feishu:alice")
    alice_group = _ctx(alice.tenant_id, alice.owner_principal, "group")
    scoped = _scoped_memory(repo, alice_group)
    assert alice.canary not in scoped
