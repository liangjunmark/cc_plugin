import asyncio
import json

import httpx
import pytest

from proxy.rewrite import ClassificationResult
from proxy.schemas import CandidateAnswer
from proxy.upstream import CallBudget, UpstreamResult, UpstreamTransport


def _candidate_answer(
    *,
    canonical_final_answer: str,
    constraint_summary: list[str],
) -> CandidateAnswer:
    return CandidateAnswer(
        "constraint_reasoner",
        canonical_final_answer,
        canonical_final_answer,
        "number",
        constraint_summary,
        ["check distinguishable shape buckets"],
        True,
        "high",
        False,
    )


def test_aggregate_candidates_runs_adjudication_when_summaries_share_generic_text_but_conflict_on_key_constraint() -> None:
    from proxy.phase2 import aggregate_candidates

    first = _candidate_answer(
        canonical_final_answer="21",
        constraint_summary=["must satisfy all constraints", "shape is distinguishable by touch"],
    )
    second = _candidate_answer(
        canonical_final_answer="21",
        constraint_summary=["must satisfy all constraints", "shape is not distinguishable by touch"],
    )
    decision = aggregate_candidates([first, second], budget_allows_adjudication=True)
    assert decision.decision == "run_adjudication"
    assert decision.reason == "summary_conflict"


def test_aggregate_candidates_runs_adjudication_when_cannot_negates_shared_constraint() -> None:
    from proxy.phase2 import aggregate_candidates

    first = _candidate_answer(
        canonical_final_answer="21",
        constraint_summary=["must satisfy all constraints", "shape can be distinguished by touch"],
    )
    second = _candidate_answer(
        canonical_final_answer="21",
        constraint_summary=["must satisfy all constraints", "shape cannot be distinguished by touch"],
    )

    decision = aggregate_candidates([first, second], budget_allows_adjudication=True)

    assert decision.decision == "run_adjudication"
    assert decision.reason == "summary_conflict"


def test_aggregate_candidates_runs_adjudication_when_distinguishable_and_cannot_be_distinguished_conflict() -> None:
    from proxy.phase2 import aggregate_candidates

    first = _candidate_answer(
        canonical_final_answer="21",
        constraint_summary=["must satisfy all constraints", "shape is distinguishable by touch"],
    )
    second = _candidate_answer(
        canonical_final_answer="21",
        constraint_summary=["must satisfy all constraints", "shape cannot be distinguished by touch"],
    )

    decision = aggregate_candidates([first, second], budget_allows_adjudication=True)

    assert decision.decision == "run_adjudication"
    assert decision.reason == "summary_conflict"


def test_is_phase2_eligible_rejects_streamed_requests(config) -> None:
    from proxy.phase2 import is_phase2_eligible
    from proxy.rewrite import ClassificationResult

    classification = ClassificationResult("rewrite", 4, True, None, "surface")
    body = {"stream": True, "messages": [{"role": "user", "content": "logic"}]}
    assert is_phase2_eligible(body, classification, config) is False


def test_is_phase2_eligible_rejects_tool_enabled_requests(config) -> None:
    from proxy.phase2 import is_phase2_eligible
    from proxy.rewrite import classify_request

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    body = {
        "model": "m",
        "max_tokens": 4096,
        "tools": [{"name": "shell", "input_schema": {"type": "object"}}],
        "messages": [{"role": "user", "content": "Only answer 21"}],
    }
    classification = classify_request(body, phase2_config)

    assert is_phase2_eligible(body, classification, phase2_config) is False


def test_is_phase2_eligible_rejects_requests_that_exceed_cost_budget_multiplier(config) -> None:
    from proxy.phase2 import is_phase2_eligible

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    phase2_config.phase2.cost_budget_multiplier = 2.0
    body = {
        "model": "test-model",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "logic"}],
    }
    classification = ClassificationResult("rewrite", 4, True, None, "surface")

    assert not is_phase2_eligible(body, classification, phase2_config)


def test_is_phase2_eligible_rejects_multi_turn_history(config) -> None:
    from proxy.phase2 import is_phase2_eligible

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    body = {
        "model": "test-model",
        "max_tokens": 4096,
        "messages": [
            {"role": "user", "content": "Find the minimum."},
            {"role": "user", "content": "Only output the answer."},
        ],
    }
    classification = ClassificationResult("rewrite", 4, True, None, "surface")

    assert not is_phase2_eligible(body, classification, phase2_config)


def test_is_phase2_eligible_rejects_assistant_history_before_latest_user(config) -> None:
    from proxy.phase2 import is_phase2_eligible

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    body = {
        "model": "test-model",
        "max_tokens": 4096,
        "messages": [
            {"role": "user", "content": "Find the minimum."},
            {"role": "assistant", "content": "21"},
            {"role": "user", "content": "Check the answer and only output it."},
        ],
    }
    classification = ClassificationResult("rewrite", 4, True, None, "surface")

    assert not is_phase2_eligible(body, classification, phase2_config)


