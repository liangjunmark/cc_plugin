from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
from typing import Any

import httpx

from proxy.config import ProxyConfig, load_config, validate_runtime_config

DIRECT_MODES = {"direct", "direct_anthropic", "captured_claude_raw_upstream"}
PROXY_MODES = {
    "captured_claude_passthrough",
    "captured_claude_rewrite",
    "claude_code_proxy",
    "claude_code_proxy_phase1",
    "claude_code_proxy_phase2",
    "claude_code_proxy_phase2b",
}
SUPPORTED_MODES = DIRECT_MODES | PROXY_MODES
PHASE_SELECTOR_BY_MODE = {
    "claude_code_proxy_phase1": "phase1",
    "claude_code_proxy_phase2": "phase2",
    "claude_code_proxy_phase2b": "phase2b",
}
PHASE2B_TIMEOUT_BUFFER_SECONDS = 10.0


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


def score_run(
    expected_answer: str,
    response_text: str,
    exact_output_required: bool,
    *,
    status_code: int,
    response_payload: dict[str, Any] | None,
) -> dict[str, bool]:
    extracted = extract_final_answer(response_text)
    stripped = response_text.strip()
    protocol_ok = 200 <= status_code < 300 and _is_anthropic_compatible_payload(status_code, response_payload)
    correct = protocol_ok and extracted == expected_answer
    format_ok = protocol_ok and (stripped == expected_answer if exact_output_required else bool(stripped))
    return {
        "correct": correct,
        "format_ok": format_ok,
        "protocol_ok": protocol_ok,
    }


def summarize_regressions(baseline: list[dict], candidate: list[dict]) -> dict[str, object]:
    baseline_correct = _rate(baseline, "correct")
    candidate_correct = _rate(candidate, "correct")
    baseline_format = _rate(baseline, "format_ok")
    candidate_format = _rate(candidate, "format_ok")
    baseline_protocol = _rate(baseline, "protocol_ok")
    candidate_protocol = _rate(candidate, "protocol_ok")
    correctness_drop = candidate_correct < baseline_correct - 0.10
    format_drop = candidate_format < baseline_format - 0.10
    protocol_drop = candidate_protocol < baseline_protocol - 0.10
    return {
        "baseline_correctness": baseline_correct,
        "candidate_correctness": candidate_correct,
        "baseline_format_ok": baseline_format,
        "candidate_format_ok": candidate_format,
        "baseline_protocol_ok": baseline_protocol,
        "candidate_protocol_ok": candidate_protocol,
        "material_regression": correctness_drop or format_drop or protocol_drop,
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
    active_client = client or httpx.Client(timeout=httpx.Timeout(_request_timeout_seconds(mode, config)))
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
            status_code=response.status_code,
            response_payload=response_payload,
        ),
    }


