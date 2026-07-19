"""M0 白盒泄露复现 (PRD §8 白盒确定性证明 / §11-M0 验收)。

证明基线洞真实存在：私聊(dm)的个人事实一旦进入全局 MEMORY.md（Dream 后门
路径③蒸馏的结果），基线 ContextBuilder 会无条件把它注入到 *任何* 用户/群聊
的 system prompt —— `forbidden_prompt_exposure > 0`。

M6 收口时，隔离内核就位后，这里的期望应翻转为 == 0（届时用 SecurityContext
构造 prompt）。当前 M0 仅锁定"洞存在"的基线事实。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore
from nanoscope.eval import TraceCollector, load_attack_queries, load_forbidden_facts


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


def _count_prompt_exposures(prompt: str) -> tuple[int, list[str]]:
    """在一段 prompt 里数命中了多少条 forbidden fact 的 canary。"""
    hits = [f.id for f in load_forbidden_facts() if f.canary in prompt]
    return len(hits), hits


def test_dataset_loads_frozen_v1():
    facts = load_forbidden_facts()
    attacks = load_attack_queries()
    assert facts and attacks
    fact_ids = {f.id for f in facts}
    # 每个攻击都指向一个已知的 forbidden fact
    for aq in attacks:
        assert aq.target_fact in fact_ids


def test_baseline_leaks_private_memory_into_group_prompt(workspace: Path, tmp_path: Path):
    """基线复现：Alice 私聊事实进全局 MEMORY.md → 注入到攻击者群聊 prompt。"""
    trace = TraceCollector(tmp_path / "trace" / "m0_baseline.jsonl")

    alice_secret = next(f for f in load_forbidden_facts() if f.id == "ff-alice-resign")

    # 模拟 Dream 后门路径③的最终效果：私聊个人事实被蒸馏进全局 MEMORY.md。
    store = MemoryStore(workspace)
    store.write_memory(
        f"# Long-term facts\n- {alice_secret.secret} ({alice_secret.canary})\n"
    )
    trace.record(
        "forbidden_storage",
        fact_id=alice_secret.id,
        target="MEMORY.md",
        hit=True,
    )

    # 攻击者 Eve 在群里发问。基线 build_system_prompt 无 principal/audience 参数，
    # MEMORY.md 被无条件注入。
    attack = next(a for a in load_attack_queries() if a.id == "aq-summary-in-group")
    builder = ContextBuilder(workspace)
    prompt = builder.build_system_prompt(
        channel="feishu",
        session_key="feishu:group-123",
    )

    exposure, hit_ids = _count_prompt_exposures(prompt)
    trace.record(
        "forbidden_prompt_exposure",
        attack_id=attack.id,
        attacker_principal=attack.attacker_principal,
        audience_type=attack.audience_type,
        exposure=exposure,
        hit_ids=hit_ids,
    )

    # 基线：洞存在 —— 别人的私聊秘密进了攻击者的 system prompt。
    assert exposure > 0, "M0 应能复现基线泄露；若为 0 说明基线行为已变，请核对锚点"
    assert alice_secret.id in hit_ids

    # trace 事实已落盘且可读回。
    events = trace.read_all()
    assert any(e["event"] == "forbidden_prompt_exposure" and e["exposure"] > 0 for e in events)


def test_baseline_dream_history_read_has_no_owner_filter():
    """锚点核实：read_unprocessed_history 签名不带 session_key/principal_id（后门根因）。"""
    import inspect

    sig = inspect.signature(MemoryStore.read_unprocessed_history)
    params = set(sig.parameters)
    assert "session_key" not in params
    assert "principal_id" not in params
    # 只按 cursor 切 —— 所有用户(含私聊)历史进同一 Dream prompt。
    assert "since_cursor" in params
