import json

import pytest

from proxy.candidate import (
    build_candidate_request,
    canonicalize_answer,
    parse_adjudication_response,
    parse_candidate_response,
)


def _base_request() -> dict[str, object]:
    return {
        "system": "Base system",
        "stream": True,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "logic"}],
    }


def _flatten_system_text(system: object) -> str:
    if isinstance(system, list):
        return "\n".join(str(block.get("text", "")) for block in system if isinstance(block, dict))
    return str(system)


def _candidate_payload(overrides: dict[str, object] | None = None) -> dict[str, object]:
    candidate = {
        "final_answer": "21",
        "answer_type": "number",
        "constraint_summary": ["shape is distinguishable by touch"],
        "critical_steps": ["check distinguishable shape buckets"],
        "counterexample_checked": True,
        "confidence": "medium",
    }
    if overrides:
        candidate.update(overrides)
    return {"content": [{"type": "text", "text": json.dumps(candidate)}]}


def test_build_candidate_request_embeds_required_json_schema() -> None:
    request = build_candidate_request(_base_request(), "constraint_reasoner", 256)
    system_text = _flatten_system_text(request["system"])
    assert '"final_answer"' in system_text
    assert '"constraint_summary"' in system_text
    assert '"critical_steps"' in system_text
    assert '"counterexample_checked"' in system_text
    assert '"confidence"' in system_text


def test_parse_candidate_response_rejects_string_constraint_summary() -> None:
    payload = _candidate_payload(
        {
            "constraint_summary": "not-a-list",
        }
    )
    with pytest.raises(ValueError, match="constraint_summary must be a list"):
        parse_candidate_response("constraint_reasoner", payload)


def test_parse_candidate_response_rejects_null_final_answer() -> None:
    payload = _candidate_payload({"final_answer": None})

    with pytest.raises(ValueError, match="final_answer must be a string"):
        parse_candidate_response("constraint_reasoner", payload)


@pytest.mark.parametrize("answer_type", ["string", "text"])
@pytest.mark.parametrize("final_answer", ["", " \t\n "])
def test_parse_candidate_response_rejects_empty_text_answer(answer_type: str, final_answer: str) -> None:
    payload = _candidate_payload({"final_answer": final_answer, "answer_type": answer_type})

    with pytest.raises(ValueError, match="final_answer must not be empty"):
        parse_candidate_response("constraint_reasoner", payload)


def test_parse_candidate_response_rejects_unknown_confidence_value() -> None:
    payload = _candidate_payload({"confidence": "certain"})

    with pytest.raises(ValueError, match="confidence must be one of"):
        parse_candidate_response("constraint_reasoner", payload)


def test_canonicalize_numeric_answer_treats_equivalent_renderings_as_equal() -> None:
    canonical_a, non_canonical_a = canonicalize_answer(" 21 ", "number")
    canonical_b, non_canonical_b = canonicalize_answer("21.0", "number")
    canonical_zero, non_canonical_zero = canonicalize_answer("-0", "number")
    assert canonical_a == "21"
    assert canonical_b == "21"
    assert canonical_zero == "0"
    assert non_canonical_a is False
    assert non_canonical_b is False
    assert non_canonical_zero is False