def test_is_phase2_eligible_counts_baseline_in_total_cost_budget(config) -> None:
    from proxy.phase2 import is_phase2_eligible

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    phase2_config.phase2.cost_budget_multiplier = 2.0
    body = {
        "model": "test-model",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "Find the minimum and only output it."}],
    }
    classification = ClassificationResult("rewrite", 4, True, None, "surface")

    assert not is_phase2_eligible(body, classification, phase2_config)


def test_is_phase2_eligible_rejects_reasoning_request_without_exact_output_constraint(config) -> None:
    from proxy.phase2 import is_phase2_eligible

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    body = {
        "model": "test-model",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": "Find the minimum number of candies needed and explain why."}],
    }
    classification = ClassificationResult("rewrite", 4, True, None, "Find the minimum number of candies needed.")

    assert not is_phase2_eligible(body, classification, phase2_config)


def test_is_phase2_eligible_rejects_structured_exact_output_format_request(config) -> None:
    from proxy.phase2 import is_phase2_eligible

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    surface = 'Find the minimum and only output JSON like {"answer": 21}.'
    body = {
        "model": "test-model",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": surface}],
    }
    classification = ClassificationResult("rewrite", 4, True, None, surface)

    assert not is_phase2_eligible(body, classification, phase2_config)


def test_is_phase2_eligible_rejects_edit_or_patch_intent(config) -> None:
    from proxy.phase2 import is_phase2_eligible
    from proxy.rewrite import classify_request

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    phase2_config.classification.reasoning_keyword_patterns = ["minimum"]
    phase2_config.classification.output_constraint_patterns = ["only output"]
    body = {
        "model": "test-model",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": "Edit the file and only output the minimum patch."}],
    }
    classification = classify_request(body, phase2_config)

    assert not is_phase2_eligible(body, classification, phase2_config)


def test_is_phase2_eligible_rejects_negated_exact_output_constraint(config) -> None:
    from proxy.phase2 import is_phase2_eligible

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    surface = "Do not merely only output the final answer. Explain each step and the minimum case in detail."
    body = {
        "model": "test-model",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": surface}],
    }
    classification = ClassificationResult("rewrite", 4, True, None, surface)

    assert not is_phase2_eligible(body, classification, phase2_config)


def test_is_phase2_eligible_rejects_explanatory_system_constraint(config) -> None:
    from proxy.phase2 import is_phase2_eligible

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    surface = (
        "Explain your reasoning step by step before the final answer.\n"
        "Find the minimum and only output 21."
    )
    body = {
        "model": "test-model",
        "max_tokens": 4096,
        "system": "Explain your reasoning step by step before the final answer.",
        "messages": [{"role": "user", "content": "Find the minimum and only output 21."}],
    }
    classification = ClassificationResult("rewrite", 4, True, None, surface)

    assert not is_phase2_eligible(body, classification, phase2_config)


def test_is_phase2_eligible_rejects_coding_request_with_reasoning_markers(config) -> None:
    from proxy.phase2 import is_phase2_eligible
    from proxy.rewrite import classify_request

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    body = {
        "model": "test-model",
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Write a Python function that solves the minimum case and only output the final code.\n"
                    "Implement the algorithm, include the function signature, and keep the response as code only.\n"
                    "This request is intentionally long enough to satisfy the rewrite-length heuristic."
                ),
            }
        ],
    }
    classification = classify_request(body, phase2_config)

    assert not is_phase2_eligible(body, classification, phase2_config)


def test_is_phase2_eligible_rejects_rust_program_request_with_exact_output_markers(config) -> None:
    from proxy.phase2 import is_phase2_eligible
    from proxy.rewrite import classify_request

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    body = {
        "model": "test-model",
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Write a Rust program that computes the minimum answer and only output the final code.\n"
                    "Use ownership-safe data structures, include a main function, and keep the response code only.\n"
                    "This request is intentionally long enough to satisfy the rewrite-length heuristic."
                ),
            }
        ],
    }
    classification = classify_request(body, phase2_config)

    assert not is_phase2_eligible(body, classification, phase2_config)


def test_is_phase2_eligible_rejects_formatting_only_request_without_reasoning_signal(config) -> None:
    from proxy.phase2 import is_phase2_eligible

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    surface = (
        "Only output the final answer with no prose.\n"
        "Keep the response compact and strictly formatted.\n"
        "This prompt is long enough to satisfy the rewrite-length heuristic."
    )
    body = {
        "model": "test-model",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": surface}],
    }
    classification = ClassificationResult("rewrite", 4, True, None, surface)

    assert not is_phase2_eligible(body, classification, phase2_config)


def test_is_phase2_eligible_rejects_fix_tests_request(config) -> None:
    from proxy.phase2 import is_phase2_eligible
    from proxy.rewrite import classify_request

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    body = {
        "model": "test-model",
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Fix the failing tests and only output the final patch.\n"
                    "Keep the response as code only and preserve the minimum diff.\n"
                    "This request is intentionally long enough to satisfy the rewrite-length heuristic."
                ),
            }
        ],
    }
    classification = classify_request(body, phase2_config)

    assert not is_phase2_eligible(body, classification, phase2_config)


