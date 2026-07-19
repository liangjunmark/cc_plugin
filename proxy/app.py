from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from proxy.config import ProxyConfig, load_config, validate_runtime_config
from proxy.normalize import anthropic_error, filter_forward_headers
from proxy.phase2 import is_phase2_eligible, run_phase2
from proxy.phase2b import is_phase2b_eligible, prepare_phase2b_body, run_phase2b
from proxy.recorder import Recorder
from proxy.rewrite import apply_rewrites, classify_request
from proxy.schemas import RequestContext
from proxy.upstream import CapabilityCache, UpstreamResult, UpstreamTransport, probe_stream_support

PHASE_SELECTOR_HEADER = "x-cc-proxy-phase"


def create_app(
    config: ProxyConfig | None = None,
    transport: UpstreamTransport | Any | None = None,
    recorder: Recorder | None = None,
    capability_cache: CapabilityCache | None = None,
) -> FastAPI:
    active_config = config or _load_default_config()
    state: dict[str, Any] = {
        "config": active_config,
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
            active_recorder = state["recorder"] or Recorder(Path(active_config.server.request_log_dir), active_config)
            state["recorder"] = active_recorder
            state["transport"] = UpstreamTransport(client, active_config, active_recorder)
            if active_config.upstream.stream_probe_on_startup:
                state["stream_probe_supported"] = await probe_stream_support(client, active_config)
        try:
            yield
        finally:
            client = state.get("http_client")
            if client is not None:
                await client.aclose()

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=anthropic_error(400, "invalid request payload", "invalid_request_error"),
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict[str, object]:
        return {
            "status": "ready",
            "stream_probe": {
                "enabled": active_config.upstream.stream_probe_on_startup,
                "supported": state["stream_probe_supported"],
            },
        }

    @app.post("/v1/messages")
    async def messages(payload: dict[str, Any], request: Request):
        validation_error = _validate_request_payload(payload)
        if validation_error is not None:
            return JSONResponse(
                status_code=400,
                content=anthropic_error(400, validation_error, "invalid_request_error"),
            )
        request_id = request.headers.get("x-request-id", str(uuid4()))
        request_kind = _request_kind(payload)
        phase_selector = request.headers.get(PHASE_SELECTOR_HEADER, "auto").strip().lower()
        forward_headers = {
            name: value
            for name, value in filter_forward_headers(dict(request.headers), set()).items()
            if name.lower() != PHASE_SELECTOR_HEADER
        }
        classification = classify_request(payload, active_config)
        outgoing_body = payload
        if active_config.rewrite.enabled and classification.route == "rewrite":
            rewrite_result = apply_rewrites(
                outgoing_body,
                active_config,
                capability_flags={
                    "thinking": state["capability_cache"].is_supported(active_config.upstream.base_url, "thinking"),
                },
            )
            outgoing_body = rewrite_result.body

        streamed_request = bool(outgoing_body.get("stream"))
        phase2b_body = prepare_phase2b_body(
            outgoing_body,
            classification,
            active_config,
            request_kind=request_kind,
        )
        phase2b_classification = classify_request(phase2b_body, active_config)
        should_run_phase2b = False
        if phase_selector == "phase2b":
            should_run_phase2b = is_phase2b_eligible(phase2b_body, phase2b_classification, active_config)
        elif phase_selector not in {"phase1", "phase2"}:
            should_run_phase2b = is_phase2b_eligible(phase2b_body, phase2b_classification, active_config)

        if should_run_phase2b:
            phase2b_result = await run_phase2b(
                transport=state["transport"],
                headers=forward_headers,
                body=phase2b_body,
                classification=phase2b_classification,
                config=active_config,
                request_id=request_id,
            )
            if streamed_request:
                if phase2b_result.downstream_payload is not None:
                    return _stream_message_response(phase2b_result.downstream_payload)
                baseline_payload = _successful_message_payload(phase2b_result.baseline_upstream)
                if baseline_payload is not None:
                    return _stream_message_response(baseline_payload)
            if phase2b_result.downstream_payload is not None:
                return JSONResponse(status_code=200, content=phase2b_result.downstream_payload)
            status_code, content = _parse_upstream_json(phase2b_result.baseline_upstream, normalize_error=True)
            return JSONResponse(status_code=status_code, content=content)

        should_run_phase2 = False
        if not bool(outgoing_body.get("stream")):
            if phase_selector == "phase2":
                should_run_phase2 = is_phase2_eligible(outgoing_body, classification, active_config)
            elif phase_selector not in {"phase1", "phase2b"}:
                should_run_phase2 = is_phase2_eligible(outgoing_body, classification, active_config)

        if should_run_phase2:
            phase2_result = await run_phase2(
                transport=state["transport"],
                headers=forward_headers,
                body=outgoing_body,
                classification=classification,
                config=active_config,
                request_id=request_id,
            )
            if phase2_result.downstream_payload is not None:
                return JSONResponse(status_code=200, content=phase2_result.downstream_payload)
            status_code, content = _parse_upstream_json(phase2_result.baseline_upstream, normalize_error=True)
            return JSONResponse(
                status_code=status_code,
                content=content,
            )

        context = RequestContext(
            request_id=request_id,
            attempt=1,
            log_dir=Path(request_id),
            metadata={"request_kind": request_kind},
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
            if not 200 <= upstream.status_code < 300:
                body = await upstream.response.aread()
                await upstream.response.aclose()
                status_code, parsed = _parse_upstream_json(
                    UpstreamResult(
                        status_code=upstream.status_code,
                        headers=upstream.headers,
                        body=body,
                    ),
                    normalize_error=True,
                )
                return JSONResponse(status_code=status_code, content=parsed)
            return StreamingResponse(
                _aiter_upstream_bytes(upstream.response),
                status_code=upstream.status_code,
                media_type=_stream_media_type(upstream.headers),
            )

        status_code, parsed = _parse_upstream_json(upstream, normalize_error=True)
        return JSONResponse(status_code=status_code, content=parsed)

    return app


def _request_kind(payload: dict[str, Any]) -> str:
    system = payload.get("system", "")
    if isinstance(system, list):
        system_text = "\n".join(
            text
            for item in system
            if isinstance(item, dict)
            for text in [_flatten_content_block(item)]
            if text
        )
    else:
        system_text = str(system)
    lowered = system_text.lower()
    if "generate a concise, sentence-case title" in lowered or "title field" in lowered:
        return "title_generation"
    return "user_request"


def _flatten_content_block(item: dict[str, Any]) -> str | None:
    block_type = item.get("type")
    if block_type == "text":
        text = item.get("text")
        return text if isinstance(text, str) else None
    return None


async def _aiter_upstream_bytes(response: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    finally:
        await response.aclose()


async def _aiter_message_sse(payload: dict[str, Any]) -> AsyncIterator[bytes]:
    message = _sse_message_envelope(payload)
    yield _encode_sse_event("message_start", {"type": "message_start", "message": message})
    for index, block in enumerate(payload.get("content", [])):
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            yield _encode_sse_event(
                "content_block_start",
                {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
            )
            yield _encode_sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": block.get("text", "")},
                },
            )
        else:
            yield _encode_sse_event(
                "content_block_start",
                {"type": "content_block_start", "index": index, "content_block": block},
            )
        yield _encode_sse_event("content_block_stop", {"type": "content_block_stop", "index": index})
    yield _encode_sse_event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": payload.get("stop_reason", "end_turn"),
                "stop_sequence": payload.get("stop_sequence"),
            },
            "usage": {"output_tokens": _usage_output_tokens(payload)},
        },
    )
    yield _encode_sse_event("message_stop", {"type": "message_stop"})