@pytest.mark.parametrize("raw", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_canonicalize_numeric_answer_rejects_non_finite_values(raw: str) -> None:
    canonical, non_canonical = canonicalize_answer(raw, "number")

    assert canonical is None
    assert non_canonical is True


def test_canonicalize_numeric_answer_preserves_large_exponent_integral_values() -> None:
    canonical, non_canonical = canonicalize_answer("1e999999", "number")

    assert canonical is not None
    assert canonical.startswith("1")
    assert len(canonical) == 1_000_000
    assert non_canonical is False


@pytest.mark.parametrize("raw", ["1e1000000", "1e-1000001"])
def test_canonicalize_numeric_answer_rejects_oversized_expansion(raw: str) -> None:
    canonical, non_canonical = canonicalize_answer(raw, "number")

    assert canonical is None
    assert non_canonical is True


def test_canonicalize_numeric_answer_preserves_large_finite_integer_exactly() -> None:
    canonical, non_canonical = canonicalize_answer("123456789012345678901234567890", "number")

    assert canonical == "123456789012345678901234567890"
    assert non_canonical is False


def test_canonicalize_numeric_answer_preserves_large_finite_fraction_exactly() -> None:
    canonical, non_canonical = canonicalize_answer("123456789012345678901234567890.1", "number")

    assert canonical == "123456789012345678901234567890.1"
    assert non_canonical is False


def test_parse_candidate_response_marks_unparseable_numeric_answer_non_canonical() -> None:
    payload = {
        "content": [
            {
                "type": "text",
                "text": '{"final_answer":"twenty one","answer_type":"number","constraint_summary":["shape is distinguishable by touch"],"critical_steps":["check distinguishable shape buckets"],"counterexample_checked":true,"confidence":"medium"}',
            }
        ]
    }

    candidate = parse_candidate_response("constraint_reasoner", payload)

    assert candidate.canonical_final_answer is None
    assert candidate.non_canonical is True


def test_parse_candidate_response_requires_json_message_content() -> None:
    with pytest.raises(ValueError, match="candidate response must contain JSON text"):
        parse_candidate_response("constraint_reasoner", {"content": [{"type": "text", "text": "not json"}]})


def test_parse_candidate_response_requires_outer_message_type() -> None:
    with pytest.raises(ValueError, match="candidate response must contain JSON text"):
        parse_candidate_response(
            "constraint_reasoner",
            {"type": "error", "content": [{"type": "text", "text": '{"final_answer":"21","answer_type":"number","constraint_summary":["shape constraint"],"critical_steps":["check bucket counts"],"counterexample_checked":true,"confidence":"medium"}'}]},
        )


@pytest.mark.parametrize("field_name", ["constraint_summary", "critical_steps"])
def test_parse_candidate_response_rejects_empty_reasoning_artifacts(field_name: str) -> None:
    payload = _candidate_payload({field_name: []})

    with pytest.raises(ValueError, match=field_name):
        parse_candidate_response("constraint_reasoner", payload)


def test_parse_candidate_response_rejects_placeholder_reasoning_artifacts() -> None:
    payload = _candidate_payload({"constraint_summary": ["shape"], "critical_steps": ["step"]})

    with pytest.raises(ValueError, match="task-relevant"):
        parse_candidate_response("constraint_reasoner", payload)


def test_parse_candidate_response_rejects_irrelevant_detailed_reasoning_artifacts_for_task_surface() -> None:
    payload = _candidate_payload(
        {
            "constraint_summary": ["the weather forecast mentions heavy rain tomorrow"],
            "critical_steps": ["compare airport delays across three terminals"],
        }
    )
    task_surface = "Find the minimum number of candies when shape can be distinguished by touch."

    with pytest.raises(ValueError, match="stay relevant to the task"):
        parse_candidate_response("constraint_reasoner", payload, task_surface=task_surface)


def test_parse_candidate_response_rejects_broad_marker_false_positive_for_task_surface() -> None:
    payload = _candidate_payload(
        {
            "constraint_summary": ["airport shape regulations change every season"],
            "critical_steps": ["minimum hotel stay guarantee applies downtown"],
        }
    )
    task_surface = "Find the minimum number of candies when shape can be distinguished by touch."

    with pytest.raises(ValueError, match="stay relevant to the task"):
        parse_candidate_response("constraint_reasoner", payload, task_surface=task_surface)


def test_parse_candidate_response_requires_boolean_counterexample_flag() -> None:
    payload = {
        "content": [
            {
                "type": "text",
                "text": '{"final_answer":"21","answer_type":"number","constraint_summary":["c1"],"critical_steps":["s1"],"counterexample_checked":"false","confidence":"medium"}',
            }
        ]
    }

    with pytest.raises(ValueError, match="counterexample_checked must be a boolean"):
        parse_candidate_response("constraint_reasoner", payload)


def test_parse_candidate_response_requires_counterexample_check_to_be_true() -> None:
    payload = {
        "content": [
            {
                "type": "text",
                "text": '{"final_answer":"21","answer_type":"number","constraint_summary":["shape constraint"],"critical_steps":["check bucket counts"],"counterexample_checked":false,"confidence":"medium"}',
            }
        ]
    }

    with pytest.raises(ValueError, match="counterexample_checked must be true"):
        parse_candidate_response("constraint_reasoner", payload)


def test_build_candidate_request_forces_non_stream_and_appends_role_prompt() -> None:
    body = {
        "system": "Base system",
        "stream": True,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "logic"}],
    }

    request = build_candidate_request(body, "constraint_reasoner", 512)

    assert request["stream"] is False
    assert request["max_tokens"] == 512
    assert "Base system" in request["system"]
    assert "strict JSON only" in request["system"]