@pytest.mark.asyncio
async def test_run_phase2_falls_back_when_baseline_200_payload_is_not_anthropic_message(config) -> None:
    from proxy.phase2 import run_phase2

    class InvalidBaselineTransport:
        async def send_with_retry(self, **kwargs) -> UpstreamResult:
            return UpstreamResult(200, {"content-type": "application/json"}, b'{"message":"not anthropic"}')

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    result = await run_phase2(
        transport=InvalidBaselineTransport(),
        headers={},
        body={"model": "test-model", "max_tokens": 4096, "messages": [{"role": "user", "content": "minimum only output"}]},
        classification=ClassificationResult("rewrite", 4, True, None, "minimum only output"),
        config=phase2_config,
        request_id="req-invalid-baseline",
    )

    assert result.mode == "fallback_phase1"
    assert result.decision.reason == "baseline_invalid_success_payload"


def test_build_adjudication_request_drops_invalid_thinking_budget_when_max_tokens_is_lower() -> None:
    from proxy.phase2 import _build_adjudication_request

    request = _build_adjudication_request(
        {
            "model": "m",
            "max_tokens": 4096,
            "thinking": {"type": "enabled", "budget_tokens": 2048},
            "messages": [{"role": "user", "content": "Only answer 21"}],
        },
        [],
        512,
    )

    assert request["max_tokens"] == 512
    assert "thinking" not in request


def test_build_adjudication_request_drops_non_positive_or_non_object_thinking_budget() -> None:
    from proxy.phase2 import _build_adjudication_request

    for thinking in (
        None,
        True,
        {"type": "enabled", "budget_tokens": 0},
        {"type": "enabled", "budget_tokens": -1},
        {"type": "enabled", "budget_tokens": False},
    ):
        request = _build_adjudication_request(
            {
                "model": "m",
                "max_tokens": 4096,
                "thinking": thinking,
                "messages": [{"role": "user", "content": "Only answer 21"}],
            },
            [],
            512,
        )

        assert request["max_tokens"] == 512
        assert "thinking" not in request


def test_aggregate_candidates_prefers_matching_canonical_answers_with_compatible_summaries() -> None:
    from proxy.phase2 import aggregate_candidates
    from proxy.schemas import CandidateAnswer

    a = CandidateAnswer(
        "constraint_reasoner",
        "21",
        "21",
        "number",
        ["shape is distinguishable", "worst case uses apple/peach buckets"],
        ["check distinguishable shape buckets"],
        True,
        "high",
        False,
    )
    b = CandidateAnswer(
        "counterexample_reasoner",
        "21.0",
        "21",
        "number",
        ["shape is distinguishable", "worst case uses apple/peach buckets"],
        ["check distinguishable shape buckets"],
        True,
        "medium",
        False,
    )

    decision = aggregate_candidates([a, b], budget_allows_adjudication=True)
    assert decision.decision == "run_adjudication"
    assert decision.reason == "raw_format_conflict"


def test_aggregate_candidates_preserves_shared_exact_raw_rendering() -> None:
    from proxy.phase2 import aggregate_candidates
    from proxy.schemas import CandidateAnswer

    a = CandidateAnswer(
        "constraint_reasoner",
        "021",
        "21",
        "number",
        ["shape is distinguishable", "worst case uses apple/peach buckets"],
        ["check distinguishable shape buckets"],
        True,
        "high",
        False,
    )
    b = CandidateAnswer(
        "counterexample_reasoner",
        "021",
        "21",
        "number",
        ["shape is distinguishable", "worst case uses apple/peach buckets"],
        ["check distinguishable shape buckets"],
        True,
        "medium",
        False,
    )

    decision = aggregate_candidates([a, b], budget_allows_adjudication=True)

    assert decision.decision == "choose_candidate"
    assert decision.chosen_answer == "021"


def test_aggregate_candidates_preserves_shared_exact_raw_whitespace_rendering() -> None:
    from proxy.phase2 import aggregate_candidates
    from proxy.schemas import CandidateAnswer

    a = CandidateAnswer(
        "constraint_reasoner",
        "021\n",
        "21",
        "number",
        ["shape is distinguishable", "worst case uses apple/peach buckets"],
        ["check distinguishable shape buckets"],
        True,
        "high",
        False,
    )
    b = CandidateAnswer(
        "counterexample_reasoner",
        "021\n",
        "21",
        "number",
        ["shape is distinguishable", "worst case uses apple/peach buckets"],
        ["check distinguishable shape buckets"],
        True,
        "medium",
        False,
    )

    decision = aggregate_candidates([a, b], budget_allows_adjudication=True)

    assert decision.decision == "choose_candidate"
    assert decision.chosen_answer == "021\n"


