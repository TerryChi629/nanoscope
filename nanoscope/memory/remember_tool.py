"""M3 memory_remember：个人/组织记忆的唯一显式写入口 (PRD §6/§11-M3)。

安全红线：
- `scope` 只允许 'user'/'org'；`owner_id` 由运行时从 SecurityContext 注入，
  **不进工具 schema**——模型无法伪造 owner，只能声明 content（+ 可选 scope）。
- org 写入需 roles 含 admin（MVP：先用配置常量放行，默认仅允许 user 写入）。
- 缺 SecurityContext（未解析出 principal）时 fail-closed 拒写。
"""

from __future__ import annotations

from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_context
from nanoscope.identity import SecurityContext
from nanoscope.memory.repository import SCOPE_ORG, SCOPE_USER, Repository
from nanoscope.memory.sanitize import sanitize_memory_content

_SOURCE_TYPE = "tool"


@tool_parameters({
    "type": "object",
    "properties": {
        "content": {
            "type": "string",
            "description": "要记住的一条事实/偏好（自然语言，简洁）。",
            "minLength": 1,
        },
        "scope": {
            "type": "string",
            "enum": [SCOPE_USER, SCOPE_ORG],
            "description": (
                "user=个人记忆（仅本人私聊可召回）；org=团队共享知识。"
                "默认 user。owner 由系统注入，不可指定。"
            ),
        },
    },
    "required": ["content"],
})
class MemoryRememberTool(Tool):
    """把一条长期记忆写入结构化语料（SQLite）。owner/tenant 由运行时注入。"""

    _plugin_discoverable = False  # 需要 Repository + 运行时 ctx，手动注册

    def __init__(self, repository: Repository, *, allow_org_write: bool = False):
        self._repo = repository
        self._allow_org_write = allow_org_write

    @property
    def name(self) -> str:
        return "memory_remember"

    @property
    def description(self) -> str:
        return (
            "记住一条长期事实/偏好。scope=user 为个人记忆（仅你私聊时可被调用），"
            "scope=org 为团队共享知识。你只能提供 content 与 scope，"
            "归属（owner）由系统按当前身份自动决定，无法伪造。"
        )

    async def execute(self, **kwargs: Any) -> Any:
        req = current_request_context()
        # fail-closed：无身份上下文（未开 multi_user 或未解析出 principal）→ 拒写。
        if req is None or not req.tenant_id or not req.principal_id:
            return ToolResult.error(
                "memory_remember 不可用：缺少安全身份上下文（需 multi_user 且已解析 principal）。"
            )

        content = kwargs.get("content")
        if not isinstance(content, str) or not content.strip():
            return ToolResult.error("content 不能为空。")
        # NanoScope (PRD_v4 §M12, 修 H2)：写入侧清洗——归一化为纯文本事实，
        # 剥离角色伪造/特殊 token/控制符/零宽字符，长度上限截断。
        content = sanitize_memory_content(content)
        if not content:
            return ToolResult.error("content 清洗后为空。")

        scope = kwargs.get("scope") or SCOPE_USER
        if scope not in (SCOPE_USER, SCOPE_ORG):
            return ToolResult.error(f"非法 scope: {scope!r}（只允许 user/org）。")
        if scope == SCOPE_ORG and not self._allow_org_write:
            return ToolResult.error("无权写入 org 记忆（需管理员权限）。")

        ctx = SecurityContext(
            tenant_id=req.tenant_id,
            principal_id=req.principal_id,
            session_key=req.session_key or f"{req.channel}:{req.chat_id}",
            audience_type=req.audience_type or "group",
        )
        rec = self._repo.add(
            ctx,
            content=content.strip(),
            scope=scope,
            source_type=_SOURCE_TYPE,
            source_ref=req.session_key,
        )
        owner_desc = "个人" if rec.scope == SCOPE_USER else "团队"
        return ToolResult(f"已记住（{owner_desc}记忆，id={rec.id[:8]}）。")
