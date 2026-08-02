"""Feishu thread reading and confirmation-gated task creation tools."""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_context
from nanobot.config_base import Base
from nanoscope.feishu_actions import (
    FeishuApiError,
    FeishuOpenApiClient,
    PendingOperationStore,
    extract_thread_messages,
    resolve_feishu_instance,
)
from nanoscope.feishu_actions.pending_store import PendingOperationError

_OPEN_ID_RE = re.compile(r"^ou_[A-Za-z0-9_-]+$")
_TOKEN_RE = re.compile(r"^FT-[A-HJ-NP-Z2-9]{8}$")


class FeishuActionToolConfig(Base):
    enabled: bool = False
    read_thread_enabled: bool = True
    task_enabled: bool = False
    thread_message_limit: int = Field(default=50, ge=1, le=100)
    thread_max_chars: int = Field(default=30_000, ge=1000, le=100_000)
    thread_max_pages: int = Field(default=10, ge=1, le=20)
    thread_lookback_days: int = Field(default=7, ge=1, le=30)
    confirmation_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    evidence_ttl_seconds: int = Field(default=3600, ge=60, le=86_400)
    request_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    database_path: str = ""


def _error(code: str, message: str) -> ToolResult:
    return ToolResult.error(json.dumps({"error": code, "message": message}, ensure_ascii=False))


def _request_identity():
    request = current_request_context()
    if request is None or not request.channel.startswith("feishu"):
        raise PendingOperationError(
            "FEISHU_CONTEXT_REQUIRED", "This tool requires a Feishu request context"
        )
    required = {
        "tenant_id": request.tenant_id,
        "principal_id": request.principal_id,
        "audience_id": request.audience_id,
        "session_key": request.session_key,
    }
    if any(not value for value in required.values()):
        raise PendingOperationError(
            "FEISHU_CONTEXT_REQUIRED",
            "Authenticated principal, audience, and session are required",
        )
    return request


def _store(workspace: str | Path, config: FeishuActionToolConfig) -> PendingOperationStore:
    path = (
        Path(config.database_path).expanduser()
        if config.database_path
        else Path(workspace) / "memory" / "feishu_actions.db"
    )
    return PendingOperationStore(path)


