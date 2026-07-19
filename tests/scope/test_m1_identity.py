"""M1 身份与安全上下文测试 (PRD §5 / §11-M1 验收)。

验收点：
- 私聊/群聊各解析出正确的 principal_id 与 audience_type。
- principal_id 含 tenant，防跨租户碰撞。
- 同一 workspace 出现第二个 tenant → fail-closed（TenantConflictError）。
- append_history 持久化 principal_id（Dream/审计追溯 owner）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.agent.memory import MemoryStore
from nanoscope.identity import (
    AUDIENCE_DM,
    AUDIENCE_GROUP,
    IdentityResolver,
    SecurityContext,
    TenantConflictError,
    make_principal_id,
)


def test_security_context_is_frozen_and_validated():
    ctx = SecurityContext(
        tenant_id="orgA",
        principal_id="orgA:feishu:u1",
        session_key="feishu:c1",
        audience_type=AUDIENCE_DM,
    )
    assert ctx.is_dm
    with pytest.raises(Exception):
        ctx.tenant_id = "orgB"  # frozen


def test_security_context_rejects_empty_and_bad_audience():
    with pytest.raises(ValueError):
        SecurityContext(tenant_id="", principal_id="p", session_key="s", audience_type=AUDIENCE_DM)
    with pytest.raises(ValueError):
        SecurityContext(tenant_id="t", principal_id="", session_key="s", audience_type=AUDIENCE_DM)
    with pytest.raises(ValueError):
        SecurityContext(tenant_id="t", principal_id="p", session_key="s", audience_type="bogus")


def test_principal_id_includes_tenant():
    pid = make_principal_id("orgA", "feishu", "u1")
    assert pid == "orgA:feishu:u1"
    # 不同 tenant 的同一平台用户不会碰撞
    assert make_principal_id("orgB", "feishu", "u1") != pid


def test_resolver_dm_audience():
    r = IdentityResolver()
    ctx = r.resolve(
        tenant_id="orgA",
        channel="feishu",
        platform_user_id="alice",
        chat_id="dm-alice",
        is_dm=True,
    )
    assert ctx.audience_type == AUDIENCE_DM
    assert ctx.is_dm
    assert ctx.principal_id == "orgA:feishu:alice"
    assert ctx.audience_id == "alice"


def test_resolver_group_audience():
    r = IdentityResolver()
    ctx = r.resolve(
        tenant_id="orgA",
        channel="feishu",
        platform_user_id="eve",
        chat_id="group-123",
        is_dm=False,
    )
    assert ctx.audience_type == AUDIENCE_GROUP
    assert not ctx.is_dm
    assert ctx.principal_id == "orgA:feishu:eve"
    assert ctx.audience_id == "group-123"


def test_resolver_tenant_fail_closed_same_workspace():
    r = IdentityResolver()
    r.resolve(
        tenant_id="orgA",
        channel="feishu",
        platform_user_id="u1",
        chat_id="c1",
        is_dm=True,
        workspace_key="ws1",
    )
    # 同一 workspace 出现第二个 tenant → 拒写
    with pytest.raises(TenantConflictError):
        r.resolve(
            tenant_id="orgB",
            channel="slack",
            platform_user_id="u2",
            chat_id="c2",
            is_dm=True,
            workspace_key="ws1",
        )
    # 不同 workspace 不受影响
    ctx = r.resolve(
        tenant_id="orgB",
        channel="slack",
        platform_user_id="u2",
        chat_id="c2",
        is_dm=True,
        workspace_key="ws2",
    )
    assert ctx.tenant_id == "orgB"


def test_append_history_persists_principal_id(tmp_path: Path):
    store = MemoryStore(tmp_path)
    store.append_history("dm secret", session_key="feishu:dm-alice", principal_id="orgA:feishu:alice")
    store.append_history("no owner")  # 向后兼容：不带 principal_id
    lines = store.history_file.read_text(encoding="utf-8").strip().splitlines()
    rec0 = json.loads(lines[0])
    rec1 = json.loads(lines[1])
    assert rec0["principal_id"] == "orgA:feishu:alice"
    assert "principal_id" not in rec1
