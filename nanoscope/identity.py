"""M1 身份与安全上下文 (PRD §5)。

四个正交维度显式区分：
- Principal(谁问) / Session(哪些消息算一段) / Audience(答案给谁) / Memory Scope(记忆给谁看)。

本模块只负责"把渠道鉴权后的稳定字段解析成一个冻结的 SecurityContext"。
记忆检索/写入谓词在 M2+ 的 Repository 里消费 ctx。

安全红线（PRD §5）：
- principal_id 只能来自渠道验签后的稳定平台用户 ID；禁止从用户消息/模型输出取。
- principal_id 含 tenant，防跨租户碰撞。
- 同一 workspace 检测到多个平台 tenant → fail-closed 拒写长期记忆。
"""

from __future__ import annotations

from dataclasses import dataclass

# 受众类型：dm=本人私聊（个人记忆唯一可召回场景），group/thread=公开会话。
AUDIENCE_DM = "dm"
AUDIENCE_GROUP = "group"
AUDIENCE_THREAD = "thread"
_VALID_AUDIENCE = {AUDIENCE_DM, AUDIENCE_GROUP, AUDIENCE_THREAD}


class TenantConflictError(Exception):
    """同一 workspace 检测到多个平台 tenant（PRD §5 fail-closed）。"""


@dataclass(frozen=True)
class SecurityContext:
    """一次请求解析出的不可变安全上下文（PRD §5 的 6 字段）。

    MVP 生效字段：tenant_id / principal_id / session_key / audience_type。
    前向兼容钩子（采集但 MVP 谓词暂不使用）：audience_id / roles。
    """

    tenant_id: str
    principal_id: str
    session_key: str
    audience_type: str
    audience_id: str | None = None
    roles: tuple[str, ...] = ("member",)

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("tenant_id 不能为空（缺失必须显式配置）")
        if not self.principal_id:
            raise ValueError("principal_id 不能为空")
        if self.audience_type not in _VALID_AUDIENCE:
            raise ValueError(f"非法 audience_type: {self.audience_type!r}")

    @property
    def is_dm(self) -> bool:
        return self.audience_type == AUDIENCE_DM


def make_principal_id(tenant_id: str, channel: str, platform_user_id: str) -> str:
    """principal_id = tenant:channel:platform_user_id（含 tenant 防跨租户碰撞，PRD §5）。"""
    if not (tenant_id and channel and platform_user_id):
        raise ValueError("tenant_id/channel/platform_user_id 均不能为空")
    return f"{tenant_id}:{channel}:{platform_user_id}"


def make_audience_id(
    tenant_id: str,
    channel: str,
    audience_type: str,
    platform_audience_id: str,
) -> str:
    """Namespace a non-DM audience so equal raw IDs cannot collide across channels."""
    if not (tenant_id and channel and platform_audience_id):
        raise ValueError("tenant_id/channel/platform_audience_id 均不能为空")
    if audience_type not in {AUDIENCE_GROUP, AUDIENCE_THREAD}:
        raise ValueError(f"非私聊 audience_type 非法: {audience_type!r}")
    return f"{tenant_id}:{channel}:{audience_type}:{platform_audience_id}"


def audience_type_from_dm(is_dm: bool) -> str:
    """从渠道确定性的 is_dm 读出受众类型（不做概率判断，PRD §4.1）。"""
    return AUDIENCE_DM if is_dm else AUDIENCE_GROUP


class IdentityResolver:
    """把渠道验签后的稳定字段解析成 SecurityContext。

    契约：只有 channel 层鉴权通过的稳定平台用户 ID 才能进来生成 principal_id；
    principal_id 与 session_key 解耦、各存各的、不互相推导。

    同时承担 tenant fail-closed 守卫：同一 workspace 若出现第二个不同 tenant，
    resolve 抛 TenantConflictError（PRD §5/P0-3）。
    """

    def __init__(self) -> None:
        # workspace_key -> 已见 tenant_id。MVP 单进程内存态即可。
        self._seen_tenant: dict[str, str] = {}

    def resolve(
        self,
        *,
        tenant_id: str,
        channel: str,
        platform_user_id: str,
        chat_id: str,
        is_dm: bool,
        session_key: str | None = None,
        roles: tuple[str, ...] = ("member",),
        workspace_key: str = "default",
    ) -> SecurityContext:
        self._guard_tenant(workspace_key, tenant_id)
        audience_type = audience_type_from_dm(is_dm)
        return SecurityContext(
            tenant_id=tenant_id,
            principal_id=make_principal_id(tenant_id, channel, platform_user_id),
            session_key=session_key or f"{channel}:{chat_id}",
            audience_type=audience_type,
            # 群受众必须带 tenant/channel/type 命名空间，裸 chat_id 跨平台并不唯一。
            audience_id=(
                platform_user_id
                if is_dm
                else make_audience_id(tenant_id, channel, audience_type, chat_id)
            ),
            roles=roles,
        )

    def _guard_tenant(self, workspace_key: str, tenant_id: str) -> None:
        seen = self._seen_tenant.get(workspace_key)
        if seen is None:
            self._seen_tenant[workspace_key] = tenant_id
        elif seen != tenant_id:
            raise TenantConflictError(
                f"workspace {workspace_key!r} 已绑定 tenant {seen!r}，"
                f"拒绝第二个 tenant {tenant_id!r}（fail-closed）"
            )
