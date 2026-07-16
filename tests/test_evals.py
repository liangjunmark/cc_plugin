from __future__ import annotations

from pathlib import Path

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

    result = run_mode("direct", Path("proxy/fixtures/candy_question.json"), config)

    assert result["mode"] == "direct"
    assert result["fixture"]["expected_answer"] == "21"
    assert result["target_base_url"] == config.upstream.base_url
    assert result["request_body"]["messages"][0]["content"]


def test_run_mode_rejects_unknown_mode(config) -> None:
    from proxy.evals import run_mode

    with pytest.raises(ValueError, match="unsupported mode"):
        run_mode("unknown-mode", Path("proxy/fixtures/candy_question.json"), config)