def run_repeated_mode(mode: str, fixture: Path, config: ProxyConfig, repeat_count: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for _ in range(repeat_count):
        results.append(run_mode(mode, fixture, config, execute=True))
    return results


def run_fixture_set(
    mode: str,
    fixture_set_path: Path,
    config: ProxyConfig,
    repeat_count: int,
) -> dict[str, Any]:
    fixture_set = _load_fixture_set(fixture_set_path)
    fixture_results: list[dict[str, Any]] = []
    for fixture_name in fixture_set["fixtures"]:
        fixture_path = fixture_set_path.parent / fixture_name
        repeated = run_repeated_mode(mode, fixture_path, config, repeat_count)
        fixture = _load_fixture(fixture_path)
        fixture_results.append(
            {
                "fixture_path": str(fixture_path),
                "fixture": fixture,
                "aggregate": _summarize_repeated_results(repeated, str(fixture["expected_answer"])),
                "results": repeated,
            }
        )
    return {
        "suite": fixture_set,
        "aggregate": _summarize_fixture_set_results(fixture_results),
        "results": fixture_results,
    }


def _summarize_repeated_results(results: list[dict[str, Any]], expected_answer: str) -> dict[str, int]:
    return {
        "runs": len(results),
        "exact_match": sum(item.get("response_text", "").strip() == expected_answer for item in results),
        "protocol_ok": sum(bool(item.get("score", {}).get("protocol_ok")) for item in results),
    }


def _summarize_fixture_set_results(results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "fixtures": len(results),
        "total_runs": sum(int(item["aggregate"]["runs"]) for item in results),
        "exact_match": sum(int(item["aggregate"]["exact_match"]) for item in results),
        "protocol_ok": sum(int(item["aggregate"]["protocol_ok"]) for item in results),
        "full_exact_match_fixtures": sum(
            int(item["aggregate"]["exact_match"]) == int(item["aggregate"]["runs"])
            for item in results
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run CC Proxy evaluation fixtures.")
    parser.add_argument("--mode", required=True, choices=sorted(SUPPORTED_MODES))
    fixture_group = parser.add_mutually_exclusive_group(required=True)
    fixture_group.add_argument("--fixture", type=Path)
    fixture_group.add_argument("--fixture-set", type=Path)
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")

    config_path = Path(os.environ.get("CC_PROXY_CONFIG", "proxy/config.toml.example"))
    config = load_config(config_path)
    validate_runtime_config(config)
    if args.fixture_set is not None:
        print(json.dumps(run_fixture_set(args.mode, args.fixture_set, config, args.repeat)))
        return
    results = run_repeated_mode(args.mode, args.fixture, config, args.repeat)
    fixture = _load_fixture(args.fixture)
    print(json.dumps({"aggregate": _summarize_repeated_results(results, str(fixture["expected_answer"])), "results": results}))


def _load_fixture(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("fixture must be a JSON object")
    return payload


def _load_fixture_set(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("fixture set must be a JSON object")
    name = payload.get("name")
    fixtures = payload.get("fixtures")
    if not isinstance(name, str) or not isinstance(fixtures, list) or not all(isinstance(item, str) for item in fixtures):
        raise ValueError("fixture set must contain string 'name' and string-list 'fixtures'")
    return {"name": name, "fixtures": fixtures}


def _rate(items: list[dict], key: str) -> float:
    if not items:
        return 0.0
    return sum(bool(item.get(key)) for item in items) / len(items)


def _target_base_url(mode: str, config: ProxyConfig) -> str:
    if mode in DIRECT_MODES:
        return config.upstream.base_url
    return f"http://{config.server.host}:{config.server.port}"


def _request_timeout_seconds(mode: str, config: ProxyConfig) -> float:
    if mode == "claude_code_proxy_phase2b":
        return max(
            config.upstream.timeout_seconds,
            config.phase2b.total_timeout_seconds + PHASE2B_TIMEOUT_BUFFER_SECONDS,
        )
    return config.upstream.timeout_seconds


def _build_request_body(
    fixture: dict[str, Any],
    config: ProxyConfig,
    captured_request_path: Path | None,
) -> dict[str, Any]:
    if captured_request_path is not None:
        captured = _load_captured_request(captured_request_path)
        return dict(captured["body"])
    prompt = str(fixture["prompt"])
    if bool(fixture.get("exact_output_required", False)):
        prompt = _ensure_exact_output_prompt(prompt)
    return {
        "model": os.environ.get("ANTHROPIC_MODEL", "eval-model"),
        "max_tokens": max(256, config.rewrite.max_tokens_floor.minimum_output_tokens),
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
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
    phase_selector = PHASE_SELECTOR_BY_MODE.get(mode)
    if phase_selector is not None:
        headers["x-cc-proxy-phase"] = phase_selector
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


def _is_anthropic_compatible_payload(status_code: int, payload: dict[str, Any] | None) -> bool:
    if payload is None:
        return False
    if 200 <= status_code < 300:
        return payload.get("type") == "message" and isinstance(payload.get("content"), list) and all(
            _is_anthropic_content_block(item) for item in payload["content"]
        )
    error = payload.get("error")
    return (
        payload.get("type") == "error"
        and isinstance(error, dict)
        and isinstance(error.get("type"), str)
        and isinstance(error.get("message"), str)
    )


def _extract_response_text_from_payload(payload: dict[str, Any] | None, response: httpx.Response) -> str:
    if payload is not None:
        text = extract_response_text(payload)
        if text:
            return text
        if _is_anthropic_compatible_payload(response.status_code, payload):
            return ""
    return response.text.strip()


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


def _ensure_exact_output_prompt(prompt: str) -> str:
    lowered = prompt.lower()
    if "只输出" in prompt or "only output" in lowered:
        return prompt
    return f"{prompt}\n只输出最终答案。"


if __name__ == "__main__":
    main()
