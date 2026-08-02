"""Minimal Feishu OpenAPI adapter used by Agent tools.

The adapter intentionally uses stable REST contracts instead of importing the
optional lark-oapi package. This keeps tool discovery available when the
Feishu channel plugin is not installed and makes API behavior easy to mock.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from nanobot.security.network import validate_url_target

_BASE_URLS = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}


class FeishuApiError(RuntimeError):
    """Stable OpenAPI error with a machine-readable code."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class FeishuInstance:
    runtime_name: str
    instance_id: str
    app_id: str
    app_secret: str
    domain: str
    identity_key: str

    @property
    def base_url(self) -> str:
        return _BASE_URLS.get(self.domain, _BASE_URLS["feishu"])


@dataclass(frozen=True)
class FeishuTaskResult:
    task_id: str
    task_url: str


def resolve_feishu_instance(channels_config: Any, runtime_name: str) -> FeishuInstance:
    """Resolve exactly the Feishu instance that received the current request."""
    if runtime_name == "feishu":
        instance_id = "default"
    elif runtime_name.startswith("feishu."):
        instance_id = runtime_name.split(".", 1)[1]
    else:
        raise FeishuApiError("FEISHU_CONTEXT_REQUIRED", "Current request is not from Feishu")

    section = getattr(channels_config, "feishu", None)
    if hasattr(section, "model_dump"):
        section = section.model_dump(mode="json", by_alias=True)
    section = section if isinstance(section, dict) else {}
    raw_instances = section.get("instances")
    if isinstance(raw_instances, list):
        inherited = {key: value for key, value in section.items() if key != "instances"}
        instances = [
            {**inherited, **item}
            for item in raw_instances
            if isinstance(item, dict)
        ]
    else:
        instances = [section]

    for index, config in enumerate(instances):
        raw_id = config.get("id") or config.get("instanceId") or config.get("instance_id")
        configured_id = str(raw_id or ("default" if index == 0 else f"assistant-{index + 1}"))
        if configured_id != instance_id:
            continue
        app_id = str(config.get("appId") or config.get("app_id") or "")
        app_secret = str(config.get("appSecret") or config.get("app_secret") or "")
        if not app_id or not app_secret:
            raise FeishuApiError(
                "FEISHU_CREDENTIALS_MISSING",
                f"Feishu instance {runtime_name!r} has no app credentials",
            )
        domain = str(config.get("domain") or "feishu").lower()
        return FeishuInstance(
            runtime_name=runtime_name,
            instance_id=instance_id,
            app_id=app_id,
            app_secret=app_secret,
            domain="lark" if domain == "lark" else "feishu",
            identity_key=str(config.get("identityKey") or config.get("identity_key") or ""),
        )
    raise FeishuApiError(
        "FEISHU_INSTANCE_NOT_FOUND",
        f"Feishu instance {runtime_name!r} is not configured",
    )