def test_aggregate_candidates_falls_back_when_only_one_candidate_parses() -> None:
    from proxy.phase2 import aggregate_candidates
    from proxy.schemas import CandidateAnswer

    a = CandidateAnswer("constraint_reasoner", "21", "21", "number", ["shape"], ["step"], True, "high", False)
    decision = aggregate_candidates([a], budget_allows_adjudication=False)
    assert decision.decision == "fallback_baseline"


def test_aggregate_candidates_rejects_generic_shared_summary_without_shared_critical_steps() -> None:
    from proxy.phase2 import aggregate_candidates
    from proxy.schemas import CandidateAnswer

    a = CandidateAnswer(
        "constraint_reasoner",
        "21",
        "21",
        "number",
        ["must satisfy all constraints"],
        ["count apple circles"],
        True,
        "high",
        False,
    )
    b = CandidateAnswer(
        "counterexample_reasoner",
        "21",
        "21",
        "number",
        ["must satisfy all constraints"],
        ["count peach stars"],
        True,
        "medium",
        False,
    )

    decision = aggregate_candidates([a, b], budget_allows_adjudication=True)

    assert decision.decision == "run_adjudication"
    assert decision.reason == "summary_conflict"


def test_aggregate_candidates_prefers_largest_canonical_majority_group() -> None:
    from proxy.phase2 import aggregate_candidates
    from proxy.schemas import CandidateAnswer

    first = CandidateAnswer(
        "constraint_reasoner",
        "10",
        "10",
        "number",
        ["shape is distinguishable", "bucket plan one"],
        ["check bucket plan one"],
        True,
        "medium",
        False,
    )
    second = CandidateAnswer(
        "counterexample_reasoner",
        "10",
        "10",
        "number",
        ["shape is distinguishable", "bucket plan one"],
        ["check bucket plan one"],
        True,
        "medium",
        False,
    )
    third = CandidateAnswer(
        "constraint_reasoner",
        "20",
        "20",
        "number",
        ["shape is distinguishable", "bucket plan two"],
        ["check bucket plan two"],
        True,
        "high",
        False,
    )
    fourth = CandidateAnswer(
        "counterexample_reasoner",
        "20",
        "20",
        "number",
        ["shape is distinguishable", "bucket plan two"],
        ["check bucket plan two"],
        True,
        "high",
        False,
    )
    fifth = CandidateAnswer(
        "constraint_reasoner",
        "20",
        "20",
        "number",
        ["shape is distinguishable", "bucket plan two"],
        ["check bucket plan two"],
        True,
        "high",
        False,
    )

    decision = aggregate_candidates([first, second, third, fourth, fifth], budget_allows_adjudication=True)

    assert decision.decision == "choose_candidate"
    assert decision.chosen_answer == "20"


def test_aggregate_candidates_treats_different_answer_types_as_disagreement() -> None:
    from proxy.phase2 import aggregate_candidates
    from proxy.schemas import CandidateAnswer

    string_answer = CandidateAnswer(
        "constraint_reasoner",
        "21",
        "21",
        "string",
        ["shape is distinguishable by touch"],
        ["check distinguishable shape buckets"],
        True,
        "high",
        False,
    )
    numeric_answer = CandidateAnswer(
        "counterexample_reasoner",
        "21",
        "21",
        "number",
        ["shape is distinguishable by touch"],
        ["verify counterexample bucket counts"],
        True,
        "medium",
        False,
    )

    decision = aggregate_candidates([string_answer, numeric_answer], budget_allows_adjudication=True)

    assert decision.decision == "run_adjudication"
    assert decision.reason == "candidate_disagreement"


def test_aggregate_candidates_runs_adjudication_when_matching_answer_has_conflicting_summaries() -> None:
    from proxy.phase2 import aggregate_candidates
    from proxy.schemas import CandidateAnswer

    constraint_candidate = CandidateAnswer(
        "constraint_reasoner",
        "21",
        "21",
        "number",
        ["geometry bucket counts"],
        ["check distinguishable shape buckets"],
        True,
        "high",
        False,
    )
    counterexample_candidate = CandidateAnswer(
        "counterexample_reasoner",
        "21.0",
        "21",
        "number",
        ["counting apple and peach quotas"],
        ["check distinguishable shape buckets"],
        True,
        "high",
        False,
    )

    decision = aggregate_candidates([constraint_candidate, counterexample_candidate], budget_allows_adjudication=True)

    assert decision.decision == "run_adjudication"
    assert decision.reason == "summary_conflict"


