from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any

from proxy.candidate import (
    build_candidate_request,
    canonicalize_answer,
    parse_adjudication_response,
    parse_candidate_response,
)
from proxy.config import ProxyConfig
from proxy.rewrite import ClassificationResult
from proxy.schemas import AggregationDecision, CandidateAnswer, Phase2ExecutionResult, RequestContext
from proxy.upstream import CallBudget, UpstreamResult, UpstreamTransport


def is_phase2_eligible(
    body: dict[str, Any],
    classification: ClassificationResult,
    config: ProxyConfig,
) -> bool:
    latest_user_text = _latest_user_text(body)
    surface = classification.effective_prompt_surface
    return (
        config.phase2.enabled
        and classification.route in config.phase2.trigger_on_routes
        and classification.replay_safe
        and not bool(body.get("stream"))
        and not bool(body.get("tools"))
        and body.get("tool_choice") in (None, "auto")
        and _is_single_turn_user_request(body)
        and latest_user_text is not None
        and _matches_patterns(
            latest_user_text,
            config.classification.reasoning_keyword_patterns,
        )
        and _matches_non_negated_patterns(
            surface,
            config.classification.output_constraint_patterns,
        )
        and not _requests_explanatory_output(surface)
        and not _requests_structured_output_format(surface)
        and not _matches_patterns(
            surface,
            config.classification.code_marker_patterns,
        )
        and _within_phase2_cost_budget(body, config)
    )


def _is_single_turn_user_request(body: dict[str, Any]) -> bool:
    messages = body.get("messages")
    return (
        isinstance(messages, list)
        and len(messages) == 1
        and isinstance(messages[0], dict)
        and messages[0].get("role") == "user"
    )


def _within_phase2_cost_budget(body: dict[str, Any], config: ProxyConfig) -> bool:
    baseline_budget = int(body.get("max_tokens", 0))
    phase2_budget = baseline_budget + (
        config.phase2.sample_count * config.phase2.max_candidate_output_tokens
        + config.phase2.max_adjudication_calls * config.phase2.max_candidate_output_tokens
    )
    return phase2_budget <= int(baseline_budget * config.phase2.cost_budget_multiplier)


def _latest_user_text(body: dict[str, Any]) -> str | None:
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        return None
    message = messages[0]
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") not in (None, "text"):
            return None
        parts.append(str(item.get("text", "")))
    return "\n".join(part for part in parts if part).strip()


def aggregate_candidates(
    candidates: list[CandidateAnswer],
    budget_allows_adjudication: bool,
) -> AggregationDecision:
    if len(candidates) < 2:
        return AggregationDecision("fallback_baseline", None, "insufficient_candidates")

    canonical_groups: dict[tuple[str, str], list[CandidateAnswer]] = {}
    for candidate in candidates:
        if candidate.canonical_final_answer is not None:
            canonical_groups.setdefault((candidate.answer_type, candidate.canonical_final_answer), []).append(candidate)

    majority_groups = [grouped for grouped in canonical_groups.values() if len(grouped) >= 2]
    if majority_groups:
        majority_groups.sort(key=len, reverse=True)
        largest_group = majority_groups[0]
        if len(majority_groups) > 1 and len(majority_groups[1]) == len(largest_group):
            return _adjudication_or_fallback(budget_allows_adjudication, "candidate_disagreement")
        if _summaries_compatible(largest_group):
            raw_answers = {candidate.raw_final_answer for candidate in largest_group}
            if len(raw_answers) == 1:
                return AggregationDecision("choose_candidate", next(iter(raw_answers)), "compatible_majority")
            return _adjudication_or_fallback(budget_allows_adjudication, "raw_format_conflict")
        return _adjudication_or_fallback(budget_allows_adjudication, "summary_conflict")

    non_canonical_count = sum(candidate.non_canonical for candidate in candidates)
    canonical_count = len(candidates) - non_canonical_count
    if canonical_count and non_canonical_count > canonical_count:
        return _adjudication_or_fallback(budget_allows_adjudication, "non_canonical_majority")

    return _adjudication_or_fallback(budget_allows_adjudication, "candidate_disagreement")