class FeishuOpenApiClient:
    """Async REST client with per-instance tenant-token caching."""

    def __init__(self, *, timeout_seconds: float = 10.0):
        self.timeout_seconds = timeout_seconds
        self._tokens: dict[str, tuple[str, float]] = {}
        self._token_locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _validate_url(url: str) -> None:
        ok, error = validate_url_target(url)
        if not ok:
            raise FeishuApiError("FEISHU_API_UNSAFE_URL", error)

    async def _tenant_token(self, instance: FeishuInstance) -> str:
        cache_key = f"{instance.domain}:{instance.app_id}"
        cached = self._tokens.get(cache_key)
        if cached and cached[1] > time.monotonic() + 60:
            return cached[0]

        lock = self._token_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = self._tokens.get(cache_key)
            if cached and cached[1] > time.monotonic() + 60:
                return cached[0]
            url = f"{instance.base_url}/open-apis/auth/v3/tenant_access_token/internal"
            data = await self._request_json(
                "POST",
                url,
                json={"app_id": instance.app_id, "app_secret": instance.app_secret},
                authenticated=False,
            )
            token = str(data.get("tenant_access_token") or "")
            if not token:
                raise FeishuApiError(
                    "FEISHU_AUTH_FAILED",
                    "Feishu token response did not include tenant_access_token",
                )
            expires = max(1, int(data.get("expire") or 7200))
            self._tokens[cache_key] = (token, time.monotonic() + expires)
            return token

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        instance: FeishuInstance | None = None,
        authenticated: bool = True,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._validate_url(url)
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if authenticated:
            if instance is None:
                raise ValueError("authenticated Feishu request requires instance")
            headers["Authorization"] = f"Bearer {await self._tenant_token(instance)}"

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                )
        except httpx.TimeoutException as exc:
            raise FeishuApiError(
                "FEISHU_API_UNAVAILABLE", "Feishu API request timed out", retryable=True
            ) from exc
        except httpx.RequestError as exc:
            raise FeishuApiError(
                "FEISHU_API_UNAVAILABLE", f"Feishu API request failed: {exc}", retryable=True
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise FeishuApiError(
                "FEISHU_API_INVALID_RESPONSE",
                f"Feishu API returned HTTP {response.status_code} with non-JSON body",
                retryable=response.status_code >= 500,
            ) from exc

        code = int(payload.get("code") or 0)
        if response.status_code >= 400 or code != 0:
            if response.status_code in {401, 403}:
                stable_code = "FEISHU_PERMISSION_DENIED"
            elif response.status_code == 429:
                stable_code = "FEISHU_RATE_LIMITED"
            else:
                stable_code = "FEISHU_API_UNAVAILABLE"
            message = str(payload.get("msg") or payload.get("message") or response.reason_phrase)
            raise FeishuApiError(
                stable_code,
                f"Feishu API error {code or response.status_code}: {message}",
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        data = payload.get("data")
        return data if isinstance(data, dict) else payload

    async def list_chat_messages(
        self,
        instance: FeishuInstance,
        *,
        chat_id: str,
        start_time: int,
        end_time: int,
        page_size: int,
        page_token: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        params: dict[str, Any] = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "start_time": str(start_time),
            "end_time": str(end_time),
            # Read newest pages first so a busy chat cannot consume the page
            # budget before the current topic is reached.
            "sort_type": "ByCreateTimeDesc",
            "page_size": page_size,
        }
        if page_token:
            params["page_token"] = page_token
        data = await self._request_json(
            "GET",
            f"{instance.base_url}/open-apis/im/v1/messages",
            instance=instance,
            params=params,
        )
        items = data.get("items")
        return (
            [item for item in items if isinstance(item, dict)] if isinstance(items, list) else [],
            str(data.get("page_token") or "") or None,
            bool(data.get("has_more")),
        )

    async def create_task(
        self,
        instance: FeishuInstance,
        *,
        title: str,
        description: str,
        assignee_open_id: str,
        due_at_ms: int | None,
        client_token: str,
    ) -> FeishuTaskResult:
        body: dict[str, Any] = {
            "summary": title,
            "description": description,
            "members": [
                {"id": assignee_open_id, "type": "user", "role": "assignee"}
            ],
            "client_token": client_token,
        }
        if due_at_ms is not None:
            body["due"] = {"timestamp": due_at_ms, "is_all_day": False}
        data = await self._request_json(
            "POST",
            f"{instance.base_url}/open-apis/task/v2/tasks",
            instance=instance,
            params={"user_id_type": "open_id"},
            json=body,
        )
        task = data.get("task")
        if not isinstance(task, dict):
            raise FeishuApiError(
                "FEISHU_API_INVALID_RESPONSE",
                "Feishu task response did not include task",
            )
        task_id = str(task.get("guid") or task.get("task_id") or "")
        if not task_id:
            raise FeishuApiError(
                "FEISHU_API_INVALID_RESPONSE",
                "Feishu task response did not include a task id",
            )
        return FeishuTaskResult(task_id=task_id, task_url=str(task.get("url") or ""))
