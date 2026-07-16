from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from proxy.config import ProxyConfig

DIRECT_MODES = {"direct", "direct_anthropic", "captured_claude_raw_upstream"}
PROXY_MODES = {"captured_claude_passthrough", "captured_claude_rewrite", "claude_code_proxy"}
SUPPORTED_MODES = DIRECT_MODES | PROXY_MODES


def extract_final_answer(text: str) -> str | None:
    matches = re.findall(r"\b(\d+)\b", text)
    if not matches:
        return None
    return matches[-1]


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


def run_mode(mode: str, fixture_path: Path, config: ProxyConfig) -> dict[str, object]:
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"unsupported mode: {mode}")
    fixture = _load_fixture(fixture_path)
    return {
        "mode": mode,
        "fixture": fixture,
        "target_base_url": _target_base_url(mode, config),
        "request_body": {
            "model": "eval-model",
            "max_tokens": max(256, config.rewrite.max_tokens_floor.minimum_output_tokens),
            "stream": False,
            "messages": [{"role": "user", "content": fixture["prompt"]}],
        },
        "exact_output_required": bool(fixture.get("exact_output_required", False)),
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
