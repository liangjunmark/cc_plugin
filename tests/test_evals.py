from __future__ import annotations

import json
from pathlib import Path
import sys

import httpx
import pytest


def test_extract_final_answer_keeps_exact_numeric_output() -> None:
    from proxy.evals import extract_final_answer

    assert extract_final_answer("21") == "21"
    assert extract_final_answer("答案是 21。") == "21"


def test_score_run_distinguishes_exact_output_from_general_correctness() -> None:
    from proxy.evals import score_run

    response_payload = {"type": "message", "content": [{"type": "text", "text": "21"}]}
    exact = score_run(
        expected_answer="21",
        response_text="21",
        exact_output_required=True,
        status_code=200,
        response_payload=response_payload,
    )
    verbose = score_run(
        expected_answer="21",
        response_text="答案是 21。",
        exact_output_required=True,
        status_code=200,
        response_payload=response_payload,
    )

    assert exact["correct"] is True
    assert exact["format_ok"] is True
    assert verbose["correct"] is True
    assert verbose["format_ok"] is False


def test_score_run_keeps_reasoning_and_format_accounting_separate() -> None:
    from proxy.evals import score_run

    score = score_run(
        "21",
        "答案是 21",
        exact_output_required=True,
        status_code=200,
        response_payload={"type": "message", "content": [{"type": "text", "text": "答案是 21"}]},
    )

    assert score["correct"] is True
    assert score["format_ok"] is False


def test_score_run_marks_plaintext_503_as_protocol_failure() -> None:
    from proxy.evals import score_run

    score = score_run(
        expected_answer="21",
        response_text="upstream failure",
        exact_output_required=False,
        status_code=503,
        response_payload=None,
    )

    assert score["protocol_ok"] is False


def test_score_run_marks_protocol_invalid_exact_answer_as_incorrect() -> None:
    from proxy.evals import score_run

    score = score_run(
        expected_answer="21",
        response_text="21",
        exact_output_required=True,
        status_code=503,
        response_payload={"type": "error", "error": {"type": "api_error", "message": "21"}},
    )

    assert score == {"correct": False, "format_ok": False, "protocol_ok": False}


def test_score_run_marks_malformed_2xx_exact_answer_as_incorrect() -> None:
    from proxy.evals import score_run

    score = score_run(
        expected_answer="21",
        response_text="21",
        exact_output_required=True,
        status_code=200,
        response_payload={"type": "message", "content": [{"type": "text", "text": 21}]},
    )

    assert score == {"correct": False, "format_ok": False, "protocol_ok": False}


def test_extract_response_text_keeps_tool_use_only_2xx_from_falling_back_to_raw_json() -> None:
    from proxy.evals import _extract_response_text_from_payload

    payload = {
        "type": "message",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "math", "input": {"value": 21}}],
    }
    response = httpx.Response(200, json=payload)

    assert _extract_response_text_from_payload(payload, response) == ""


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


def test_regression_summary_flags_protocol_only_drop_as_material() -> None:
    from proxy.evals import summarize_regressions

    summary = summarize_regressions(
        [{"correct": True, "format_ok": True, "protocol_ok": True}],
        [{"correct": True, "format_ok": True, "protocol_ok": False}],
    )

    assert summary["material_regression"] is True


def test_run_mode_loads_candy_fixture_and_builds_direct_request(config) -> None:
    from proxy.evals import run_mode

    result = run_mode("direct", Path("proxy/fixtures/candy_question.json"), config, execute=False)

    assert result["mode"] == "direct"
    assert result["fixture"]["expected_answer"] == "21"
    assert result["target_base_url"] == config.upstream.base_url
    assert result["request_body"]["messages"][0]["content"]


def test_run_mode_supports_phase2_proxy_mode(config) -> None:
    from proxy.evals import run_mode

    result = run_mode("claude_code_proxy_phase2", Path("proxy/fixtures/candy_question.json"), config, execute=False)

    assert result["mode"] == "claude_code_proxy_phase2"


def test_run_mode_supports_phase2b_proxy_mode(config) -> None:
    from proxy.evals import run_mode

    result = run_mode("claude_code_proxy_phase2b", Path("proxy/fixtures/candy_question.json"), config, execute=False)

    assert result["request_headers"]["x-cc-proxy-phase"] == "phase2b"


def test_phase2b_proxy_mode_timeout_covers_phase2b_execution_budget(config) -> None:
    from proxy.evals import _request_timeout_seconds

    timeout_seconds = _request_timeout_seconds("claude_code_proxy_phase2b", config)

    assert timeout_seconds > config.phase2b.total_timeout_seconds
    assert timeout_seconds >= config.upstream.timeout_seconds


def test_run_repeated_mode_records_phase2b_repeated_results(config, monkeypatch) -> None:
    from proxy.evals import run_repeated_mode

    monkeypatch.setattr(
        "proxy.evals.run_mode",
        lambda mode, fixture, config, execute=True: {
            "mode": mode,
            "fixture": str(fixture),
            "response_text": "21",
            "score": {"exact_match": True, "protocol_ok": True},
        },
    )

    results = run_repeated_mode(
        "claude_code_proxy_phase2b",
        Path("proxy/fixtures/candy_question.json"),
        config,
        repeat_count=3,
    )

    assert len(results) == 3
    assert all(item["response_text"] == "21" for item in results)


