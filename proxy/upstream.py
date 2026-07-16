from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

import httpx

from proxy.config import ProxyConfig
from proxy.recorder import Recorder
from proxy.schemas import RequestContext


class RetryBudget:
    def __init__(self, max_retries: int) -> None:
        self.max_retries = max_retries
        self.spent = 0

    def consume(self) -> bool:
        if self.spent >= self.max_retries:
            return False
        self.spent += 1
        return True


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
    ) -> UpstreamResult:
        budget = RetryBudget(self.config.upstream.max_retries)

        while True:
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

            if replay_safe and response.status_code in self.config.upstream.retry_statuses and budget.consume():
                self._record(
                    context,
                    f"attempt-{context.attempt}-response-meta",
                    {"status_code": response.status_code, "headers": dict(response.headers), "retrying": True},
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
            self._record(
                context,
                f"attempt-{context.attempt}-response",
                {"status_code": result.status_code, "headers": result.headers, "body": _decode_body(content)},
            )
            await response.aclose()

            if (
                not replay_safe
                or response.status_code not in self.config.upstream.retry_statuses
                or not budget.consume()
            ):
                return result

            context.attempt += 1

    def _record(self, context: RequestContext, name: str, payload: dict[str, Any]) -> None:
        if self.recorder is None:
            return
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
