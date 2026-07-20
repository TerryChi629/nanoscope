"""M8 owner-aware Dream candidate 测试 (PRD §7 P1 / §11-M8 验收)。

验收点：
- 分批不混 principal：每次喂给 distiller 的历史只含单个 principal。
- owner 由批次确定性继承，非 LLM 指定：即便 distiller 输出里塞了别人的 owner，
  入库 owner 仍等于批次 principal。
- fail-closed：无 principal_id 的历史条目不蒸馏（宁可漏，绝不归错）。
- 入库后仍受 DM 门约束：候选是 scope='user'，他人查不到、跨受众查不到。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoscope.identity import AUDIENCE_DM, AUDIENCE_GROUP, SecurityContext
from nanoscope.memory import (
    Repository,
    distill_candidates,
    group_by_principal,
    run_dream_candidates,
)

_TENANT = "orgA"
_ALICE = "orgA:feishu:alice"
_BOB = "orgA:feishu:bob"


class _RecordingDistiller:
    """记录每次被喂入的历史，回显固定候选。owner 无从指定（契约只返回文本）。"""

    def __init__(self):
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def distill(self, principal_id, entries):
        self.calls.append((principal_id, tuple(e.get("principal_id") for e in entries)))
        return [f"{principal_id} 说了一件事"]


def _ctx(principal: str, audience: str = AUDIENCE_DM) -> SecurityContext:
    return SecurityContext(
        tenant_id=_TENANT,
        principal_id=principal,
        session_key=f"feishu:{principal}",
        audience_type=audience,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    r = Repository(tmp_path / "mem.db")
    yield r
    r.close()


def _history() -> list[dict]:
    # 交错的多人历史（模拟 read_unprocessed_history 的原始混合流）。
    return [
        {"cursor": 1, "content": "alice-1", "principal_id": _ALICE},
        {"cursor": 2, "content": "bob-1", "principal_id": _BOB},
        {"cursor": 3, "content": "alice-2", "principal_id": _ALICE},
        {"cursor": 4, "content": "no-owner", },  # 无 principal_id
        {"cursor": 5, "content": "bob-2", "principal_id": _BOB},
    ]


def test_group_by_principal_never_mixes_and_drops_ownerless():
    batches = group_by_principal(_history())
    # 只剩 alice/bob 两批，无 owner 的条目被丢弃。
    assert [b.principal_id for b in batches] == [_ALICE, _BOB]
    for b in batches:
        assert all(e["principal_id"] == b.principal_id for e in b.entries)
    # 无 owner 的 "no-owner" 不在任何批次。
    all_contents = {e["content"] for b in batches for e in b.entries}
    assert "no-owner" not in all_contents


def test_distiller_only_sees_single_principal_history():
    dist = _RecordingDistiller()
    distill_candidates(_history(), dist)
    # 每次调用喂入的历史 principal 全同一人（不混人）。
    for _pid, pids in dist.calls:
        assert len(set(pids)) == 1
    # alice 批只有 alice 的 principal，bob 批只有 bob 的。
    assert dist.calls[0][1] == (_ALICE, _ALICE)
    assert dist.calls[1][1] == (_BOB, _BOB)


class _MaliciousDistiller:
    """恶意蒸馏器：试图在输出里"指定" owner——本模块必须忽略，按批次归属。"""

    def distill(self, principal_id, entries):
        # 返回文本里塞了别人的 principal，但 owner 仍应由批次确定。
        return [f"owner={_BOB} 的伪造事实"]


def test_owner_inherited_from_batch_not_llm(repo: Repository):
    entries = [{"cursor": 1, "content": "x", "principal_id": _ALICE}]
    written = run_dream_candidates(entries, _MaliciousDistiller(), repo, tenant_id=_TENANT)
    assert len(written) == 1
    # owner 恒等于批次 principal（alice），无视 distiller 输出里的 bob。
    assert written[0].owner_id == _ALICE
    assert written[0].scope == "user"


def test_committed_candidates_respect_dm_gate(repo: Repository):
    dist = _RecordingDistiller()
    run_dream_candidates(_history(), dist, repo, tenant_id=_TENANT)

    # alice 私聊能召回自己的候选。
    alice_dm = {r.content for r in repo.search_visible(_ctx(_ALICE))}
    assert f"{_ALICE} 说了一件事" in alice_dm

    # bob 查不到 alice 的候选（跨 principal 隔离）。
    bob_dm = {r.content for r in repo.search_visible(_ctx(_BOB))}
    assert f"{_ALICE} 说了一件事" not in bob_dm

    # alice 在群聊里查不到自己的个人候选（DM 门）。
    alice_group = {r.content for r in repo.search_visible(_ctx(_ALICE, AUDIENCE_GROUP))}
    assert f"{_ALICE} 说了一件事" not in alice_group


def test_ownerless_history_produces_no_candidates(repo: Repository):
    entries = [{"cursor": 1, "content": "orphan"}]  # 全部无 principal_id
    written = run_dream_candidates(entries, _RecordingDistiller(), repo, tenant_id=_TENANT)
    assert written == []


def test_blank_distiller_output_skipped(repo: Repository):
    class _Blank:
        def distill(self, principal_id, entries):
            return ["", "   "]

    entries = [{"cursor": 1, "content": "x", "principal_id": _ALICE}]
    written = run_dream_candidates(entries, _Blank(), repo, tenant_id=_TENANT)
    assert written == []
