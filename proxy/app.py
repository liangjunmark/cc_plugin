from __future__ import annotations

from contextlib import asynccontextmanager
import json
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from proxy.config import ProxyConfig
from proxy.normalize import anthropic_error, filter_forward_headers
from proxy.recorder import Recorder
from proxy.rewrite import apply_rewrites, classify_request
from proxy.schemas import RequestContext
from proxy.upstream import CapabilityCache, UpstreamResult, UpstreamTransport, probe_stream_support


def create_app(
    config: ProxyConfig,
    transport: UpstreamTransport | Any | None = None,
    recorder: Recorder | None = None,
    capability_cache: CapabilityCache | None = None,
) -> FastAPI:
    state: dict[str, Any] = {
        "config": config,
        "transport": transport,
        "recorder": recorder,
        "capability_cache": capability_cache or CapabilityCache(),
        "stream_probe_supported": None,
        "http_client": None,
    }

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if state["transport"] is None:
            client = httpx.AsyncClient()
            state["http_client"] = client
            active_recorder = state["recorder"] or Recorder(Path(config.server.request_log_dir), config)
            state["recorder"] = active_recorder
            state["transport"] = UpstreamTransport(client, config, active_recorder)
            if config.upstream.stream_probe_on_startup:
                state["stream_probe_supported"] = await probe_stream_support(client, config)
        try:
            yield
        finally:
            client = state.get("http_client")
            if client is not None:
                await client.aclose()

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict[str, object]:
        return {
            "status": "ready",
            "stream_probe": {
                "enabled": config.upstream.stream_probe_on_startup,
                "supported": state["stream_probe_supported"],
            },
        }

    @app.post("/v1/messages")
    async def messages(payload: dict[str, Any], request: Request):
        request_id = request.headers.get("x-request-id", str(uuid4()))
        forward_headers = filter_forward_headers(dict(request.headers), set())
        classification = classify_request(payload, config)
        outgoing_body = payload
        if config.rewrite.enabled and classification.route == "rewrite":
            rewrite_result = apply_rewrites(
                outgoing_body,
                config,
                capability_flags={
                    "thinking": state["capability_cache"].is_supported(config.upstream.base_url, "thinking"),
                },
            )
            outgoing_body = rewrite_result.body

        context = RequestContext(
            request_id=request_id,
            attempt=1,
            log_dir=Path(request_id),
        )
        upstream = await state["transport"].send_with_retry(
            context=context,
            headers=forward_headers,
            body=outgoing_body,
            replay_safe=classification.replay_safe,
            stream=bool(outgoing_body.get("stream")),
        )

        if upstream.streamed:
            if upstream.response is None:
                return JSONResponse(
                    status_code=502,
                    content=anthropic_error(502, "streamed upstream response missing body"),
                )
            return StreamingResponse(
                _aiter_upstream_bytes(upstream.response),
                status_code=upstream.status_code,
                media_type=_stream_media_type(upstream.headers),
            )

        parsed = _parse_upstream_json(upstream)
        return JSONResponse(status_code=upstream.status_code, content=parsed)

    return app


async def _aiter_upstream_bytes(response: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    finally:
        await response.aclose()


def _stream_media_type(headers: dict[str, str]) -> str:
    return headers.get("content-type", "text/event-stream")


def _parse_upstream_json(upstream: UpstreamResult) -> dict[str, Any]:
    try:
        parsed = json.loads(upstream.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        message = upstream.body.decode("utf-8", errors="replace")
        return anthropic_error(upstream.status_code, message)
    if isinstance(parsed, dict):
        return parsed
    return anthropic_error(upstream.status_code, "upstream returned a non-object JSON payload")