def test_aggregate_candidates_helper_falls_back_when_noncanonical_majority_beats_canonical_minority_without_budget() -> None:
    """Exercise generic aggregation input; the runtime default has two candidates."""
    from proxy.phase2 import aggregate_candidates
    from proxy.schemas import CandidateAnswer

    unparseable_a = CandidateAnswer(
        "constraint_reasoner",
        "NaN",
        None,
        "number",
        ["shape is distinguishable by touch"],
        ["check distinguishable shape buckets"],
        True,
        "low",
        True,
    )
    unparseable_b = CandidateAnswer(
        "counterexample_reasoner",
        "Infinity",
        None,
        "number",
        ["shape is distinguishable by touch"],
        ["check distinguishable shape buckets"],
        True,
        "low",
        True,
    )
    canonical_candidate = CandidateAnswer(
        "canonical_reasoner",
        "21",
        "21",
        "number",
        ["shape is distinguishable by touch"],
        ["check distinguishable shape buckets"],
        True,
        "high",
        False,
    )

    decision = aggregate_candidates(
        [unparseable_a, unparseable_b, canonical_candidate], budget_allows_adjudication=False
    )

    assert decision.decision == "fallback_baseline"
    assert decision.reason == "non_canonical_majority"


@pytest.mark.asyncio
async def test_run_phase2_caps_retry_attempts_across_baseline_and_candidates(config) -> None:
    from proxy.phase2 import run_phase2

    upstream_calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        if upstream_calls == 1:
            return httpx.Response(200, json={"type": "message", "content": [{"type": "text", "text": "baseline"}]})
        return httpx.Response(503, json={"error": "retry"})

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await run_phase2(
            transport=UpstreamTransport(client, phase2_config),
            headers={},
            body={"model": "test", "max_tokens": 16, "messages": [{"role": "user", "content": "logic"}]},
            classification=ClassificationResult("rewrite", 4, True, None, "surface"),
            config=phase2_config,
            request_id="req-phase2-budget",
        )

    assert upstream_calls == 4
    assert result.mode == "fallback_phase1"


@pytest.mark.asyncio
async def test_run_phase2_falls_back_when_baseline_returns_redirect(config) -> None:
    from proxy.phase2 import run_phase2

    class RedirectBaselineTransport:
        def __init__(self) -> None:
            self.calls = 0

        async def send_with_retry(self, **kwargs) -> UpstreamResult:
            self.calls += 1
            return UpstreamResult(302, {}, b'{"type":"message","content":[{"type":"text","text":"baseline"}]}')

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    transport = RedirectBaselineTransport()
    result = await run_phase2(
        transport=transport,
        headers={},
        body={"model": "test", "max_tokens": 16, "messages": [{"role": "user", "content": "logic"}]},
        classification=ClassificationResult("rewrite", 4, True, None, "surface"),
        config=phase2_config,
        request_id="req-phase2-baseline-redirect",
    )

    assert transport.calls == 1
    assert result.mode == "fallback_phase1"
    assert result.decision.reason == "baseline_failed"


@pytest.mark.asyncio
async def test_run_phase2_falls_back_when_candidates_return_redirect(config) -> None:
    from proxy.phase2 import run_phase2

    class RedirectCandidateTransport:
        def __init__(self) -> None:
            self.calls = 0

        async def send_with_retry(self, **kwargs) -> UpstreamResult:
            self.calls += 1
            if self.calls == 1:
                return UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"text","text":"baseline"}]}')
            candidate = {
                "answer_type": "number",
                "final_answer": "21",
                "constraint_summary": ["shape is distinguishable by touch"],
                "critical_steps": ["check distinguishable shape buckets"],
                "counterexample_checked": True,
                "confidence": "high",
            }
            return UpstreamResult(
                302,
                {},
                json.dumps({"type": "message", "content": [{"type": "text", "text": json.dumps(candidate)}]}).encode(),
            )

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    transport = RedirectCandidateTransport()
    result = await run_phase2(
        transport=transport,
        headers={},
        body={"model": "test", "max_tokens": 16, "messages": [{"role": "user", "content": "logic"}]},
        classification=ClassificationResult("rewrite", 4, True, None, "surface"),
        config=phase2_config,
        request_id="req-phase2-candidate-redirect",
    )

    assert transport.calls == 3
    assert result.mode == "fallback_phase1"
    assert result.decision.reason == "insufficient_candidates"


