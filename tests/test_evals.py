from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest


def test_extract_final_answer_keeps_exact_numeric_output() -> None:
    from proxy.evals import extract_final_answer

    assert extract_final_answer("21") == "21"
    assert extract_final_answer("答案是 21。") == "21"


def test_score_run_distinguishes_exact_output_from_general_correctness() -> None:
    from proxy.evals import score_run

    exact = score_run(expected_answer="21", response_text="21", exact_output_required=True)
    verbose = score_run(expected_answer="21", response_text="答案是 21。", exact_output_required=True)

    assert exact["correct"] is True
    assert exact["format_ok"] is True
    assert verbose["correct"] is True
    assert verbose["format_ok"] is False


def test_regression_summary_flags_large_correctness_drop() -> None:
    from proxy.evals import summarize_regressions

    baseline = [
        {"correct": True, "format_ok": True, "protocol_ok": True},
        {"correct": True, "format_ok": True, "protocol_ok": True},
    ]
    candidate = [
        {"correct": False, "format_ok": True, "protocol_ok": True},
        {"correct": True, "format_ok": True, "protocol_ok": True},
    ]

    summary = summarize_regressions(baseline, candidate)

    assert summary["material_regression"] is True


def test_run_mode_loads_candy_fixture_and_builds_direct_request(config) -> None:
    from proxy.evals import run_mode

    result = run_mode("direct", Path("proxy/fixtures/candy_question.json"), config, execute=False)

    assert result["mode"] == "direct"
    assert result["fixture"]["expected_answer"] == "21"
    assert result["target_base_url"] == config.upstream.base_url
    assert result["request_body"]["messages"][0]["content"]


def test_run_mode_uses_anthropic_model_env_when_present(config, monkeypatch: pytest.MonkeyPatch) -> None:
    from proxy.evals import run_mode

    monkeypatch.setenv("ANTHROPIC_MODEL", "astron-code-latest")
    result = run_mode("direct", Path("proxy/fixtures/candy_question.json"), config, execute=False)

    assert result["request_body"]["model"] == "astron-code-latest"


def test_run_mode_rejects_unknown_mode(config) -> None:
    from proxy.evals import run_mode

    with pytest.raises(ValueError, match="unsupported mode"):
        run_mode("unknown-mode", Path("proxy/fixtures/candy_question.json"), config, execute=False)


def test_run_mode_executes_direct_request_and_scores_answer(config, monkeypatch: pytest.MonkeyPatch) -> None:
    from proxy.evals import run_mode

    seen_request: httpx.Request | None = None

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = request
        return httpx.Response(
            200,
            json={
                "type": "message",
                "content": [{"type": "text", "text": "21"}],
            },
        )

    monkeypatch.setenv(config.upstream.api_key_env, "secret-token")
    transport = httpx.MockTransport(handle)

    result = run_mode(
        "direct",
        Path("proxy/fixtures/candy_question.json"),
        config,
        client=httpx.Client(transport=transport),
    )

    assert result["status_code"] == 200
    assert result["response_text"] == "21"
    assert result["answer"] == "21"
    assert result["score"]["correct"] is True
    assert seen_request is not None
    assert seen_request.url == "https://example.com/anthropic/v1/messages"
    assert seen_request.headers["x-api-key"] == "secret-token"
    assert seen_request.headers["anthropic-version"] == "2023-06-01"


def test_run_mode_replays_captured_request_body_and_headers(
    config,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from proxy.evals import run_mode

    captured_request = tmp_path / "captured.json"
    captured_request.write_text(
        json.dumps(
            {
                "headers": {
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": "prompt-caching-2024-07-31",
                    "x-request-id": "req-captured-1",
                    "x-api-key": "stale-key",
                },
                "body": {
                    "model": "captured-model",
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "只输出 21"}],
                },
            },
        ),
        encoding="utf-8",
    )

    seen_request: httpx.Request | None = None

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = request
        return httpx.Response(
            200,
            json={
                "type": "message",
                "content": [{"type": "text", "text": "21"}],
            },
        )

    monkeypatch.setenv(config.upstream.api_key_env, "fresh-key")
    transport = httpx.MockTransport(handle)

    result = run_mode(
        "captured_claude_raw_upstream",
        Path("proxy/fixtures/candy_question.json"),
        config,
        client=httpx.Client(transport=transport),
        captured_request_path=captured_request,
    )

    assert result["status_code"] == 200
    assert result["request_body"]["model"] == "captured-model"
    assert result["request_body"]["messages"][0]["content"] == "只输出 21"
    assert seen_request is not None
    assert seen_request.headers["anthropic-beta"] == "prompt-caching-2024-07-31"
    assert seen_request.headers["x-request-id"] == "req-captured-1"
    assert seen_request.headers["x-api-key"] == "fresh-key"


def test_extract_response_text_reads_anthropic_message_content() -> None:
    from proxy.evals import extract_response_text

    text = extract_response_text(
        {
            "type": "message",
            "content": [
                {"type": "text", "text": "答案是"},
                {"type": "text", "text": " 21"},
            ],
        },
    )

    assert text == "答案是 21"
