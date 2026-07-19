"""M2 记忆数据模型 + Repository 单入口测试 (PRD §6 / §11-M2 验收)。

验收点：
- 遍历 tenant×principal×audience×scope，断言 search_visible 可见集合恒满足谓词
  （org 同 tenant 全员可见；user 仅本人且仅 DM 召回 —— DM 门）。
- 非法写入（user 缺 owner / org 带 owner）被 DB CHECK 拒绝（fail-closed）。
- 跨 tenant 记忆不可见（硬隔离）。
"""

from __future__ import annotations

import itertools
import sqlite3
from pathlib import Path

import pytest

from nanoscope.identity import AUDIENCE_DM, AUDIENCE_GROUP, SecurityContext
from nanoscope.memory import SCOPE_ORG, SCOPE_USER, Repository


def _ctx(tenant: str, principal: str, audience: str) -> SecurityContext:
    return SecurityContext(
        tenant_id=tenant,
        principal_id=principal,
        session_key=f"feishu:{principal}",
        audience_type=audience,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    r = Repository(tmp_path / "mem.db")
    yield r
    r.close()


def test_schema_check_rejects_user_without_owner(repo: Repository):
    # 直接绕过 add 走底层 INSERT，验证 DB CHECK 本身 fail-closed。
    with pytest.raises(sqlite3.IntegrityError):
        repo._conn.execute(
            "INSERT INTO memories (id, tenant_id, scope, owner_id, content, "
            "content_hash, source_type, created_at, updated_at) "
            "VALUES ('x','t','user',NULL,'c','h','tool',0,0)"
        )


def test_schema_check_rejects_org_with_owner(repo: Repository):
    with pytest.raises(sqlite3.IntegrityError):
        repo._conn.execute(
            "INSERT INTO memories (id, tenant_id, scope, owner_id, content, "
            "content_hash, source_type, created_at, updated_at) "
            "VALUES ('x','t','org','someone','c','h','tool',0,0)"
        )


def test_add_rejects_bad_scope(repo: Repository):
    ctx = _ctx("orgA", "orgA:feishu:alice", AUDIENCE_DM)
    with pytest.raises(ValueError):
        repo.add(ctx, content="x", scope="conversation", source_type="tool")


def test_add_user_injects_owner_org_nulls_owner(repo: Repository):
    ctx = _ctx("orgA", "orgA:feishu:alice", AUDIENCE_DM)
    u = repo.add(ctx, content="alice secret", scope=SCOPE_USER, source_type="tool")
    o = repo.add(ctx, content="team faq", scope=SCOPE_ORG, source_type="tool")
    assert u.owner_id == "orgA:feishu:alice"
    assert o.owner_id is None


def _predicate_visible(rec_scope, rec_owner, rec_tenant, ctx) -> bool:
    """PRD §6 授权谓词的独立参考实现（测试 oracle）。"""
    if rec_tenant != ctx.tenant_id:
        return False
    if rec_scope == SCOPE_ORG:
        return True
    # scope == user：仅本人且仅 DM。
    return rec_owner == ctx.principal_id and ctx.audience_type == AUDIENCE_DM


def test_search_visible_matches_predicate_over_full_grid(repo: Repository):
    """遍历 tenant×principal×audience×scope，可见集合恒等于谓词 oracle。"""
    tenants = ["orgA", "orgB"]
    principals = ["alice", "bob"]
    scopes = [SCOPE_USER, SCOPE_ORG]

    # 播种：每个 tenant 下，每个 principal 存一条 user 记忆 + 一条 org 记忆。
    seeded = []  # (tenant, scope, owner, content)
    for tenant in tenants:
        for scope in scopes:
            if scope == SCOPE_USER:
                for p in principals:
                    principal_id = f"{tenant}:feishu:{p}"
                    ctx = _ctx(tenant, principal_id, AUDIENCE_DM)
                    content = f"{tenant}/{p}/user"
                    repo.add(ctx, content=content, scope=scope, source_type="tool")
                    seeded.append((tenant, scope, principal_id, content))
            else:
                # org 写入不依赖具体 principal，用任一 principal 的 ctx 注入 tenant。
                ctx = _ctx(tenant, f"{tenant}:feishu:alice", AUDIENCE_DM)
                content = f"{tenant}/org"
                repo.add(ctx, content=content, scope=scope, source_type="tool")
                seeded.append((tenant, scope, None, content))

    audiences = [AUDIENCE_DM, AUDIENCE_GROUP]
    for tenant, p, audience in itertools.product(tenants, principals, audiences):
        principal_id = f"{tenant}:feishu:{p}"
        ctx = _ctx(tenant, principal_id, audience)
        got = {r.content for r in repo.search_visible(ctx, top_k=100)}
        expected = {
            content
            for (rt, rscope, rowner, content) in seeded
            if _predicate_visible(rscope, rowner, rt, ctx)
        }
        assert got == expected, f"tenant={tenant} p={p} audience={audience}"


def test_dm_gate_blocks_personal_memory_in_group(repo: Repository):
    """闭洞证明：A 私聊存的个人记忆，A 在群里问时整条不召回。"""
    principal_id = "orgA:feishu:alice"
    dm_ctx = _ctx("orgA", principal_id, AUDIENCE_DM)
    repo.add(dm_ctx, content="alice private", scope=SCOPE_USER, source_type="tool")

    group_ctx = _ctx("orgA", principal_id, AUDIENCE_GROUP)
    contents = {r.content for r in repo.search_visible(group_ctx)}
    assert "alice private" not in contents


def test_cross_tenant_isolation(repo: Repository):
    a = _ctx("orgA", "orgA:feishu:alice", AUDIENCE_DM)
    b = _ctx("orgB", "orgB:feishu:bob", AUDIENCE_DM)
    repo.add(a, content="orgA org fact", scope=SCOPE_ORG, source_type="tool")
    contents = {r.content for r in repo.search_visible(b)}
    assert "orgA org fact" not in contents