def test_non_candy_reasoning_fixture_uses_correct_expected_answer() -> None:
    fixture = json.loads(Path("proxy/fixtures/non_candy_reasoning.json").read_text(encoding="utf-8"))

    assert fixture["expected_answer"] == "6"


def test_triple_color_double_guarantee_fixture_uses_correct_expected_answer() -> None:
    fixture = json.loads(Path("proxy/fixtures/triple_color_double_guarantee.json").read_text(encoding="utf-8"))

    assert fixture["expected_answer"] == "17"


def test_non_candy_exact_output_suite_manifest_lists_expected_fixtures() -> None:
    manifest = json.loads(Path("proxy/fixtures/non_candy_exact_output_suite.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "non_candy_exact_output_suite"
    assert manifest["fixtures"] == [
        "digit_swap_sum.json",
        "modulo_minus_one.json",
        "door_toggle_100.json",
        "triple_color_double_guarantee.json",
    ]


def test_run_mode_phase2_request_includes_exact_output_constraint(config) -> None:
    from proxy.evals import run_mode

    result = run_mode("claude_code_proxy_phase2", Path("proxy/fixtures/candy_question.json"), config, execute=False)

    prompt = result["request_body"]["messages"][0]["content"]
    assert "只输出" in prompt or "only output" in prompt.lower()


@pytest.mark.parametrize(
    ("mode", "expected_phase"),
    [
        ("claude_code_proxy_phase1", "phase1"),
        ("claude_code_proxy_phase2", "phase2"),
    ],
)
def test_run_mode_sets_proxy_phase_selector(config, mode: str, expected_phase: str) -> None:
    from proxy.evals import run_mode

    result = run_mode(mode, Path("proxy/fixtures/candy_question.json"), config, execute=False)

    assert result["request_headers"]["x-cc-proxy-phase"] == expected_phase


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


def test_run_mode_marks_non_anthropic_json_error_as_protocol_failure(config) -> None:
    from proxy.evals import run_mode

    transport = httpx.MockTransport(lambda request: httpx.Response(503, json={"message": "upstream failure"}))

    result = run_mode(
        "direct",
        Path("proxy/fixtures/candy_question.json"),
        config,
        client=httpx.Client(transport=transport),
    )

    assert result["status_code"] == 503
    assert result["score"]["protocol_ok"] is False


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


def test_run_fixture_set_aggregates_per_fixture_and_overall(config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from proxy.evals import run_fixture_set

    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    for name in ("a.json", "b.json"):
        (fixture_dir / name).write_text(
            json.dumps(
                {
                    "name": name.removesuffix(".json"),
                    "expected_answer": "1",
                    "exact_output_required": True,
                    "prompt": "只输出最终答案。",
                },
            ),
            encoding="utf-8",
        )
    fixture_set = tmp_path / "suite.json"
    fixture_set.write_text(
        json.dumps({"name": "tmp_suite", "fixtures": ["fixtures/a.json", "fixtures/b.json"]}),
        encoding="utf-8",
    )

    def fake_run_repeated_mode(mode, fixture, config, repeat_count):
        if fixture.name == "a.json":
            return [
                {"response_text": "1", "score": {"protocol_ok": True}},
                {"response_text": "0", "score": {"protocol_ok": True}},
            ]
        return [
            {"response_text": "1", "score": {"protocol_ok": True}},
            {"response_text": "1", "score": {"protocol_ok": False}},
        ]

    monkeypatch.setattr("proxy.evals.run_repeated_mode", fake_run_repeated_mode)

    result = run_fixture_set(
        "claude_code_proxy_phase2b",
        fixture_set,
        config,
        repeat_count=2,
    )

    assert result["suite"]["name"] == "tmp_suite"
    assert result["aggregate"] == {
        "fixtures": 2,
        "total_runs": 4,
        "exact_match": 3,
        "protocol_ok": 3,
        "full_exact_match_fixtures": 1,
    }
    assert result["results"][0]["aggregate"]["exact_match"] == 1
    assert result["results"][1]["aggregate"]["exact_match"] == 2


def test_main_supports_fixture_set_output(config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    from proxy import evals

    fixture_set = tmp_path / "suite.json"
    fixture_set.write_text(
        json.dumps({"name": "tmp_suite", "fixtures": ["a.json"]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CC_PROXY_CONFIG", str(Path("proxy/config.toml.example").resolve()))
    monkeypatch.setattr(evals, "load_config", lambda path: config)
    monkeypatch.setattr(evals, "validate_runtime_config", lambda loaded: None)
    monkeypatch.setattr(
        evals,
        "run_fixture_set",
        lambda mode, fixture_set_path, config, repeat_count: {
            "suite": {"name": "tmp_suite"},
            "aggregate": {"fixtures": 1, "total_runs": 1, "exact_match": 1, "protocol_ok": 1, "full_exact_match_fixtures": 1},
            "results": [],
        },
    )
    monkeypatch.setattr(sys, "argv", [
        "proxy.evals",
        "--mode",
        "claude_code_proxy_phase2b",
        "--fixture-set",
        str(fixture_set),
        "--repeat",
        "1",
    ])

    evals.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["suite"]["name"] == "tmp_suite"
    assert payload["aggregate"]["full_exact_match_fixtures"] == 1