@pytest.mark.asyncio
async def test_run_phase2_falls_back_when_adjudication_returns_redirect(config) -> None:
    from proxy.phase2 import run_phase2

    class RedirectAdjudicationTransport:
        def __init__(self) -> None:
            self.calls = 0

        async def send_with_retry(self, **kwargs) -> UpstreamResult:
            self.calls += 1
            if self.calls == 1:
                return UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"text","text":"baseline"}]}')
            if self.calls in {2, 3}:
                candidate = {
                    "answer_type": "number",
                    "final_answer": "21",
                    "constraint_summary": (
                        ["shape can be distinguished"] if self.calls == 2 else ["shape cannot be distinguished"]
                    ),
                    "critical_steps": ["check distinguishable shape buckets"],
                    "counterexample_checked": True,
                    "confidence": "high",
                }
                return UpstreamResult(
                    200,
                    {},
                    json.dumps({"type": "message", "content": [{"type": "text", "text": json.dumps(candidate)}]}).encode(),
                )
            adjudication = {
                "final_answer": "21",
                "answer_type": "number",
                "decision_summary": "compatible_majority",
            }
            return UpstreamResult(
                302,
                {},
                json.dumps({"type": "message", "content": [{"type": "text", "text": json.dumps(adjudication)}]}).encode(),
            )

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    phase2_config.phase2.max_adjudication_calls = 1
    transport = RedirectAdjudicationTransport()
    result = await run_phase2(
        transport=transport,
        headers={},
        body={"model": "test", "max_tokens": 16, "messages": [{"role": "user", "content": "logic"}]},
        classification=ClassificationResult("rewrite", 4, True, None, "surface"),
        config=phase2_config,
        request_id="req-phase2-adjudication-redirect",
    )

    assert transport.calls == 4
    assert result.mode == "fallback_phase1"
    assert result.decision.reason == "invalid_adjudication"


@pytest.mark.asyncio
async def test_run_phase2_applies_total_timeout_to_the_baseline(config) -> None:
    from proxy.phase2 import run_phase2

    class SlowBaselineTransport:
        def __init__(self) -> None:
            self.calls = 0

        async def send_with_retry(self, **kwargs) -> UpstreamResult:
            self.calls += 1
            await asyncio.sleep(0.05)
            return UpstreamResult(200, {}, b'{"type":"message"}')

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    phase2_config.phase2.total_timeout_seconds = 0.01
    transport = SlowBaselineTransport()

    result = await run_phase2(
        transport=transport,
        headers={},
        body={"model": "test", "max_tokens": 16, "messages": [{"role": "user", "content": "logic"}]},
        classification=ClassificationResult("rewrite", 4, True, None, "surface"),
        config=phase2_config,
        request_id="req-phase2-timeout",
    )

    assert transport.calls == 1
    assert result.mode == "fallback_phase1"
    assert result.decision.reason == "baseline_timeout"


@pytest.mark.asyncio
async def test_run_phase2_rejects_streamed_input_before_calling_transport(config) -> None:
    from proxy.phase2 import run_phase2

    class TrackingTransport:
        def __init__(self) -> None:
            self.calls = 0

        async def send_with_retry(self, **kwargs) -> UpstreamResult:
            self.calls += 1
            return UpstreamResult(200, {}, b'{}')

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    transport = TrackingTransport()

    with pytest.raises(ValueError, match="streamed"):
        await run_phase2(
            transport=transport,
            headers={},
            body={"stream": True, "messages": [{"role": "user", "content": "logic"}]},
            classification=ClassificationResult("rewrite", 4, True, None, "surface"),
            config=phase2_config,
            request_id="req-phase2-streamed",
        )

    assert transport.calls == 0


@pytest.mark.asyncio
async def test_run_phase2_falls_back_when_baseline_retry_exhausts_adjudication_budget(config) -> None:
    from proxy.phase2 import run_phase2

    upstream_calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        if upstream_calls == 1:
            return httpx.Response(503, json={"error": "retry"})
        if upstream_calls == 2:
            return httpx.Response(200, json={"type": "message", "content": [{"type": "text", "text": "baseline"}]})

        answer = "21" if upstream_calls == 3 else "34"
        candidate = {
            "answer_type": "number",
            "final_answer": answer,
            "constraint_summary": ["shape is distinguishable by touch"],
            "critical_steps": ["check distinguishable shape buckets"],
            "counterexample_checked": True,
            "confidence": "high",
        }
        return httpx.Response(
            200,
            json={"type": "message", "content": [{"type": "text", "text": json.dumps(candidate)}]},
        )

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await run_phase2(
            transport=UpstreamTransport(client, phase2_config),
            headers={},
            body={"model": "test", "max_tokens": 16, "messages": [{"role": "user", "content": "logic"}]},
            classification=ClassificationResult("rewrite", 4, True, None, "surface"),
            config=phase2_config,
            request_id="req-phase2-adjudication-budget",
        )

    assert upstream_calls == 4
    assert result.decision.decision == "fallback_baseline"
    assert result.decision.reason == "candidate_disagreement"