def _stream_message_response(payload: dict[str, Any]) -> StreamingResponse:
    return StreamingResponse(_aiter_message_sse(payload), status_code=200, media_type="text/event-stream")


def _successful_message_payload(upstream: UpstreamResult) -> dict[str, Any] | None:
    status_code, payload = _parse_upstream_json(upstream, normalize_error=True)
    if status_code == 200 and _is_anthropic_message(payload):
        return payload
    return None


def _sse_message_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    usage = payload.get("usage")
    output_tokens = 0
    input_tokens = 0
    if isinstance(usage, dict):
        if isinstance(usage.get("output_tokens"), int):
            output_tokens = usage["output_tokens"]
        if isinstance(usage.get("input_tokens"), int):
            input_tokens = usage["input_tokens"]
    return {
        "id": payload.get("id", "msg_phase2b"),
        "type": "message",
        "role": payload.get("role", "assistant"),
        "model": payload.get("model", "phase2b-proxy"),
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def _usage_output_tokens(payload: dict[str, Any]) -> int:
    usage = payload.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("output_tokens"), int):
        return usage["output_tokens"]
    return 0


def _encode_sse_event(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")


def _stream_media_type(headers: dict[str, str]) -> str:
    return headers.get("content-type", "text/event-stream")


def _parse_upstream_json(upstream: UpstreamResult, normalize_error: bool = False) -> tuple[int, dict[str, Any]]:
    try:
        parsed = json.loads(upstream.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        if 200 <= upstream.status_code < 300:
            return 502, anthropic_error(502, "upstream returned an invalid success payload")
        message = upstream.body.decode("utf-8", errors="replace")
        return upstream.status_code, anthropic_error(upstream.status_code, message)
    if isinstance(parsed, dict):
        if 200 <= upstream.status_code < 300 and not _is_anthropic_message(parsed):
            return 502, anthropic_error(502, "upstream returned an invalid success payload")
        if normalize_error and not 200 <= upstream.status_code < 300 and not _is_anthropic_error(parsed):
            message = parsed.get("message")
            return upstream.status_code, anthropic_error(
                upstream.status_code,
                message if isinstance(message, str) else "upstream returned an invalid error payload",
            )
        return upstream.status_code, parsed
    if 200 <= upstream.status_code < 300:
        return 502, anthropic_error(502, "upstream returned an invalid success payload")
    return upstream.status_code, anthropic_error(upstream.status_code, "upstream returned a non-object JSON payload")


def _is_anthropic_message(payload: dict[str, Any]) -> bool:
    content = payload.get("content")
    return (
        payload.get("type") == "message"
        and isinstance(content, list)
        and all(_is_anthropic_content_block(item) for item in content)
    )


def _is_anthropic_error(payload: dict[str, Any]) -> bool:
    error = payload.get("error")
    return (
        payload.get("type") == "error"
        and isinstance(error, dict)
        and isinstance(error.get("type"), str)
        and isinstance(error.get("message"), str)
    )


def _is_anthropic_content_block(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    block_type = item.get("type")
    if not isinstance(block_type, str):
        return False
    if block_type == "text":
        return isinstance(item.get("text"), str)
    if block_type == "tool_use":
        return (
            isinstance(item.get("id"), str)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("input"), dict)
        )
    if block_type == "thinking":
        return isinstance(item.get("thinking"), str) and isinstance(item.get("signature"), str)
    if block_type == "redacted_thinking":
        return isinstance(item.get("data"), str)
    return True


def _load_default_config() -> ProxyConfig:
    config_path = _default_config_path()
    config = load_config(config_path)
    validate_runtime_config(config)
    return config


def _default_config_path() -> Path:
    configured = os.environ.get("CC_PROXY_CONFIG")
    if configured:
        return Path(configured).expanduser().resolve()
    project_local = Path(".claude/cc-proxy.toml")
    if project_local.exists():
        return project_local.resolve()
    return Path("proxy/config.toml.example").resolve()


def _validate_request_payload(payload: dict[str, Any]) -> str | None:
    max_tokens = payload.get("max_tokens")
    if max_tokens is not None and (not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0):
        return "invalid request payload"
    messages = payload.get("messages")
    if messages is not None and not isinstance(messages, list):
        return "invalid request payload"
    return None
