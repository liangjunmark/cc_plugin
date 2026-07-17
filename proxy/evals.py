from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any

import httpx

from proxy.config import ProxyConfig

DIRECT_MODES = {"direct", "direct_anthropic", "captured_claude_raw_upstream"}
PROXY_MODES = {"captured_claude_passthrough", "captured_claude_rewrite", "claude_code_proxy"}
SUPPORTED_MODES = DIRECT_MODES | PROXY_MODES


def extract_final_answer(text: str) -> str | None:
    matches = re.findall(r"\b(\d+)\b", text)
    if not matches:
        return None
    return matches[-1]


def extract_response_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts).strip()
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message", "")).strip()
    return ""


def score_run(expected_answer: str, response_text: str, exact_output_required: bool) -> dict[str, bool]:
    extracted = extract_final_answer(response_text)
    stripped = response_text.strip()
    correct = extracted == expected_answer
    format_ok = stripped == expected_answer if exact_output_required else bool(stripped)
    return {
        "correct": correct,
        "format_ok": format_ok,
        "protocol_ok": True,
    }


def summarize_regressions(baseline: list[dict], candidate: list[dict]) -> dict[str, object]:
    baseline_correct = _rate(baseline, "correct")
    candidate_correct = _rate(candidate, "correct")
    baseline_format = _rate(baseline, "format_ok")
    candidate_format = _rate(candidate, "format_ok")
    baseline_protocol = _rate(baseline, "protocol_ok")
    candidate_protocol = _rate(candidate, "protocol_ok")
    return {
        "baseline_correctness": baseline_correct,
        "candidate_correctness": candidate_correct,
        "baseline_format_ok": baseline_format,
        "candidate_format_ok": candidate_format,
        "baseline_protocol_ok": baseline_protocol,
        "candidate_protocol_ok": candidate_protocol,
        "material_regression": candidate_correct < baseline_correct - 0.10,
    }


def run_mode(
    mode: str,
    fixture_path: Path,
    config: ProxyConfig,
    *,
    execute: bool = True,
    client: httpx.Client | None = None,
    captured_request_path: Path | None = None,
) -> dict[str, object]:
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"unsupported mode: {mode}")
    fixture = _load_fixture(fixture_path)
    request_spec = {
        "mode": mode,
        "fixture": fixture,
        "target_base_url": _target_base_url(mode, config),
        "request_headers": _build_request_headers(mode, config, captured_request_path),
        "request_body": _build_request_body(fixture, config, captured_request_path),
        "exact_output_required": bool(fixture.get("exact_output_required", False)),
    }
    if not execute:
        return request_spec

    owns_client = client is None
    active_client = client or httpx.Client(timeout=httpx.Timeout(config.upstream.timeout_seconds))
    try:
        response = active_client.post(
            _messages_url(request_spec["target_base_url"]),
            headers=request_spec["request_headers"],
            json=request_spec["request_body"],
        )
    finally:
        if owns_client:
            active_client.close()

    response_payload = _decode_response_payload(response)
    response_text = _extract_response_text_from_payload(response_payload, response)
    return {
        **request_spec,
        "status_code": response.status_code,
        "response_json": response_payload,
        "response_text": response_text,
        "answer": extract_final_answer(response_text),
        "score": score_run(
            expected_answer=str(fixture["expected_answer"]),
            response_text=response_text,
            exact_output_required=bool(fixture.get("exact_output_required", False)),
        ),
    }


def _load_fixture(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("fixture must be a JSON object")
    return payload


def _rate(items: list[dict], key: str) -> float:
    if not items:
        return 0.0
    return sum(bool(item.get(key)) for item in items) / len(items)


def _target_base_url(mode: str, config: ProxyConfig) -> str:
    if mode in DIRECT_MODES:
        return config.upstream.base_url
    return f"http://{config.server.host}:{config.server.port}"


def _build_request_body(
    fixture: dict[str, Any],
    config: ProxyConfig,
    captured_request_path: Path | None,
) -> dict[str, Any]:
    if captured_request_path is not None:
        captured = _load_captured_request(captured_request_path)
        return dict(captured["body"])
    return {
        "model": os.environ.get("ANTHROPIC_MODEL", "eval-model"),
        "max_tokens": max(256, config.rewrite.max_tokens_floor.minimum_output_tokens),
        "stream": False,
        "messages": [{"role": "user", "content": fixture["prompt"]}],
    }


def _build_request_headers(
    mode: str,
    config: ProxyConfig,
    captured_request_path: Path | None,
) -> dict[str, str]:
    headers = {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    if captured_request_path is not None:
        captured = _load_captured_request(captured_request_path)
        for key, value in captured["headers"].items():
            if key.lower() in {"authorization", "x-api-key", "content-length", "host"}:
                continue
            headers[key] = value
    api_key = os.environ.get(config.upstream.api_key_env)
    if api_key:
        headers["x-api-key"] = api_key
    if mode == "direct_anthropic":
        headers.setdefault("anthropic-version", "2023-06-01")
    return headers


def _load_captured_request(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("captured request must be a JSON object")
    headers = payload.get("headers")
    body = payload.get("body")
    if not isinstance(headers, dict) or not isinstance(body, dict):
        raise ValueError("captured request must contain object 'headers' and 'body'")
    return {"headers": {str(key): str(value) for key, value in headers.items()}, "body": body}


def _messages_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/v1/messages"


def _decode_response_payload(response: httpx.Response) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _extract_response_text_from_payload(payload: dict[str, Any] | None, response: httpx.Response) -> str:
    if payload is not None:
        text = extract_response_text(payload)
        if text:
            return text
    return response.text.strip()