@pytest.mark.asyncio
async def test_phase2_exact_output_adjudication_emits_answer_only(config) -> None:
    from proxy.phase2 import run_phase2

    class CandidateTransport:
        def __init__(self) -> None:
            self.calls = 0
            self.adjudication_requests: list[dict[str, object]] = []

        async def send_with_retry(self, **kwargs) -> UpstreamResult:
            self.calls += 1
            if self.calls == 1:
                return UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"text","text":"baseline"}]}')
            system = str(kwargs["body"].get("system", ""))
            if "Adjudicate the candidate answers" in system:
                self.adjudication_requests.append(kwargs["body"])
                adjudication = {
                    "answer_type": "number",
                    "final_answer": "34",
                    "decision_summary": "The counterexample candidate satisfies the stated constraints.",
                }
                return UpstreamResult(
                    200,
                    {},
                    json.dumps({"type": "message", "content": [{"type": "text", "text": json.dumps(adjudication)}]}).encode(),
                )
            candidate = {
                "answer_type": "number",
                "final_answer": "21" if "List explicit constraints" in system else "34",
                "constraint_summary": ["shape is distinguishable by touch"],
                "critical_steps": ["check distinguishable shape buckets"],
                "counterexample_checked": True,
                "confidence": "high",
            }
            return UpstreamResult(
                200,
                {},
                json.dumps({"type": "message", "content": [{"type": "text", "text": json.dumps(candidate)}]}).encode(),
            )

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    transport = CandidateTransport()
    result = await run_phase2(
        transport=transport,
        headers={},
        body={"model": "test", "max_tokens": 16, "messages": [{"role": "user", "content": "logic"}]},
        classification=ClassificationResult("rewrite", 4, True, None, "surface"),
        config=phase2_config,
        request_id="req-phase2-exact-output",
    )

    assert transport.calls == 4
    assert len(transport.adjudication_requests) == 1
    adjudication_system = str(transport.adjudication_requests[0]["system"])
    assert '"final_answer": "21"' in adjudication_system
    assert '"final_answer": "34"' in adjudication_system
    assert result.mode == "phase2"
    assert result.downstream_payload == {
        "type": "message",
        "id": "phase2",
        "role": "assistant",
        "model": "test",
        "content": [{"type": "text", "text": "34"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


@pytest.mark.asyncio
async def test_phase2_selected_answer_payload_is_anthropic_compatible(config) -> None:
    from proxy.phase2 import run_phase2

    class CompatibleCandidateTransport:
        def __init__(self) -> None:
            self.calls = 0

        async def send_with_retry(self, **kwargs) -> UpstreamResult:
            self.calls += 1
            if self.calls == 1:
                return UpstreamResult(
                    200,
                    {},
                    b'{"type":"message","content":[{"type":"text","text":"baseline"}]}',
                )
            candidate = {
                "answer_type": "number",
                "final_answer": "21",
                "constraint_summary": ["shape is distinguishable by touch"],
                "critical_steps": ["check distinguishable shape buckets"],
                "counterexample_checked": True,
                "confidence": "high",
            }
            return UpstreamResult(
                200,
                {},
                json.dumps({"type": "message", "content": [{"type": "text", "text": json.dumps(candidate)}]}).encode(),
            )

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    result = await run_phase2(
        transport=CompatibleCandidateTransport(),
        headers={},
        body={"model": "test-model", "max_tokens": 16, "messages": [{"role": "user", "content": "logic"}]},
        classification=ClassificationResult("rewrite", 4, True, None, "surface"),
        config=phase2_config,
        request_id="req-phase2-anthropic-payload",
    )

    payload = result.downstream_payload
    assert payload is not None
    assert payload["type"] == "message"
    assert payload["role"] == "assistant"
    assert "model" in payload
    assert isinstance(payload["usage"]["input_tokens"], int)


@pytest.mark.asyncio
async def test_run_phase2_falls_back_when_adjudication_output_is_invalid(config) -> None:
    from proxy.phase2 import run_phase2

    class InvalidAdjudicationTransport:
        def __init__(self) -> None:
            self.calls = 0

        async def send_with_retry(self, **kwargs) -> UpstreamResult:
            self.calls += 1
            if self.calls == 1:
                return UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"text","text":"baseline"}]}')
            system = str(kwargs["body"].get("system", ""))
            if "Adjudicate the candidate answers" in system:
                return UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"text","text":"not json"}]}')
            answer = "21" if "List explicit constraints" in system else "34"
            candidate = {
                "answer_type": "number",
                "final_answer": answer,
                "constraint_summary": ["shape is distinguishable by touch"],
                "critical_steps": ["check distinguishable shape buckets"],
                "counterexample_checked": True,
                "confidence": "high",
            }
            return UpstreamResult(
                200,
                {},
                json.dumps({"type": "message", "content": [{"type": "text", "text": json.dumps(candidate)}]}).encode(),
            )

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    transport = InvalidAdjudicationTransport()
    result = await run_phase2(
        transport=transport,
        headers={},
        body={"model": "test", "max_tokens": 16, "messages": [{"role": "user", "content": "logic"}]},
        classification=ClassificationResult("rewrite", 4, True, None, "surface"),
        config=phase2_config,
        request_id="req-phase2-invalid-adjudication",
    )

    assert transport.calls == 4
    assert result.mode == "fallback_phase1"
    assert result.decision.decision == "fallback_baseline"
    assert result.decision.reason == "invalid_adjudication"