async def run_phase2(
    transport: UpstreamTransport,
    headers: dict[str, str],
    body: dict[str, Any],
    classification: ClassificationResult,
    config: ProxyConfig,
    request_id: str,
) -> Phase2ExecutionResult:
    if bool(body.get("stream")):
        raise ValueError("Phase 2 does not support streamed requests")

    call_budget = CallBudget(config.phase2.max_total_upstream_calls)
    baseline: UpstreamResult | None = None
    candidates: list[CandidateAnswer] = []
    stage = "baseline"
    try:
        async with asyncio.timeout(config.phase2.total_timeout_seconds):
            baseline = await transport.send_with_retry(
                context=RequestContext(request_id=request_id, attempt=1, log_dir=Path(request_id) / "phase2-baseline"),
                headers=headers,
                body=body,
                replay_safe=classification.replay_safe,
                stream=False,
                call_budget=call_budget,
            )
            if not 200 <= baseline.status_code < 300:
                return _fallback_result(baseline, [], "baseline_failed")
            if not _is_valid_anthropic_message_result(baseline):
                return _fallback_result(baseline, [], "baseline_invalid_success_payload")
            stage = "candidate"
            candidates = await _run_structured_candidates(
                transport=transport,
                headers=headers,
                body=body,
                classification=classification,
                config=config,
                request_id=request_id,
                call_budget=call_budget,
            )
            budget_allows_adjudication = (
                config.phase2.max_adjudication_calls > 0
                and call_budget.remaining >= config.phase2.max_adjudication_calls
            )
            decision = aggregate_candidates(candidates, budget_allows_adjudication)
            if decision.decision == "choose_candidate":
                return Phase2ExecutionResult(
                    "phase2",
                    baseline,
                    candidates,
                    decision,
                    _candidate_to_anthropic_message(decision.chosen_answer or "", str(body.get("model", ""))),
                )
            if decision.decision == "run_adjudication":
                stage = "adjudication"
                try:
                    adjudication = await _run_adjudication(
                        transport=transport,
                        headers=headers,
                        body=body,
                        candidates=candidates,
                        classification=classification,
                        config=config,
                        request_id=request_id,
                        call_budget=call_budget,
                    )
                except TimeoutError:
                    raise
                except Exception:
                    return _fallback_result(baseline, candidates, "adjudication_failed")
                if adjudication is not None:
                    final_answer, decision_summary = adjudication
                    return Phase2ExecutionResult(
                        "phase2",
                        baseline,
                        candidates,
                        AggregationDecision("choose_candidate", final_answer, decision_summary),
                        _candidate_to_anthropic_message(final_answer, str(body.get("model", ""))),
                    )
                return _fallback_result(baseline, candidates, "invalid_adjudication")
    except TimeoutError:
        if baseline is None:
            return _fallback_result(_timeout_result(), [], "baseline_timeout")
        return _fallback_result(baseline, candidates, f"{stage}_timeout")

    return Phase2ExecutionResult("fallback_phase1", baseline, candidates, decision, None)


async def _run_structured_candidates(
    transport: UpstreamTransport,
    headers: dict[str, str],
    body: dict[str, Any],
    classification: ClassificationResult,
    config: ProxyConfig,
    request_id: str,
    call_budget: CallBudget,
) -> list[CandidateAnswer]:
    semaphore = asyncio.Semaphore(config.phase2.max_parallelism)

    async def run_candidate(role: str) -> CandidateAnswer | None:
        async with semaphore:
            result = await transport.send_with_retry(
                context=RequestContext(
                    request_id=request_id,
                    attempt=1,
                    log_dir=Path(request_id) / f"phase2-candidate-{role}",
                ),
                headers=headers,
                body=build_candidate_request(body, role, config.phase2.max_candidate_output_tokens),
                replay_safe=classification.replay_safe,
                stream=False,
                call_budget=call_budget,
            )
        return _parse_candidate_result(role, result, classification.effective_prompt_surface)

    results = await asyncio.gather(
        *(run_candidate(role) for role in config.phase2.candidate_roles),
        return_exceptions=True,
    )
    return [result for result in results if isinstance(result, CandidateAnswer)]


