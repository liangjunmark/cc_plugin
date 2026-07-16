from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from proxy.config import ProxyConfig
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
    streamed: bool = False


class UpstreamTransport:
    def __init__(self, client: httpx.AsyncClient, config: ProxyConfig) -> None:
        self.client = client
        self.config = config

    async def send_once(
        self,
        headers: dict[str, str],
        body: dict[str, Any],
        stream: bool,
    ) -> httpx.Response:
        request = self.client.build_request(
            "POST",
            f"{self.config.upstream.base_url}/v1/messages",
            headers=headers,
            json=body,
            timeout=httpx.Timeout(
                self.config.upstream.timeout_seconds,
                connect=self.config.upstream.connect_timeout_seconds,
            ),
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
            response = await self.send_once(headers, body, stream)
            content = await response.aread()
            result = UpstreamResult(
                status_code=response.status_code,
                headers=dict(response.headers),
                body=content,
                streamed=stream,
            )
            await response.aclose()

            if (
                not replay_safe
                or response.status_code not in self.config.upstream.retry_statuses
                or not budget.consume()
            ):
                return result

            context.attempt += 1


async def probe_stream_support(client: httpx.AsyncClient, config: ProxyConfig) -> bool:
    payload = {
        "model": "probe",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "reply with 1"}],
        "stream": True,
    }
    response = await client.post(f"{config.upstream.base_url}/v1/messages", json=payload)
    return response.status_code < 500
