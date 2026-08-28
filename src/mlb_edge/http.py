"""HTTP fetching with rate limiting, retries and honest failure.

Every upstream here is free or cheap and will rate-limit or ban an impolite
client, so throttling is opt-out rather than opt-in. Retries are exponential and
bounded; a 4xx that is not 429 is *not* retried, because hammering a URL that
returned "you asked for the wrong thing" only gets the IP blocked.

The CA bundle and proxy settings are read from the standard environment
variables. Certificate verification is never disabled -- a TLS failure here
means the environment is wrong, and quietly ignoring it would mean not knowing
whether the bytes came from the upstream at all.
"""

from __future__ import annotations

import os
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class UpstreamError(RuntimeError):
    """A non-retryable upstream failure."""

    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class RetryableUpstreamError(UpstreamError):
    """A transient upstream failure worth another attempt."""


class BudgetExceeded(RuntimeError):
    """Raised when a metered API's request budget would be exceeded."""


@dataclass
class Response:
    url: str
    status: int
    content: bytes
    headers: dict[str, str]

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "application/octet-stream").split(";")[0].strip()

    def json(self) -> Any:
        import json

        return json.loads(self.content)

    def text(self, encoding: str = "utf-8") -> str:
        return self.content.decode(encoding, errors="replace")


class RateLimiter:
    """Sliding-window limiter, shared across threads."""

    def __init__(self, per_minute: float) -> None:
        self.per_minute = max(float(per_minute), 0.0)
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if self.per_minute <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                while self._events and now - self._events[0] >= 60.0:
                    self._events.popleft()
                if len(self._events) < self.per_minute:
                    self._events.append(now)
                    return
                sleep_for = 60.0 - (now - self._events[0]) + 0.01
            time.sleep(min(sleep_for, 60.0))


class HttpClient:
    """Configured fetcher for one source."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float = 30.0,
        max_attempts: int = 5,
        backoff_initial_seconds: float = 2.0,
        backoff_max_seconds: float = 60.0,
        rate_limit_per_minute: float = 60.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.max_attempts = max_attempts
        self.backoff_initial_seconds = backoff_initial_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.limiter = RateLimiter(rate_limit_per_minute)

        default_headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        default_headers.update(headers or {})

        # Build an explicit SSL context from the environment's CA bundle.
        # Verification is never disabled: a TLS failure means the environment is
        # misconfigured, and silently accepting it would mean not knowing whether
        # the bytes came from the upstream at all.
        verify: ssl.SSLContext | bool = True
        ca_bundle = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
        if ca_bundle and os.path.isfile(ca_bundle):
            verify = ssl.create_default_context(cafile=ca_bundle)

        self._client = httpx.Client(
            headers=default_headers,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
            verify=verify,
            trust_env=True,   # picks up HTTPS_PROXY / NO_PROXY
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        return self._request("GET", url, params=params, headers=headers)

    def post(
        self,
        url: str,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        return self._request("POST", url, json_body=json_body, headers=headers)

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        def _log_retry(state: RetryCallState) -> None:
            exc = state.outcome.exception() if state.outcome else None
            print(f"  [http] retry {state.attempt_number}/{self.max_attempts} {url} :: {exc}")

        @retry(
            retry=retry_if_exception_type(RetryableUpstreamError),
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(
                multiplier=self.backoff_initial_seconds, max=self.backoff_max_seconds
            ),
            before_sleep=_log_retry,
            reraise=True,
        )
        def _attempt() -> Response:
            self.limiter.acquire()
            try:
                response = self._client.request(
                    method, url, params=params, json=json_body, headers=headers
                )
            except httpx.TimeoutException as exc:
                raise RetryableUpstreamError(f"timeout: {exc}", url=url) from exc
            except httpx.TransportError as exc:
                # Covers proxy CONNECT failures, DNS, TLS. Retryable because a
                # transient network blip looks identical to a real outage here;
                # the attempt cap stops it becoming a hammer.
                raise RetryableUpstreamError(f"transport error: {exc}", url=url) from exc

            if response.status_code == 429 or response.status_code >= 500:
                raise RetryableUpstreamError(
                    f"HTTP {response.status_code}", status=response.status_code, url=url
                )
            if response.status_code >= 400:
                snippet = response.text[:300].replace("\n", " ")
                raise UpstreamError(
                    f"HTTP {response.status_code} for {url}: {snippet}",
                    status=response.status_code,
                    url=url,
                )
            return Response(
                url=str(response.url),
                status=response.status_code,
                content=response.content,
                headers={k.lower(): v for k, v in response.headers.items()},
            )

        return _attempt()


def client_for(settings: Any, source_name: str, headers: dict[str, str] | None = None) -> HttpClient:
    """Build a client using the http block plus the source's rate limit."""
    http_cfg = settings.section("http")
    source = settings.source(source_name)
    return HttpClient(
        user_agent=http_cfg.get("user_agent", "mlb-edge/0.1"),
        timeout_seconds=float(http_cfg.get("timeout_seconds", 30)),
        max_attempts=int(http_cfg.get("max_attempts", 5)),
        backoff_initial_seconds=float(http_cfg.get("backoff_initial_seconds", 2)),
        backoff_max_seconds=float(http_cfg.get("backoff_max_seconds", 60)),
        rate_limit_per_minute=float(source.get("rate_limit_per_minute", 60)),
        headers=headers,
    )
