from pathlib import Path

import httpx
import pytest

from proxy.recorder import Recorder
from proxy.schemas import RequestContext
from proxy.upstream import CapabilityCache, RetryBudget, UpstreamTransport, probe_stream_support


def test_retry_budget_allows_only_one_retry_total() -> None:
    budget = RetryBudget(max_retries=1)

    assert budget.consume() is True
    assert budget.consume() is False


def test_capability_cache_requires_positive_evidence() -> None:
    cache = CapabilityCache()

    assert cache.is_supported("xf", "thinking") is False
    cache.mark_supported("xf", "thinking")
    assert cache.is_supported("xf", "thinking") is True


@pytest.mark.asyncio
async def test_send_once_posts_message_to_upstream(config: object) -> None:
    seen_request: httpx.Request | None = None

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = request
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = UpstreamTransport(client, config)
        response = await transport.send_once({"x-test": "value"}, {"model": "test"}, stream=False)

    assert response.status_code == 200
    assert seen_request is not None
    assert seen_request.url == "https://example.com/anthropic/v1/messages"
    assert seen_request.headers["x-test"] == "value"


@pytest.mark.asyncio
async def test_send_with_retry_retries_retryable_status_once(config: object, tmp_path: Path) -> None:
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": "retry"})
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = UpstreamTransport(client, config, Recorder(tmp_path, config))
        result = await transport.send_with_retry(
            context=RequestContext(request_id="req-1", attempt=1, log_dir=Path("req-1")),
            headers={},
            body={"model": "test"},
            replay_safe=True,
            stream=False,
        )

    assert calls == 2
    assert result.status_code == 200


@pytest.mark.asyncio
async def test_send_with_retry_does_not_retry_when_not_replay_safe(config: object, tmp_path: Path) -> None:
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": "retry"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = UpstreamTransport(client, config, Recorder(tmp_path, config))
        result = await transport.send_with_retry(
            context=RequestContext(request_id="req-2", attempt=1, log_dir=Path("req-2")),
            headers={},
            body={"model": "test"},
            replay_safe=False,
            stream=False,
        )

    assert calls == 1
    assert result.status_code == 503


@pytest.mark.asyncio
async def test_send_with_retry_retries_transport_error_once(config: object, tmp_path: Path) -> None:
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = UpstreamTransport(client, config, Recorder(tmp_path, config))
        result = await transport.send_with_retry(
            context=RequestContext(request_id="req-3", attempt=1, log_dir=Path("req-3")),
            headers={},
            body={"model": "test"},
            replay_safe=True,
            stream=False,
        )

    assert calls == 2
    assert result.status_code == 200


@pytest.mark.asyncio
async def test_send_with_retry_preserves_streaming_response(config: object, tmp_path: Path) -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"event: message\ndata: ok\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = UpstreamTransport(client, config, Recorder(tmp_path, config))
        result = await transport.send_with_retry(
            context=RequestContext(request_id="req-4", attempt=1, log_dir=Path("req-4")),
            headers={},
            body={"model": "test"},
            replay_safe=True,
            stream=True,
        )
        body = await result.response.aread()
        await result.response.aclose()

    assert result.streamed is True
    assert result.response is not None
    assert body == b"event: message\ndata: ok\n\n"


@pytest.mark.asyncio
async def test_probe_stream_support_returns_false_on_transport_error(config: object) -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        supported = await probe_stream_support(client, config)

    assert supported is False