def test_build_candidate_request_drops_invalid_thinking_budget_when_max_tokens_is_lower() -> None:
    request = build_candidate_request(
        {
            "model": "m",
            "max_tokens": 4096,
            "thinking": {"type": "enabled", "budget_tokens": 2048},
            "messages": [{"role": "user", "content": "Only answer 21"}],
        },
        "constraint_reasoner",
        512,
    )

    assert request["max_tokens"] == 512
    assert "thinking" not in request


def test_build_candidate_request_drops_boolean_thinking_budget() -> None:
    request = build_candidate_request(
        {
            "model": "m",
            "max_tokens": 4096,
            "thinking": {"type": "enabled", "budget_tokens": True},
            "messages": [{"role": "user", "content": "Only answer 21"}],
        },
        "constraint_reasoner",
        512,
    )

    assert request["max_tokens"] == 512
    assert "thinking" not in request


def test_build_candidate_request_drops_non_positive_or_non_object_thinking_budget() -> None:
    for thinking in (
        None,
        True,
        {"type": "enabled", "budget_tokens": 0},
        {"type": "enabled", "budget_tokens": -1},
    ):
        request = build_candidate_request(
            {
                "model": "m",
                "max_tokens": 4096,
                "thinking": thinking,
                "messages": [{"role": "user", "content": "Only answer 21"}],
            },
            "constraint_reasoner",
            512,
        )

        assert request["max_tokens"] == 512
        assert "thinking" not in request


def test_build_candidate_request_preserves_system_content_blocks() -> None:
    body = {
        "system": [{"type": "text", "text": "Base system"}],
        "stream": False,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "logic"}],
    }

    request = build_candidate_request(body, "counterexample_reasoner", 512)

    assert isinstance(request["system"], list)
    assert request["system"][0] == {"type": "text", "text": "Base system"}
    assert request["system"][-1]["type"] == "text"
    assert "strict JSON only" in request["system"][-1]["text"]


def test_parse_adjudication_response_returns_tuple() -> None:
    payload = {
        "content": [
            {
                "type": "text",
                "text": '{"final_answer":"21","answer_type":"number","decision_summary":"compatible_majority"}',
            }
        ]
    }

    final_answer, answer_type, decision_summary = parse_adjudication_response(payload)

    assert final_answer == "21"
    assert answer_type == "number"
    assert decision_summary == "compatible_majority"


@pytest.mark.parametrize("field_name", ["final_answer", "answer_type", "decision_summary"])
def test_parse_adjudication_response_rejects_non_string_fields(field_name: str) -> None:
    adjudication: dict[str, object] = {
        "final_answer": "21",
        "answer_type": "number",
        "decision_summary": "compatible_majority",
    }
    adjudication[field_name] = 21
    payload = {"content": [{"type": "text", "text": json.dumps(adjudication)}]}

    with pytest.raises(ValueError, match=rf"{field_name} must be a string"):
        parse_adjudication_response(payload)