async def _run_adjudication(
    transport: UpstreamTransport,
    headers: dict[str, str],
    body: dict[str, Any],
    candidates: list[CandidateAnswer],
    classification: ClassificationResult,
    config: ProxyConfig,
    request_id: str,
    call_budget: CallBudget,
) -> tuple[str, str] | None:
    result = await transport.send_with_retry(
        context=RequestContext(
            request_id=request_id,
            attempt=1,
            log_dir=Path(request_id) / "phase2-adjudication",
        ),
        headers=headers,
        body=_build_adjudication_request(body, candidates, config.phase2.max_candidate_output_tokens),
        replay_safe=False,
        stream=False,
        call_budget=call_budget,
    )
    if not 200 <= result.status_code < 300:
        return None
    try:
        payload = json.loads(result.body.decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        final_answer, answer_type, decision_summary = parse_adjudication_response(payload)
        _, non_canonical = canonicalize_answer(final_answer, answer_type)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, KeyError):
        return None
    if non_canonical or not final_answer.strip() or not decision_summary.strip():
        return None
    return final_answer, decision_summary.strip()


def _parse_candidate_result(role: str, result: UpstreamResult, task_surface: str) -> CandidateAnswer | None:
    if not 200 <= result.status_code < 300:
        return None
    try:
        payload = json.loads(result.body.decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        return parse_candidate_response(role, payload, task_surface=task_surface)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None


def _build_adjudication_request(
    body: dict[str, Any],
    candidates: list[CandidateAnswer],
    max_output_tokens: int,
) -> dict[str, Any]:
    request = deepcopy(body)
    request["stream"] = False
    request["max_tokens"] = max_output_tokens
    thinking = request.get("thinking")
    if "thinking" in request and not isinstance(thinking, dict):
        request.pop("thinking", None)
    elif isinstance(thinking, dict):
        budget = thinking.get("budget_tokens")
        if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0 or budget >= max_output_tokens:
            request.pop("thinking", None)
    summaries = [
        {
            "role": candidate.role,
            "final_answer": candidate.raw_final_answer,
            "answer_type": candidate.answer_type,
            "constraint_summary": candidate.constraint_summary,
            "critical_steps": candidate.critical_steps,
            "counterexample_checked": candidate.counterexample_checked,
            "confidence": candidate.confidence,
        }
        for candidate in candidates
    ]
    prompt = (
        "Adjudicate the candidate answers below. Reject candidates that violate explicit constraints. "
        "Return strict JSON only with final_answer, answer_type, and a short decision_summary.\n"
        f"Candidate summaries:\n{json.dumps(summaries)}"
    )
    request["system"] = _append_system_text(request.get("system"), prompt)
    return request


def _append_system_text(system: Any, suffix: str) -> Any:
    if isinstance(system, list):
        updated = deepcopy(system)
        updated.append({"type": "text", "text": suffix})
        return updated
    base = "" if system is None else str(system)
    return f"{base}\n{suffix}".strip()


def _matches_patterns(surface: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, surface, re.IGNORECASE) for pattern in patterns)


