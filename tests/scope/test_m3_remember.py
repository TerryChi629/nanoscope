"""M3 memory_remember 写入工具测试 (PRD §6/§11-M3 验收)。

验收点：
- 模型无法伪造 owner/scope：schema 只暴露 content + scope，owner 由运行时注入。
- 写入的记录带正确标签（tenant/scope/owner）。
- 缺安全上下文时 fail-closed 拒写。
- org 写入需 admin 放行（MVP 默认禁用）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.context import RequestContext, request_context
from nanoscope.identity import AUDIENCE_DM, SecurityContext
from nanoscope.memory import SCOPE_ORG, SCOPE_USER, MemoryRememberTool, Repository


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    r = Repository(tmp_path / "mem.db")
    yield r
    r.close()


def _req(tenant="orgA", principal="orgA:feishu:alice", audience=AUDIENCE_DM) -> RequestContext:
    return RequestContext(
        channel="feishu",
        chat_id="dm-alice",
        session_key="feishu:dm-alice",
        tenant_id=tenant,
        principal_id=principal,
        audience_type=audience,
    )


def test_schema_does_not_expose_owner(repo: Repository):
    tool = MemoryRememberTool(repo)
    props = tool.parameters["properties"]
    # owner/owner_id/tenant/principal 均不在 schema —— 模型无法伪造归属。
    assert "owner" not in props
    assert "owner_id" not in props
    assert "tenant_id" not in props
    assert "principal_id" not in props
    assert set(props) <= {"content", "scope"}


async def test_write_user_memory_injects_owner(repo: Repository):
    tool = MemoryRememberTool(repo)
    with request_context(_req()):
        res = await tool.execute(content="alice 喜欢喝美式")
    assert isinstance(res, ToolResult) and not res.is_error
    # 落库带正确 owner/tenant/scope。
    dm_ctx = SecurityContext(
        tenant_id="orgA", principal_id="orgA:feishu:alice",
        session_key="feishu:dm-alice", audience_type=AUDIENCE_DM,
    )
    recs = repo.search_visible(dm_ctx)
    assert len(recs) == 1
    assert recs[0].scope == SCOPE_USER
    assert recs[0].owner_id == "orgA:feishu:alice"
    assert recs[0].tenant_id == "orgA"


async def test_fail_closed_without_security_context(repo: Repository):
    tool = MemoryRememberTool(repo)
    # 无 request_context（单用户/未解析 principal）→ 拒写。
    res = await tool.execute(content="x")
    assert res.is_error
    # 缺 tenant/principal 的 ctx 同样拒写。
    with request_context(RequestContext(channel="c", chat_id="1")):
        res2 = await tool.execute(content="x")
    assert res2.is_error


async def test_org_write_denied_by_default(repo: Repository):
    tool = MemoryRememberTool(repo)  # allow_org_write=False
    with request_context(_req()):
        res = await tool.execute(content="团队规范", scope=SCOPE_ORG)
    assert res.is_error


async def test_org_write_allowed_with_flag(repo: Repository):
    tool = MemoryRememberTool(repo, allow_org_write=True)
    with request_context(_req()):
        res = await tool.execute(content="团队规范", scope=SCOPE_ORG)
    assert not res.is_error
    # org 记忆 owner 恒为 NULL。
    ctx = SecurityContext(
        tenant_id="orgA", principal_id="orgA:feishu:bob",
        session_key="feishu:dm-bob", audience_type=AUDIENCE_DM,
    )
    recs = repo.search_visible(ctx)
    assert any(r.scope == SCOPE_ORG and r.owner_id is None for r in recs)


async def test_model_cannot_forge_owner_via_extra_kwarg(repo: Repository):
    """即便模型硬塞 owner_id/tenant_id 参数，也被忽略（owner 只来自 ctx）。"""
    tool = MemoryRememberTool(repo)
    with request_context(_req(principal="orgA:feishu:alice")):
        res = await tool.execute(
            content="secret", owner_id="orgA:feishu:victim", tenant_id="orgX"
        )
    assert not res.is_error
    dm_ctx = SecurityContext(
        tenant_id="orgA", principal_id="orgA:feishu:alice",
        session_key="feishu:dm-alice", audience_type=AUDIENCE_DM,
    )
    recs = repo.search_visible(dm_ctx)
    assert len(recs) == 1
    assert recs[0].owner_id == "orgA:feishu:alice"  # 不是 victim
    assert recs[0].tenant_id == "orgA"  # 不是 orgX
