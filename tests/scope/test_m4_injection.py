"""M4 检索注入替换全量注入测试 (PRD §6/§11-M4 验收)。

验收点：
- 群聊里 A 的个人记忆不出现在 prompt（DM 门在注入路径上生效）。
- org 知识正常注入。
- multi_user 下（scoped_memory 非 None）绝不回退到全局 MEMORY.md（隔离墙硬过滤）。
- 单用户下（scoped_memory=None）行为与基线一致（仍读全局 MEMORY.md）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore
from nanoscope.identity import AUDIENCE_DM, AUDIENCE_GROUP, SecurityContext
from nanoscope.memory import SCOPE_ORG, SCOPE_USER, Repository

_ALICE_CANARY = "CANARY-ALICE-4c4c"
_ORG_CANARY = "CANARY-ORG-9a9a"


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    r = Repository(tmp_path / "mem.db")
    yield r
    r.close()


def _ctx(principal: str, audience: str) -> SecurityContext:
    return SecurityContext(
        tenant_id="orgA",
        principal_id=principal,
        session_key="feishu:x",
        audience_type=audience,
    )


def _scoped_memory(repo: Repository, ctx: SecurityContext) -> str:
    """复刻 loop._scoped_memory_for_message 的注入字符串构造。"""
    records = repo.search_visible(ctx)
    if not records:
        return ""
    return "\n".join(f"- {r.content}" for r in records)


def _seed(repo: Repository) -> None:
    alice = _ctx("orgA:feishu:alice", AUDIENCE_DM)
    repo.add(
        alice,
        content=f"Alice 计划离职 ({_ALICE_CANARY})",
        scope=SCOPE_USER,
        source_type="tool",
    )
    # org 记忆：写入 owner=None（用任意 principal 的 ctx 都可，scope=org 忽略 owner）。
    repo.add(
        alice,
        content=f"团队周会周三 10 点 ({_ORG_CANARY})",
        scope=SCOPE_ORG,
        source_type="tool",
    )


def test_group_audience_excludes_personal_memory(repo: Repository, tmp_path: Path):
    """Eve 在群里发问：Alice 的个人记忆不进注入串，org 记忆进。"""
    _seed(repo)
    eve_group = _ctx("orgA:feishu:eve", AUDIENCE_GROUP)
    injected = _scoped_memory(repo, eve_group)

    assert _ALICE_CANARY not in injected
    assert _ORG_CANARY in injected

    # 进一步：把注入串塞进 build_system_prompt，确认 canary 不出现在 prompt。
    builder = ContextBuilder(tmp_path)
    prompt = builder.build_system_prompt(channel="feishu", scoped_memory=injected)
    assert _ALICE_CANARY not in prompt
    assert _ORG_CANARY in prompt


def test_owner_dm_includes_personal_memory(repo: Repository, tmp_path: Path):
    """Alice 本人私聊：个人记忆 + org 记忆都可召回并注入。"""
    _seed(repo)
    alice_dm = _ctx("orgA:feishu:alice", AUDIENCE_DM)
    injected = _scoped_memory(repo, alice_dm)

    assert _ALICE_CANARY in injected
    assert _ORG_CANARY in injected

    builder = ContextBuilder(tmp_path)
    prompt = builder.build_system_prompt(channel="feishu", scoped_memory=injected)
    assert _ALICE_CANARY in prompt


def test_scoped_memory_never_falls_back_to_global(tmp_path: Path):
    """multi_user 下即使 workspace 存在全局 MEMORY.md，也绝不回退全量注入。"""
    store = MemoryStore(tmp_path)
    store.write_memory(f"# facts\n- 全局泄露秘密 ({_ALICE_CANARY})\n")

    builder = ContextBuilder(tmp_path)
    # scoped_memory 非 None（含空串）即多用户模式：不读全局 MEMORY.md。
    prompt = builder.build_system_prompt(channel="feishu", scoped_memory="")
    assert _ALICE_CANARY not in prompt


def test_single_user_still_reads_global_memory(tmp_path: Path):
    """单用户（scoped_memory=None）行为与基线一致：仍注入全局 MEMORY.md。"""
    store = MemoryStore(tmp_path)
    store.write_memory(f"# facts\n- 本地长期记忆 ({_ORG_CANARY})\n")

    builder = ContextBuilder(tmp_path)
    prompt = builder.build_system_prompt(channel="feishu", scoped_memory=None)
    assert _ORG_CANARY in prompt