@pytest.mark.asyncio
async def test_run_phase2_falls_back_when_only_one_candidate_parses_from_mixed_candidate_responses(config) -> None:
    from proxy.phase2 import run_phase2

    class MixedCandidateTransport:
        def __init__(self) -> None:
            self.calls = 0
            self.baseline = UpstreamResult(
                200,
                {},
                b'{"type":"message","content":[{"type":"text","text":"baseline"}]}',
            )

        async def send_with_retry(self, **kwargs) -> UpstreamResult:
            self.calls += 1
            if self.calls == 1:
                return self.baseline
            system = str(kwargs["body"].get("system", ""))
            if "List explicit constraints" in system:
                candidate = {
                    "answer_type": "number",
                    "final_answer": "21",
                    "constraint_summary": ["shape is distinguishable by touch"],
                    "critical_steps": ["check distinguishable shape buckets"],
                    "counterexample_checked": True,
                    "confidence": "high",
                }
                return UpstreamResult(
                    200,
                    {},
                    json.dumps({"type": "message", "content": [{"type": "text", "text": json.dumps(candidate)}]}).encode(),
                )
            return UpstreamResult(
                200,
                {},
                b'{"type":"message","content":[{"type":"text","text":"not json"}]}',
            )

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    transport = MixedCandidateTransport()
    result = await run_phase2(
        transport=transport,
        headers={},
        body={"model": "test", "max_tokens": 16, "messages": [{"role": "user", "content": "logic"}]},
        classification=ClassificationResult("rewrite", 4, True, None, "surface"),
        config=phase2_config,
        request_id="req-phase2-mixed-parse-failure",
    )

    assert transport.calls == 3
    assert result.mode == "fallback_phase1"
    assert result.baseline_upstream is transport.baseline
    assert len(result.candidates) == 1
    assert result.candidates[0].canonical_final_answer == "21"
    assert result.decision.reason == "insufficient_candidates"


@pytest.mark.asyncio
async def test_run_phase2_falls_back_when_adjudication_transport_raises(config) -> None:
    from proxy.phase2 import run_phase2

    class AdjudicationFailureTransport:
        def __init__(self) -> None:
            self.calls = 0
            self.baseline = UpstreamResult(
                200,
                {},
                b'{"type":"message","content":[{"type":"text","text":"baseline"}]}',
            )

        async def send_with_retry(self, **kwargs) -> UpstreamResult:
            if not kwargs["call_budget"].consume():
                return UpstreamResult(599, {}, b"upstream call budget exhausted")
            self.calls += 1
            if self.calls == 1:
                return self.baseline
            system = str(kwargs["body"].get("system", ""))
            if "Adjudicate the candidate answers" in system:
                raise httpx.ConnectError("adjudication unavailable")
            candidate = {
                "answer_type": "number",
                "final_answer": "21" if "List explicit constraints" in system else "34",
                "constraint_summary": ["shape is distinguishable by touch"],
                "critical_steps": ["check distinguishable shape buckets"],
                "counterexample_checked": True,
                "confidence": "high",
            }
            return UpstreamResult(
                200,
                {},
                json.dumps({"type": "message", "content": [{"type": "text", "text": json.dumps(candidate)}]}).encode(),
            )

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    transport = AdjudicationFailureTransport()
    result = await run_phase2(
        transport=transport,
        headers={},
        body={"model": "test", "max_tokens": 16, "messages": [{"role": "user", "content": "logic"}]},
        classification=ClassificationResult("rewrite", 4, True, None, "surface"),
        config=phase2_config,
        request_id="req-phase2-adjudication-transport-error",
    )

    assert transport.calls == 4
    assert result.mode == "fallback_phase1"
    assert result.baseline_upstream is transport.baseline
    assert result.decision.decision == "fallback_baseline"
    assert result.downstream_payload is None


@pytest.mark.asyncio
async def test_run_adjudication_does_not_retry_retryable_response(config) -> None:
    from proxy.phase2 import _run_adjudication

    upstream_calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(503, json={"error": "retryable"})

    phase2_config = config.model_copy(deep=True)
    phase2_config.upstream.max_retries = 1
    phase2_config.upstream.retry_statuses = [503]
    candidates = [
        CandidateAnswer(
            "constraint_reasoner",
            "21",
            "21",
            "number",
            ["shape is distinguishable by touch"],
            ["check distinguishable shape buckets"],
            True,
            "high",
            False,
        ),
        CandidateAnswer(
            "counterexample_reasoner",
            "34",
            "34",
            "number",
            ["shape is distinguishable by touch"],
            ["check distinguishable shape buckets"],
            True,
            "high",
            False,
        ),
    ]
    call_budget = CallBudget(2)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await _run_adjudication(
            transport=UpstreamTransport(client, phase2_config),
            headers={},
            body={"model": "test", "max_tokens": 16, "messages": [{"role": "user", "content": "logic"}]},
            candidates=candidates,
            classification=ClassificationResult("rewrite", 4, True, None, "surface"),
            config=phase2_config,
            request_id="req-phase2-no-adjudication-retry",
            call_budget=call_budget,
        )

    assert result is None
    assert upstream_calls == 1
    assert call_budget.remaining == 1
