from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any
from urllib.parse import urlparse

import httpx

from proxy.config import ProxyConfig
from proxy.recorder import Recorder
from proxy.schemas import RequestContext

XFYUN_HOST_MARKER = "xf-yun.com"
XFYUN_TRANSIENT_API_ERROR_MARKERS = (
    "prefill transfer failed",
    "decode instance could be dead",
    "session",
    "is not alive",
)


class RetryBudget:
    def __init__(self, max_retries: int) -> None:
        self.max_retries = max_retries
        self.spent = 0

    def consume(self) -> bool:
        if self.spent >= self.max_retries:
            return False
        self.spent += 1
        return True


class CallBudget:
    def __init__(self, max_calls: int) -> None:
        self.max_calls = max_calls
        self.spent = 0

    def consume(self) -> bool:
        if self.spent >= self.max_calls:
            return False
        self.spent += 1
        return True

    @property
    def remaining(self) -> int:
        return self.max_calls - self.spent


@dataclass
class CapabilityCache:
    support: dict[str, set[str]] = field(default_factory=dict)

    def mark_supported(self, provider_key: str, field_family: str) -> None:
        self.support.setdefault(provider_key, set()).add(field_family)

    def is_supported(self, provider_key: str, field_family: str) -> bool:
        return field_family in self.support.get(provider_key, set())


@dataclass
class UpstreamResult:
    status_code: int
    headers: dict[str, str]
    body: bytes
    response: httpx.Response | None = None
    streamed: bool = False


class UpstreamTransport:
    def __init__(
        self,
        client: httpx.AsyncClient,
        config: ProxyConfig,
        recorder: Recorder | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.recorder = recorder

    async def send_once(
        self,
        headers: dict[str, str],
        body: dict[str, Any],
        stream: bool,
    ) -> httpx.Response:
        timeout = httpx.Timeout(
            self.config.upstream.timeout_seconds,
            connect=self.config.upstream.connect_timeout_seconds,
        )
        request = self.client.build_request(
            "POST",
            _messages_url(self.config),
            headers=headers,
            json=body,
            timeout=timeout,
        )
        return await self.client.send(request, stream=stream)

    async def send_with_retry(
        self,
        context: RequestContext,
        headers: dict[str, str],
        body: dict[str, Any],
        replay_safe: bool,
        stream: bool,
        call_budget: CallBudget | None = None,
    ) -> UpstreamResult:
        budget = RetryBudget(self.config.upstream.max_retries)

        while True:
            if call_budget is not None and not call_budget.consume():
                return UpstreamResult(
                    status_code=599,
                    headers={},
                    body=b"upstream call budget exhausted",
                    streamed=False,
                )
            self._record(context, f"attempt-{context.attempt}-request", {"headers": headers, "body": body})
            try:
                response = await self.send_once(headers, body, stream)
            except httpx.HTTPError as exc:
                self._record(context, f"attempt-{context.attempt}-transport-error", {"error": str(exc)})
                if replay_safe and budget.consume():
                    context.attempt += 1
                    continue
                return UpstreamResult(
                    status_code=599,
                    headers={},
                    body=str(exc).encode("utf-8"),
                    streamed=False,
                )

            retry_reason = _retry_reason_from_status(self.config, response.status_code) if replay_safe else None
            if retry_reason is not None and budget.consume():
                self._record(
                    context,
                    f"attempt-{context.attempt}-response-meta",
                    {
                        "status_code": response.status_code,
                        "headers": dict(response.headers),
                        "retrying": True,
                        "retry_reason": retry_reason,
                    },
                )
                await response.aclose()
                context.attempt += 1
                continue

            if stream and response.status_code < 400:
                self._record(
                    context,
                    f"attempt-{context.attempt}-response-meta",
                    {"status_code": response.status_code, "headers": dict(response.headers), "streamed": True},
                )
                return UpstreamResult(
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    body=b"",
                    response=response,
                    streamed=True,
                )

            content = await response.aread()
            result = UpstreamResult(
                status_code=response.status_code,
                headers=dict(response.headers),
                body=content,
                response=None,
                streamed=False,
            )
            retry_reason = _retry_reason_for_response(
                self.config,
                result.status_code,
                content,
            ) if replay_safe else None
            should_retry = retry_reason is not None
            self._record(
                context,
                f"attempt-{context.attempt}-response",
                {
                    "status_code": result.status_code,
                    "headers": result.headers,
                    "body": _decode_body(content),
                    "retrying": should_retry,
                    "retry_reason": retry_reason,
                },
            )
            await response.aclose()

            if not should_retry or not budget.consume():
                return result

            context.attempt += 1

    def _record(self, context: RequestContext, name: str, payload: dict[str, Any]) -> None:
        if self.recorder is None:
            return
        if context.metadata:
            self.recorder.write_artifact(context, "metadata", context.metadata)
        self.recorder.write_artifact(context, name, payload)


async def probe_stream_support(client: httpx.AsyncClient, config: ProxyConfig) -> bool:
    payload = {
        "model": "probe",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "reply with 1"}],
        "stream": True,
    }
    try:
        response = await client.post(_messages_url(config), json=payload)
    except httpx.HTTPError:
        return False
    return response.status_code < 500


def _messages_url(config: ProxyConfig) -> str:
    return f"{config.upstream.base_url.rstrip('/')}/v1/messages"


def _decode_body(content: bytes) -> Any:
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return content.decode("utf-8", errors="replace")


def _retry_reason_from_status(config: ProxyConfig, status_code: int) -> str | None:
    if status_code in config.upstream.retry_statuses:
        return f"status_code_{status_code}"
    return None


def _retry_reason_for_response(config: ProxyConfig, status_code: int, content: bytes) -> str | None:
    status_reason = _retry_reason_from_status(config, status_code)
    if status_reason is not None:
        return status_reason
    if _is_known_xfyun_transient_api_error(config.upstream.base_url, status_code, content):
        return "xfyun_transient_api_error"
    return None


def _is_known_xfyun_transient_api_error(base_url: str, status_code: int, content: bytes) -> bool:
    if status_code != 500 or not _is_xfyun_upstream(base_url):
        return False
    payload = _decode_body(content)
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if not isinstance(error, dict) or error.get("type") != "api_error":
        return False
    message = str(error.get("message", "")).lower()
    return all(marker in message for marker in XFYUN_TRANSIENT_API_ERROR_MARKERS)


def _is_xfyun_upstream(base_url: str) -> bool:
    hostname = urlparse(base_url).hostname or ""
    return XFYUN_HOST_MARKER in hostname.lower()
