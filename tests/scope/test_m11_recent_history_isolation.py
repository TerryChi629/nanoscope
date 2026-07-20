"""M11 Recent History 的 principal/audience 隔离 (PRD_v4 §M11 / 修 H1)。

M4/M6 堵的是"记忆"通道；M11 堵的是"最近对话历史"通道。二者都会进 system prompt，
必须用同一套 principal/audience 谓词。

验收（PRD_v4 §M11.3）：
- R1 私聊不串：B 的 dm prompt 不含 A 的 dm canary。
- R2 私聊历史不进群：A 私聊 canary 不进群语境 prompt。
- R3 群内可见：群 G 内 A 的发言对同群 B 可见，对不在 G 的 C 不可见。
- R4 无归属丢弃：缺 principal_id 的条目隔离下被丢弃。
- R5 单用户零回归：isolation=False 输出与改动前完全一致。
- R6 端到端翻转：history 通道攻击集隔离态 exposure==0；改前（isolation=False）>0。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore
from nanoscope.eval import load_attack_queries, load_forbidden_facts
from nanoscope.identity import AUDIENCE_DM, AUDIENCE_GROUP

_A_CANARY = "HIST-CANARY-ALICE-a1a1"
_B_CANARY = "HIST-CANARY-BOB-b2b2"

_ALICE = "orgA:feishu:alice"
_BOB = "orgA:feishu:bob"
_CAROL = "orgA:feishu:carol"
_GROUP_G = "feishu:group-G"
_GROUP_H = "feishu:group-H"


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path)


def _read(store: MemoryStore, *, principal_id, audience_type, audience_id, isolation):
    return store.read_recent_history_for_prompt(
        since_cursor=0,
        session_key=None,
        principal_id=principal_id,
        audience_type=audience_type,
        audience_id=audience_id,
        isolation=isolation,
    )


# --- R1 私聊不串 ---------------------------------------------------------


def test_r1_dm_history_not_crossed_between_principals(store: MemoryStore):
    store.append_history(
        f"离职 ({_A_CANARY})", principal_id=_ALICE,
        audience_type=AUDIENCE_DM, audience_id="alice",
    )
    store.append_history(
        f"薪资 ({_B_CANARY})", principal_id=_BOB,
        audience_type=AUDIENCE_DM, audience_id="bob",
    )
    bob_entries = _read(store, principal_id=_BOB, audience_type=AUDIENCE_DM,
                        audience_id="bob", isolation=True)
    text = " ".join(e["content"] for e in bob_entries)
    assert _A_CANARY not in text
    assert _B_CANARY in text


# --- R2 私聊历史不进群 ---------------------------------------------------


def test_r2_dm_history_never_enters_group(store: MemoryStore):
    store.append_history(
        f"离职 ({_A_CANARY})", principal_id=_ALICE,
        audience_type=AUDIENCE_DM, audience_id="alice",
    )
    # A 也在群 G 里，用 A 的群语境构建 —— 私聊历史绝不进群。
    group_entries = _read(store, principal_id=_ALICE, audience_type=AUDIENCE_GROUP,
                          audience_id=_GROUP_G, isolation=True)
    text = " ".join(e["content"] for e in group_entries)
    assert _A_CANARY not in text


# --- R3 群内可见 ---------------------------------------------------------


def test_r3_group_history_visible_within_same_group_only(store: MemoryStore):
    store.append_history(
        f"周会周三 ({_A_CANARY})", principal_id=_ALICE,
        audience_type=AUDIENCE_GROUP, audience_id=_GROUP_G,
    )
    # 同群 B 可见
    bob_in_g = _read(store, principal_id=_BOB, audience_type=AUDIENCE_GROUP,
                     audience_id=_GROUP_G, isolation=True)
    assert _A_CANARY in " ".join(e["content"] for e in bob_in_g)
    # 不在 G 的 C 不可见
    carol_in_h = _read(store, principal_id=_CAROL, audience_type=AUDIENCE_GROUP,
                       audience_id=_GROUP_H, isolation=True)
    assert _A_CANARY not in " ".join(e["content"] for e in carol_in_h)


# --- R4 无归属丢弃 -------------------------------------------------------


def test_r4_unowned_history_dropped_under_isolation(store: MemoryStore):
    # 老数据：无 principal_id / 无 audience。
    store.append_history(f"legacy ({_A_CANARY})")
    dm_entries = _read(store, principal_id=_ALICE, audience_type=AUDIENCE_DM,
                       audience_id="alice", isolation=True)
    assert dm_entries == []
    group_entries = _read(store, principal_id=_ALICE, audience_type=AUDIENCE_GROUP,
                          audience_id=_GROUP_G, isolation=True)
    assert group_entries == []


# --- R5 单用户零回归 -----------------------------------------------------


def test_r5_single_user_zero_regression(store: MemoryStore):
    """isolation=False 时输出与改动前（只按 session_key）完全一致。"""
    store.append_history("legacy entry without session")
    store.append_history("telegram entry", session_key="telegram:chat-1")
    store.append_history("slack entry", session_key="slack:chat-2")

    entries = store.read_recent_history_for_prompt(
        since_cursor=0,
        session_key="telegram:chat-1",
    )
    assert [e["content"] for e in entries] == ["telegram entry"]

    # unified 分支也保持不变
    unified = store.read_recent_history_for_prompt(
        since_cursor=0,
        session_key="telegram:chat-1",
        unified_session=True,
    )
    assert "legacy entry without session" in [e["content"] for e in unified]


# --- R6 端到端翻转（history 通道） ---------------------------------------


def _seed_forbidden_history(store: MemoryStore) -> None:
    """把冻结攻击集的私聊个人事实经 history 通道落盘（owner=本人，DM 语境）。"""
    for fact in load_forbidden_facts():
        store.append_history(
            f"{fact.secret} ({fact.canary})",
            principal_id=fact.owner_principal,
            audience_type=AUDIENCE_DM,
            audience_id=fact.owner_principal,
        )


def _count_exposures(text: str) -> int:
    return sum(1 for f in load_forbidden_facts() if f.canary in text)


def test_r6_history_channel_leaks_before_fix(store: MemoryStore):
    """改前可证伪：isolation=False 下 history 通道会把私聊 canary 泄露给群攻击者。"""
    _seed_forbidden_history(store)
    total = 0
    for attack in load_attack_queries():
        entries = store.read_recent_history_for_prompt(
            since_cursor=0, session_key=None,
        )
        total += _count_exposures(" ".join(e["content"] for e in entries))
    assert total > 0, "改前 history 通道应能泄露（否则测试无法证伪）"


def test_r6_history_channel_isolation_flips_to_zero(store: MemoryStore, tmp_path: Path):
    """改后翻转：隔离态 history 通道 exposure==0（端到端，经 ContextBuilder prompt）。"""
    _seed_forbidden_history(store)
    builder = ContextBuilder(tmp_path)
    builder.memory = store
    builder.multi_user_isolation = True

    total = 0
    for attack in load_attack_queries():
        fact = next(f for f in load_forbidden_facts() if f.id == attack.target_fact)
        prompt = builder.build_system_prompt(
            channel="feishu",
            scoped_memory="",  # 记忆通道已由 M4/M6 堵死，这里只验 history 通道
            memory_isolation=True,
            history_principal_id=attack.attacker_principal,
            history_audience_type=attack.audience_type,
            history_audience_id="feishu:some-group",
        )
        assert fact.canary not in prompt, f"攻击 {attack.id} 经 history 通道泄露"
        total += _count_exposures(prompt)
    assert total == 0
