import httpx
import pytest

from proxy.upstream import CapabilityCache, RetryBudget, UpstreamTransport


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