def _instance_identity(instance) -> str:
    if instance.identity_key:
        return instance.identity_key
    return hashlib.sha256(f"{instance.domain}:{instance.app_id}".encode()).hexdigest()


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "include_bot_messages": {"type": "boolean"},
        },
        "additionalProperties": False,
    }
)
class FeishuReadThreadTool(Tool):
    """Read only the current authenticated Feishu thread."""

    config_key = "feishu_actions"

    @classmethod
    def config_cls(cls):
        return FeishuActionToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.config.feishu_actions.enabled and ctx.config.feishu_actions.read_thread_enabled

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(
            config=ctx.config.feishu_actions,
            channels_config=ctx.channels_config,
            client=FeishuOpenApiClient(
                timeout_seconds=ctx.config.feishu_actions.request_timeout_seconds
            ),
            store=_store(ctx.workspace, ctx.config.feishu_actions),
        )

    def __init__(
        self,
        *,
        config: FeishuActionToolConfig,
        channels_config: Any,
        client: FeishuOpenApiClient,
        store: PendingOperationStore,
    ):
        self.config = config
        self.channels_config = channels_config
        self.client = client
        self.store = store

    @property
    def name(self) -> str:
        return "feishu_read_thread"

    @property
    def description(self) -> str:
        return (
            "Read the complete current Feishu topic when the saved conversation history is "
            "insufficient, such as when users discussed before mentioning the bot. The target "
            "chat and topic are fixed by the authenticated request; they cannot be supplied."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        limit: int | None = None,
        include_bot_messages: bool = False,
        **_: Any,
    ) -> Any:
        try:
            request = _request_identity()
            metadata = request.metadata if isinstance(request.metadata, dict) else {}
            root_id = str(metadata.get("root_id") or "")
            if not root_id:
                return _error(
                    "THREAD_CONTEXT_MISSING",
                    "Current message is not inside an existing Feishu topic",
                )
            instance = resolve_feishu_instance(self.channels_config, request.channel)
            create_time = int(metadata.get("create_time") or 0)
            if create_time > 10_000_000_000:
                create_time //= 1000
            if create_time <= 0:
                return _error(
                    "THREAD_CONTEXT_MISSING",
                    "Current Feishu message timestamp is unavailable",
                )

            requested_limit = min(limit or self.config.thread_message_limit, 100)
            start_time = create_time - self.config.thread_lookback_days * 86_400
            page_token: str | None = None
            all_items: list[dict[str, Any]] = []
            upstream_truncated = False
            for page in range(self.config.thread_max_pages):
                items, page_token, has_more = await self.client.list_chat_messages(
                    instance,
                    chat_id=request.chat_id,
                    start_time=start_time,
                    end_time=create_time + 1,
                    page_size=50,
                    page_token=page_token,
                )
                all_items.extend(items)
                if not has_more or not page_token:
                    break
                if page + 1 == self.config.thread_max_pages:
                    upstream_truncated = True

            result = extract_thread_messages(
                all_items,
                chat_id=request.chat_id,
                root_id=root_id,
                include_bot_messages=include_bot_messages,
                limit=requested_limit,
                max_chars=self.config.thread_max_chars,
                upstream_truncated=upstream_truncated,
            )
            self.store.record_evidence(
                tenant_id=request.tenant_id or "",
                principal_id=request.principal_id or "",
                audience_id=request.audience_id or "",
                session_key=request.session_key or "",
                channel_instance=request.channel,
                root_id=root_id,
                message_ids=[item.message_id for item in result.messages],
                ttl_seconds=self.config.evidence_ttl_seconds,
            )
            return result.to_untrusted_block()
        except (PendingOperationError, FeishuApiError) as exc:
            return _error(exc.code, str(exc))


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["prepare", "commit"]},
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "description": {"type": "string", "maxLength": 5000},
            "assignee_open_id": {"type": "string", "maxLength": 128},
            "due_at": {"type": "string", "maxLength": 64},
            "source_message_ids": {
                "type": "array",
                "items": {"type": "string", "maxLength": 128},
                "maxItems": 50,
            },
            "confirmation_token": {"type": "string", "maxLength": 32},
        },
        "required": ["action"],
        "additionalProperties": False,
    }
)
class FeishuTaskTool(Tool):
    """Prepare or commit one confirmation-gated Feishu task."""

    config_key = "feishu_actions"

    @classmethod
    def config_cls(cls):
        return FeishuActionToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.config.feishu_actions.enabled and ctx.config.feishu_actions.task_enabled

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(
            config=ctx.config.feishu_actions,
            channels_config=ctx.channels_config,
            client=FeishuOpenApiClient(
                timeout_seconds=ctx.config.feishu_actions.request_timeout_seconds
            ),
            store=_store(ctx.workspace, ctx.config.feishu_actions),
        )

    def __init__(
        self,
        *,
        config: FeishuActionToolConfig,
        channels_config: Any,
        client: FeishuOpenApiClient,
        store: PendingOperationStore,
    ):
        self.config = config
        self.channels_config = channels_config
        self.client = client
        self.store = store

    @property
    def name(self) -> str:
        return "feishu_task"

    @property
    def description(self) -> str:
        return (
            "Create one Feishu task through a mandatory two-step protocol. First call action="
            "'prepare' with the task fields and show the returned exact confirmation command "
            "to the user. Only after the user sends that command, call action='commit' with "
            "the token. Never claim a task was created from prepare alone."
        )

    @staticmethod
    def _due_timestamp(value: str | None) -> int | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PendingOperationError(
                "INVALID_TASK_ARGUMENT", "due_at must be an ISO-8601 timestamp with timezone"
            ) from exc
        if parsed.tzinfo is None:
            raise PendingOperationError(
                "INVALID_TASK_ARGUMENT", "due_at must include a timezone"
            )
        return int(parsed.timestamp() * 1000)

    async def execute(
        self,
        action: Literal["prepare", "commit"],
        title: str | None = None,
        description: str | None = None,
        assignee_open_id: str | None = None,
        due_at: str | None = None,
        source_message_ids: list[str] | None = None,
        confirmation_token: str | None = None,
        **_: Any,
    ) -> Any:
        try:
            request = _request_identity()
            instance = resolve_feishu_instance(self.channels_config, request.channel)
            identity = _instance_identity(instance)
            if action == "prepare":
                return self._prepare(
                    request=request,
                    instance_identity=identity,
                    title=title,
                    description=description,
                    assignee_open_id=assignee_open_id,
                    due_at=due_at,
                    source_message_ids=source_message_ids,
                    confirmation_token=confirmation_token,
                )
            return await self._commit(
                request=request,
                instance=instance,
                instance_identity=identity,
                confirmation_token=confirmation_token,
                supplied_fields=(title, description, assignee_open_id, due_at, source_message_ids),
            )
        except (PendingOperationError, FeishuApiError) as exc:
            return _error(exc.code, str(exc))

    def _prepare(
        self,
        *,
        request,
        instance_identity: str,
        title: str | None,
        description: str | None,
        assignee_open_id: str | None,
        due_at: str | None,
        source_message_ids: list[str] | None,
        confirmation_token: str | None,
    ) -> str:
        if confirmation_token:
            raise PendingOperationError(
                "INVALID_TASK_ARGUMENT", "prepare must not include confirmation_token"
            )
        normalized_title = (title or "").strip()
        assignee = (assignee_open_id or "").strip()
        if not normalized_title or not assignee:
            raise PendingOperationError(
                "INVALID_TASK_ARGUMENT", "prepare requires title and assignee_open_id"
            )
        if not _OPEN_ID_RE.fullmatch(assignee):
            raise PendingOperationError(
                "INVALID_TASK_ARGUMENT", "assignee_open_id must be a Feishu open_id"
            )
        due_at_ms = self._due_timestamp(due_at)
        message_ids = list(dict.fromkeys(source_message_ids or []))
        self.store.validate_evidence(
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            audience_id=request.audience_id,
            session_key=request.session_key,
            channel_instance=request.channel,
            message_ids=message_ids,
        )
        payload = {
            "title": normalized_title,
            "description": (description or "").strip(),
            "assignee_open_id": assignee,
            "due_at": due_at,
            "due_at_ms": due_at_ms,
            "source_message_ids": message_ids,
        }
        operation, token = self.store.prepare(
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            audience_id=request.audience_id,
            session_key=request.session_key,
            channel_instance=request.channel,
            instance_identity=instance_identity,
            payload=payload,
            ttl_seconds=self.config.confirmation_ttl_seconds,
        )
        if operation.status == "succeeded":
            return json.dumps(
                {
                    "status": "succeeded",
                    "task_id": operation.remote_task_id,
                    "task_url": operation.remote_task_url,
                    "idempotency_reused": True,
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "status": "pending_confirmation",
                "operation_id": operation.id,
                "preview": payload,
                "confirmation_token": token,
                "confirmation_command": f"确认创建 {token}",
                "expires_at": operation.expires_at,
            },
            ensure_ascii=False,
        )

    async def _commit(
        self,
        *,
        request,
        instance,
        instance_identity: str,
        confirmation_token: str | None,
        supplied_fields: tuple[Any, ...],
    ) -> str:
        token = (confirmation_token or "").strip()
        if not _TOKEN_RE.fullmatch(token):
            raise PendingOperationError(
                "CONFIRMATION_REQUIRED", "commit requires a valid confirmation token"
            )
        if any(value not in (None, [], "") for value in supplied_fields):
            raise PendingOperationError(
                "PAYLOAD_MISMATCH", "commit cannot override prepared task fields"
            )
        expected = f"确认创建 {token}"
        if (request.original_user_text or "").strip() != expected:
            raise PendingOperationError(
                "CONFIRMATION_REQUIRED",
                f"User must send the exact confirmation command: {expected}",
            )
        operation = self.store.claim(
            token=token,
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            audience_id=request.audience_id,
            session_key=request.session_key,
            channel_instance=request.channel,
            instance_identity=instance_identity,
        )
        if operation.status == "succeeded":
            return json.dumps(
                {
                    "status": "succeeded",
                    "task_id": operation.remote_task_id,
                    "task_url": operation.remote_task_url,
                    "idempotency_reused": True,
                },
                ensure_ascii=False,
            )
        payload = operation.payload
        try:
            result = await self.client.create_task(
                instance,
                title=payload["title"],
                description=payload["description"],
                assignee_open_id=payload["assignee_open_id"],
                due_at_ms=payload["due_at_ms"],
                client_token=operation.idempotency_key,
            )
        except FeishuApiError as exc:
            self.store.fail(operation.id, code=exc.code, retryable=exc.retryable)
            raise
        completed = self.store.succeed(
            operation.id,
            task_id=result.task_id,
            task_url=result.task_url,
        )
        return json.dumps(
            {
                "status": "succeeded",
                "task_id": completed.remote_task_id,
                "task_url": completed.remote_task_url,
                "idempotency_reused": False,
                "committed_at": int(time.time()),
            },
            ensure_ascii=False,
        )