def _matches_non_negated_patterns(surface: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        for match in re.finditer(pattern, surface, re.IGNORECASE):
            prefix = surface[max(0, match.start() - 40) : match.start()]
            if re.search(r"(do not|don't|不要|别|不是|not)(?:\W+\w+){0,3}\W*$", prefix, re.IGNORECASE):
                continue
            return True
    return False


def _requests_explanatory_output(text: str) -> bool:
    return bool(
        re.search(
            r"\b(explain|each step|step by step|show your work|why|reasoning|analysis)\b|解释|步骤|推理|分析",
            text,
            re.IGNORECASE,
        )
    )


def _requests_structured_output_format(text: str) -> bool:
    return bool(
        re.search(
            r"\b(json|yaml|yml|xml|csv|tsv|markdown|html|object|array|dict|dictionary|schema|table)\b|```|[{\[]\s*\"?[a-zA-Z_]",
            text,
            re.IGNORECASE,
        )
    )


def _is_valid_anthropic_message_result(result: UpstreamResult) -> bool:
    try:
        payload = json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    content = payload.get("content")
    return (
        payload.get("type") == "message"
        and isinstance(content, list)
        and all(_is_valid_content_block(item) for item in content)
    )


def _is_valid_content_block(item: Any) -> bool:
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


def _summaries_compatible(candidates: list[CandidateAnswer]) -> bool:
    summary_sets = [set(_normalize_summary_items(candidate.constraint_summary)) for candidate in candidates]
    common_summary = set.intersection(*summary_sets)
    if not common_summary:
        return False
    critical_step_sets = [set(_normalize_summary_items(candidate.critical_steps)) for candidate in candidates]
    common_steps = set.intersection(*critical_step_sets)
    if not common_steps:
        return False
    normalized = [list(items) for items in summary_sets]
    return not _has_explicit_summary_conflict(normalized, [candidate.constraint_summary for candidate in candidates])


def _normalize_summary_items(items: list[str]) -> list[str]:
    normalized_items = []
    for item in items:
        normalized = " ".join(item.casefold().split())
        normalized = normalized.replace("cannot be", "can not be")
        normalized = normalized.replace("is not", "is")
        normalized_items.append(normalized)
    return normalized_items


def _has_explicit_summary_conflict(summaries: list[list[str]], raw_summaries: list[list[str]]) -> bool:
    items = [
        (item, raw_item)
        for summary, raw_summary in zip(summaries, raw_summaries, strict=True)
        for item, raw_item in zip(summary, raw_summary, strict=True)
    ]
    for index, (item, raw_item) in enumerate(items):
        for other, other_raw_item in items[index + 1 :]:
            if (
                _contains_explicit_negation(raw_item) != _contains_explicit_negation(other_raw_item)
                and _without_negation(item) == _without_negation(other)
            ):
                return True
    return False


def _without_negation(item: str) -> str:
    without_negation = re.sub(r"\bnot\s+", "", item)
    without_auxiliary = re.sub(r"\b(?:is|can(?:\s+be)?)\s+", "", without_negation)
    return re.sub(r"\bdistinguished\b", "distinguishable", without_auxiliary)


def _contains_explicit_negation(item: str) -> bool:
    return bool(re.search(r"\b(?:cannot|not)\b", item.casefold()))


def _adjudication_or_fallback(budget_allows_adjudication: bool, reason: str) -> AggregationDecision:
    if budget_allows_adjudication:
        return AggregationDecision("run_adjudication", None, reason)
    return AggregationDecision("fallback_baseline", None, reason)


def _fallback_result(
    baseline: UpstreamResult,
    candidates: list[CandidateAnswer],
    reason: str,
) -> Phase2ExecutionResult:
    return Phase2ExecutionResult(
        "fallback_phase1",
        baseline,
        candidates,
        AggregationDecision("fallback_baseline", None, reason),
        None,
    )


def _timeout_result() -> UpstreamResult:
    return UpstreamResult(
        status_code=599,
        headers={},
        body=b"phase2 baseline timed out",
        streamed=False,
    )


def _candidate_to_anthropic_message(answer: str, model: str) -> dict[str, Any]:
    return {
        "type": "message",
        "id": "phase2",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": answer}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
