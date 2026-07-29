"""Fail-closed JSON HTTP transport with bounded retries and rate limiting."""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import Message
from typing import Any


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    initial_backoff_s: float = 0.5
    max_backoff_s: float = 8.0
    jitter_ratio: float = 0.1


class RateLimiter:
    """Process-local minimum-interval limiter shared by one provider client."""

    def __init__(self, requests_per_second: float = 2.0):
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._interval = 1.0 / requests_per_second
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_at - now)
            if delay:
                time.sleep(delay)
            self._next_at = max(now, self._next_at) + self._interval


class JsonHttpClient:
    def __init__(
        self,
        *,
        timeout: float,
        requests_per_second: float = 2.0,
        retry_policy: RetryPolicy | None = None,
    ):
        self.timeout = timeout
        self.rate_limiter = RateLimiter(requests_per_second)
        self.retry_policy = retry_policy or RetryPolicy()

    def post(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        policy = self.retry_policy
        for attempt in range(1, policy.max_attempts + 1):
            self.rate_limiter.wait()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8")
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("Provider response must be a JSON object")
                return body
            except urllib.error.HTTPError as exc:
                if exc.code != 429 and not 500 <= exc.code < 600:
                    raise
                if attempt == policy.max_attempts:
                    raise
                delay = _retry_after(exc.headers) or _backoff(policy, attempt)
            except (urllib.error.URLError, TimeoutError):
                if attempt == policy.max_attempts:
                    raise
                delay = _backoff(policy, attempt)
            time.sleep(delay)
        raise AssertionError("retry loop exhausted")  # pragma: no cover


def _retry_after(headers: Message | None) -> float | None:
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _backoff(policy: RetryPolicy, attempt: int) -> float:
    base = min(policy.max_backoff_s, policy.initial_backoff_s * 2 ** (attempt - 1))
    jitter = base * policy.jitter_ratio * random.random()
    return base + jitter
