from __future__ import annotations

from copy import deepcopy

import httpx
from fastapi.testclient import TestClient

from proxy.upstream import UpstreamResult


class StubTransport:
    def __init__(self, result: UpstreamResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def send_with_retry(
        self,
        *,
        context,
        headers: dict[str, str],
        body: dict[str, object],
        replay_safe: bool,
        stream: bool,
    ) -> UpstreamResult:
        self.calls.append(
            {
                "request_id": context.request_id,
                "attempt": context.attempt,
                "headers": headers,
                "body": body,
                "replay_safe": replay_safe,
                "stream": stream,
            },
        )
        return self.result


def test_health_endpoint_returns_ok(config) -> None:
    from proxy.app import create_app

    with TestClient(create_app(config)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_proxy_startup_smoke(config) -> None:
    from proxy.app import create_app

    app = create_app(config)

    assert app is not None


def test_factory_create_app_uses_default_example_config() -> None:
    from proxy.app import create_app

    app = create_app()

    assert app is not None


def test_ready_endpoint_reports_probe_state(config) -> None:
    from proxy.app import create_app

    with TestClient(create_app(config)) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert "stream_probe" in response.json()


def test_messages_returns_non_streamed_upstream_json(config) -> None:
    from proxy.app import create_app

    transport = StubTransport(
        UpstreamResult(
            status_code=200,
            headers={"content-type": "application/json"},
            body=b'{"type":"message","id":"msg_1","content":[{"type":"text","text":"21"}]}',
        ),
    )
    body = {
        "model": "test-model",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "reply with 21"}],
    }

    with TestClient(create_app(config, transport=transport)) as client:
        response = client.post("/v1/messages", json=body, headers={"x-request-id": "req-app-1"})

    assert response.status_code == 200
    assert response.json()["type"] == "message"
    assert transport.calls[0]["replay_safe"] is True
    assert transport.calls[0]["stream"] is False
    assert transport.calls[0]["body"] == body


def test_messages_streams_successful_upstream_sse(config) -> None:
    from proxy.app import create_app

    payload = b"event: message\ndata: ok\n\n"
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream; charset=utf-8"},
        content=payload,
    )
    transport = StubTransport(
        UpstreamResult(
            status_code=200,
            headers=dict(response.headers),
            body=b"",
            response=response,
            streamed=True,
        ),
    )
    body = {
        "model": "test-model",
        "max_tokens": 32,
        "stream": True,
        "messages": [{"role": "user", "content": "reply with 21"}],
    }

    with TestClient(create_app(config, transport=transport)) as client:
        streamed = client.post("/v1/messages", json=body)

    assert streamed.status_code == 200
    assert streamed.content == payload
    assert streamed.headers["content-type"].startswith("text/event-stream")
    assert transport.calls[0]["stream"] is True


def test_messages_wraps_non_json_upstream_body_before_downstream_bytes(config) -> None:
    from proxy.app import create_app

    transport = StubTransport(
        UpstreamResult(
            status_code=502,
            headers={"content-type": "text/plain"},
            body=b"gateway exploded",
        ),
    )
    body = {
        "model": "test-model",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "reply with 21"}],
    }

    with TestClient(create_app(config, transport=transport)) as client:
        response = client.post("/v1/messages", json=body)

    assert response.status_code == 502
    assert response.json()["type"] == "error"
    assert response.json()["error"]["message"] == "gateway exploded"


def test_messages_rewrites_when_route_enters_rewrite_band(config) -> None:
    from proxy.app import create_app

    rewritten_config = config.model_copy(deep=True)
    rewritten_config.rewrite.enabled = True
    rewritten_config.rewrite.max_tokens_floor.minimum_output_tokens = 4096
    rewritten_config.classification.reasoning_keyword_patterns = ["minimum"]
    rewritten_config.classification.output_constraint_patterns = ["only output"]
    transport = StubTransport(
        UpstreamResult(
            status_code=200,
            headers={"content-type": "application/json"},
            body=b'{"type":"message","id":"msg_2","content":[{"type":"text","text":"21"}]}',
        ),
    )
    original = {
        "model": "test-model",
        "max_tokens": 32,
        "messages": [
            {
                "role": "user",
                "content": deepcopy(
                    "Find the minimum answer and only output the final number.\n"
                    "Explain nothing else.\n"
                    "This is a long reasoning prompt that exceeds the rewrite threshold."
                ),
            }
        ],
    }

    with TestClient(create_app(rewritten_config, transport=transport)) as client:
        response = client.post("/v1/messages", json=original)

    assert response.status_code == 200
    assert transport.calls[0]["body"]["max_tokens"] == 4096
