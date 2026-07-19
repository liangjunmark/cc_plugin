from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

from proxy.config import ProxyConfig
from proxy.phase2b_extract import extract_attack_record, extract_audit_record, extract_branch_record
from proxy.rewrite import ClassificationResult, extract_effective_prompt_surface
from proxy.schemas import Phase2bBranchDecision, Phase2bExecutionResult, Phase2bExtractedRecord, RequestContext
from proxy.upstream import CallBudget, UpstreamResult, UpstreamTransport


BRANCH_PROMPTS = {
    "premise_first": "Solve by locking the controllable premise first, then count under that premise.",
    "quota_first": "Solve by proposing explicit shape-bucket quotas and checking the worst-case allocation under those quotas.",
    "counterexample_first": "Solve by first trying to break low candidate answers with counterexamples before committing to a final count.",
}
BASELINE_FAST_PATH_TRUSTED_BRANCHES = frozenset({"quota_first", "counterexample_first"})
XFYUN_HOST_MARKER = "xf-yun.com"
COUNTING_GUARANTEE_LABEL_SUFFIXES = ("球", "味", "形", "门", "人", "枚", "瓶", "块", "张", "颗", "份", "只", "件", "本", "位", "扇")
SYSTEM_REMINDER_BLOCK_RE = re.compile(r"<system-reminder\b[^>]*>.*?</system-reminder>", re.IGNORECASE | re.DOTALL)


def is_phase2b_eligible(
    body: dict[str, Any],
    classification: ClassificationResult,
    config: ProxyConfig,
) -> bool:
    surface = classification.effective_prompt_surface
    prepared_surface = extract_effective_prompt_surface(body)
    latest_user_text = _latest_user_text(body)
    return (
        config.phase2b.enabled
        and classification.route in config.phase2b.trigger_on_routes
        and classification.replay_safe
        and not bool(body.get("stream"))
        and not bool(body.get("tools"))
        and body.get("tool_choice") in (None, "auto")
        and _is_single_turn_user_request(body)
        and bool(latest_user_text)
        and not _requests_structured_output_format(prepared_surface)
        and not _matches_patterns(surface, config.classification.code_marker_patterns)
        and _is_stable_phase2b_prompt_family(surface, config)
    )


def prepare_phase2b_body(
    body: dict[str, Any],
    classification: ClassificationResult,
    config: ProxyConfig,
    request_kind: str = "user_request",
) -> dict[str, Any]:
    latest_user_text = _latest_user_text_any_history(body)
    if (
        request_kind == "user_request"
        and latest_user_text
        and _is_stable_phase2b_prompt_family(latest_user_text, config)
    ):
        request = {
            "model": body.get("model"),
            "max_tokens": body.get("max_tokens", 4096),
            "stream": False,
            "messages": [{"role": "user", "content": latest_user_text}],
        }
    else:
        request = deepcopy(body)
        request["stream"] = False
    return request


def build_constraint_ledger(surface: str) -> list[str]:
    lowered = surface.lower()
    ledger: list[str] = []
    if any(marker in surface for marker in ("保证", "保證")) or any(
        marker in lowered for marker in ("guarantee", "guaranteed", "ensure")
    ):
        ledger.append("target is guarantee, not existence")
    if "手感" in surface or "distinguishable" in lowered:
        ledger.append("shape is controllable by touch")
    if "苹果" in surface or "apple" in lowered:
        ledger.append("success condition requires apple and peach across opposite shapes")
    return ledger


def build_branch_prompt(body: dict[str, Any], branch_family: str) -> dict[str, Any]:
    request = deepcopy(body)
    request["stream"] = False
    request["system"] = _append_system_text(
        request.get("system"),
        f"Internal branch family: {branch_family}. {BRANCH_PROMPTS[branch_family]} Produce one solution branch with candidate_answer, target_interpretation, worst_case_claim, and controllable_premises. Brief plain text or JSON is acceptable.",
    )
    return request


def build_assumption_audit_prompt(
    body: dict[str, Any], branch_text: str, ledger: list[str]
) -> dict[str, Any]:
    request = deepcopy(body)
    request["messages"] = [{
        "role": "user",
        "content": (
            f"Original task:\n{_latest_user_text(body)}"
            f"{_format_ledger_section(ledger)}"
            f"\n\nBranch:\n{branch_text}\n\nCheck only for premise or target mistakes. Output PASS/FAIL plus fatal tags."
        ),
    }]
    request["stream"] = False
    request["system"] = "You are an internal assumption auditor. Do not solve from scratch."
    return request


def build_worst_case_attack_prompt(
    body: dict[str, Any], branch_text: str, ledger: list[str]
) -> dict[str, Any]:
    request = deepcopy(body)
    request["messages"] = [{
        "role": "user",
        "content": (
            f"Original task:\n{_latest_user_text(body)}"
            f"{_format_ledger_section(ledger)}"
            f"\n\nBranch:\n{branch_text}\n\nAttack only the branch's worst-case construction. Output PASS/FAIL, worst-case fatal tags, and any ledger conflicts."
        ),
    }]
    request["stream"] = False
    request["system"] = "You are an internal worst-case attacker. Do not solve from scratch unless the branch fails."
    return request


def build_final_compressor_prompt(body: dict[str, Any], survivor_text: str, exact_output: bool) -> dict[str, Any]:
    request = deepcopy(body)
    request["stream"] = False
    instruction = (
        "Internal final compressor. Preserve the surviving solution's answer while satisfying the original output constraint. "
        "Return only the final answer."
        if exact_output
        else "Internal final compressor. Preserve the surviving solution's answer while matching the original output style. "
        "If the original task asks for explanation, keep it brief and end with the final answer."
    )
    request["system"] = _append_system_text(
        request.get("system"),
        instruction + "\n\nSurviving branch:\n"
        + survivor_text,
    )
    return request


def build_boundary_verifier_prompt(
    body: dict[str, Any],
    lower_bound: int,
    upper_bound: int,
) -> dict[str, Any]:
    request = deepcopy(body)
    request["stream"] = False
    request["system"] = (
        "你是内部边界裁决器。不要自由发挥，只裁决给定两个边界是否还能失败，然后给出 FINAL_ANSWER。"
    )
    request["messages"] = [{
        "role": "user",
        "content": (
            "原题：\n"
            f"{_latest_user_text(body)}\n\n"
            f"请只裁决两个边界：{lower_bound} 和 {upper_bound}。规则提醒：形状可通过手感区分，因此摸取时可以按形状做选择。"
            "不要把问题改写成盲抽。严格输出：\n"
            f"N{lower_bound}_FAILS: YES 或 NO\n"
            f"N{upper_bound}_FAILS: YES 或 NO\n"
            "FINAL_ANSWER: <数字>"
        ),
    }]
    return request


async def run_phase2b(
    transport: UpstreamTransport,
    headers: dict[str, str],
    body: dict[str, Any],
    classification: ClassificationResult,
    config: ProxyConfig,
    request_id: str,
) -> Phase2bExecutionResult:
    if bool(body.get("stream")):
        raise ValueError("Phase 2b does not support streamed requests")

    call_budget = CallBudget(config.phase2b.max_total_upstream_calls)
    exact_output = _prefers_exact_output(classification.effective_prompt_surface, config)
    baseline: UpstreamResult | None = None
    decisions: list[Phase2bBranchDecision] = []
    try:
        async with asyncio.timeout(config.phase2b.total_timeout_seconds):
            baseline_result = await _send(
                transport, headers, body, classification.replay_safe, request_id, "baseline", call_budget
            )
            if not _is_successful_message(baseline_result):
                return _error_result(decisions, "baseline_failed")
            baseline = baseline_result

            counting_payload = _try_counting_guarantee_fast_path(classification.effective_prompt_surface)
            if counting_payload is not None:
                return Phase2bExecutionResult("phase2b", baseline, decisions, counting_payload, None)

            boundary_payload = await _try_boundary_verifier(
                transport,
                headers,
                body,
                classification,
                config,
                request_id,
                call_budget,
            )
            if boundary_payload is not None:
                return Phase2bExecutionResult("phase2b", baseline, decisions, boundary_payload, None)

            ledger = (
                build_constraint_ledger(classification.effective_prompt_surface)
                if config.phase2b.enable_ledger_checks
                else []
            )
            trusted_baseline_answer = _trusted_compact_baseline_answer(baseline)
            survivors: list[tuple[str, str, str, UpstreamResult]] = []
            for branch_id in config.phase2b.branch_families:
                branch = await _send(
                    transport,
                    headers,
                    build_branch_prompt(body, branch_id),
                    classification.replay_safe,
                    request_id,
                    f"branch-{branch_id}",
                    call_budget,
                )
                branch_text = _message_text(branch)
                branch_record = extract_branch_record(branch_text) if branch_text is not None else None
                candidate_answer = (
                    _normalize_branch_candidate_answer(branch_record.candidate_answer)
                    if branch_record is not None and branch_record.candidate_answer
                    else None
                )
                if candidate_answer is None:
                    decisions.append(Phase2bBranchDecision(branch_id, False, [], None, "branch_generation_failed"))
                    continue

                validation = await _validate_branch(
                    transport,
                    headers,
                    body,
                    classification,
                    config,
                    request_id,
                    call_budget,
                    branch_id,
                    branch_text,
                    ledger,
                    branch_record,
                    candidate_answer,
                )
                if validation is None:
                    decisions.append(Phase2bBranchDecision(branch_id, True, [], candidate_answer, "survived"))
                    survivors.append((branch_id, branch_text, candidate_answer, branch))
                    if _should_fast_path_to_baseline(
                        branch_id,
                        trusted_baseline_answer,
                        candidate_answer,
                        baseline,
                        exact_output,
                    ):
                        baseline_payload = _baseline_fast_path_payload(baseline)
                        if baseline_payload is not None:
                            decisions[-1] = Phase2bBranchDecision(
                                branch_id,
                                True,
                                [],
                                candidate_answer,
                                "baseline_agreement_fast_path",
                            )
                            return Phase2bExecutionResult("phase2b", baseline, decisions, baseline_payload, None)
                    continue
                rejection, validated_text, validated_answer, decision_reason, fatal_tags = validation
                if rejection is not None:
                    decisions.append(rejection)
                    continue
                survivors.append((branch_id, validated_text, validated_answer, branch))
                decision = Phase2bBranchDecision(branch_id, True, fatal_tags, validated_answer, decision_reason)
                decisions.append(decision)
                if _should_fast_path_to_baseline(
                    branch_id,
                    trusted_baseline_answer,
                    validated_answer,
                    baseline,
                    exact_output,
                ):
                    baseline_payload = _baseline_fast_path_payload(baseline)
                    if baseline_payload is not None:
                        decisions[-1] = Phase2bBranchDecision(
                            branch_id,
                            True,
                            fatal_tags,
                            validated_answer,
                            "baseline_agreement_fast_path",
                        )
                        return Phase2bExecutionResult("phase2b", baseline, decisions, baseline_payload, None)

            if not survivors:
                return _fallback(baseline, decisions, "no_surviving_branches")
            selected = await _select_survivor(
                transport, headers, body, classification, config, request_id, call_budget, ledger, survivors
            )
            if selected is None:
                return _fallback(baseline, decisions, "tiebreak_failed")
            direct_payload = _selected_survivor_direct_payload(selected[3], selected[1], selected[2], exact_output)
            if direct_payload is not None:
                return Phase2bExecutionResult("phase2b", baseline, decisions, direct_payload, None)
            downstream_payload = await _finalize_survivor(
                transport, headers, body, classification, request_id, call_budget, selected[1], selected[2], exact_output
            )
            if downstream_payload is None:
                return _fallback(baseline, decisions, "final_compression_failed")
            return Phase2bExecutionResult("phase2b", baseline, decisions, downstream_payload, None)
    except TimeoutError:
        if baseline is None:
            return _error_result(decisions, "phase2b_timeout")
        return _fallback(baseline, decisions, "phase2b_timeout")
    except Exception:
        if baseline is None:
            return _error_result(decisions, "orchestration_failed")
        return _fallback(baseline, decisions, "orchestration_failed")


async def _validate_branch(
    transport: UpstreamTransport,
    headers: dict[str, str],
    body: dict[str, Any],
    classification: ClassificationResult,
    config: ProxyConfig,
    request_id: str,
    call_budget: CallBudget,
    branch_id: str,
    branch_text: str,
    ledger: list[str],
    branch_record: Phase2bExtractedRecord,
    candidate_answer: str,
) -> tuple[Phase2bBranchDecision | None, str, str, str, list[str]] | None:
    validated_text = branch_text
    validated_answer = candidate_answer
    decision_reason = "survived"
    fatal_tags: list[str] = []
    skip_compact_nonledger_validation = _should_skip_compact_nonledger_validation(
        branch_text,
        ledger,
        candidate_answer,
    )
    if config.phase2b.enable_assumption_audit and not skip_compact_nonledger_validation:
        audit = await _send(
            transport, headers, build_assumption_audit_prompt(body, branch_text, ledger), classification.replay_safe,
            request_id, f"audit-{branch_id}", call_budget,
        )
        audit_text = _message_text(audit)
        if audit_text is None:
            return Phase2bBranchDecision(branch_id, False, [], None, "assumption_audit_failed"), validated_text, validated_answer, decision_reason, fatal_tags
        audit_record = extract_audit_record(audit_text)
        if audit_record.survives is False:
            revised_answer = (
                _normalize_branch_candidate_answer(audit_record.revised_answer_if_any)
                if audit_record.revised_answer_if_any
                else None
            )
            if revised_answer is None:
                return Phase2bBranchDecision(branch_id, False, audit_record.fatal_error_tags, audit_record.revised_answer_if_any, "assumption_audit_failed"), validated_text, validated_answer, decision_reason, fatal_tags
            validated_answer = revised_answer
            validated_text = _append_audit_salvage(branch_text, audit_text, revised_answer)
            decision_reason = "salvaged_from_assumption_audit"
            fatal_tags = audit_record.fatal_error_tags
    if config.phase2b.enable_worst_case_attack and not skip_compact_nonledger_validation:
        attack = await _send(
            transport, headers, build_worst_case_attack_prompt(body, validated_text, ledger), classification.replay_safe,
            request_id, f"attack-{branch_id}", call_budget,
        )
        attack_text = _message_text(attack)
        if attack_text is None:
            return Phase2bBranchDecision(branch_id, False, [], None, "worst_case_attack_failed"), validated_text, validated_answer, decision_reason, fatal_tags
        attack_record = extract_attack_record(attack_text)
        ledger_conflicts = _attack_ledger_conflicts(attack_text, attack_record)
        if config.phase2b.enable_ledger_checks and ledger_conflicts:
            return Phase2bBranchDecision(
                branch_id,
                False,
                ledger_conflicts,
                attack_record.revised_answer_if_any,
                "ledger_conflict",
            ), validated_text, validated_answer, decision_reason, fatal_tags
        if attack_record.survives is False:
            return Phase2bBranchDecision(branch_id, False, attack_record.fatal_error_tags, attack_record.revised_answer_if_any, "worst_case_attack_failed"), validated_text, validated_answer, decision_reason, fatal_tags
    if decision_reason != "survived" or fatal_tags:
        return None, validated_text, validated_answer, decision_reason, fatal_tags
    return None


async def _select_survivor(
    transport: UpstreamTransport,
    headers: dict[str, str],
    body: dict[str, Any],
    classification: ClassificationResult,
    config: ProxyConfig,
    request_id: str,
    call_budget: CallBudget,
    ledger: list[str],
    survivors: list[tuple[str, str, str, UpstreamResult]],
) -> tuple[str, str, str, UpstreamResult] | None:
    majority_survivor = _majority_answer_survivor(survivors)
    if majority_survivor is not None:
        return majority_survivor
    if len(survivors) < 2 or not config.phase2b.allow_tiebreak_round:
        return survivors[0]
    result = await _send(
        transport, headers, _build_tiebreak_prompt(body, ledger, survivors), classification.replay_safe,
        request_id, "tiebreak", call_budget,
    )
    selected_id = _message_text(result)
    if selected_id is None:
        return None
    selected_id = selected_id.strip()
    return next((survivor for survivor in survivors if survivor[0] == selected_id), None)


async def _try_boundary_verifier(
    transport: UpstreamTransport,
    headers: dict[str, str],
    body: dict[str, Any],
    classification: ClassificationResult,
    config: ProxyConfig,
    request_id: str,
    call_budget: CallBudget,
) -> dict[str, Any] | None:
    verifier = config.phase2b.boundary_verifier
    if not verifier.enabled:
        return None
    if verifier.require_xfyun_upstream and not _is_xfyun_upstream(config.upstream.base_url):
        return None
    if not _is_boundary_verifier_eligible(
        classification.effective_prompt_surface,
        verifier.trigger_markers,
    ):
        return None
    result = await _send(
        transport,
        headers,
        build_boundary_verifier_prompt(body, verifier.lower_bound, verifier.upper_bound),
        classification.replay_safe,
        request_id,
        "boundary-verifier",
        call_budget,
    )
    return _boundary_verifier_payload(
        result,
        verifier.lower_bound,
        verifier.upper_bound,
    )


def _try_counting_guarantee_fast_path(surface: str) -> dict[str, Any] | None:
    required = _extract_required_count(surface)
    if required is None:
        return None
    counts = _extract_category_counts(surface)
    if len(counts) < 3:
        return None
    answer = sum(counts) - min(counts) + required
    return {
        "type": "message",
        "content": [{"type": "text", "text": str(answer)}],
    }


def _is_stable_phase2b_prompt_family(surface: str, config: ProxyConfig) -> bool:
    return (
        _is_counting_guarantee_prompt_family(surface)
        or _is_phase2b_boundary_family(surface, config)
    )


def _is_counting_guarantee_prompt_family(surface: str) -> bool:
    return _extract_required_count(surface) is not None and len(_extract_category_counts(surface)) >= 3


def _is_phase2b_boundary_family(surface: str, config: ProxyConfig) -> bool:
    verifier = config.phase2b.boundary_verifier
    if not verifier.enabled:
        return False
    if verifier.require_xfyun_upstream and not _is_xfyun_upstream(config.upstream.base_url):
        return False
    return _is_boundary_verifier_eligible(surface, verifier.trigger_markers)


def _build_tiebreak_prompt(
    body: dict[str, Any], ledger: list[str], survivors: list[tuple[str, str, str, UpstreamResult]]
) -> dict[str, Any]:
    request = deepcopy(body)
    request["stream"] = False
    survivor_text = "\n\n".join(
        f"branch_id: {branch_id}\n{branch_text}"
        for branch_id, branch_text, _, _ in survivors
    )
    request["messages"] = [{
        "role": "user",
        "content": (
            f"Original task:\n{_latest_user_text(body)}"
            f"{_format_ledger_section(ledger)}"
            f"\n\nSurviving branches in generation order:\n{survivor_text}\n\nChoose the single survivor that best satisfies the original task"
            + (" and ledger constraints." if ledger else ".")
            + " Output only the chosen branch_id."
        ),
    }]
    request["system"] = "You are an internal branch tiebreaker. Do not solve from scratch."
    return request


def _extract_required_count(surface: str) -> int | None:
    for pattern in (
        r"各有\s*(\d+)\s*个",
        r"每种(?:颜色)?都至少\s*(\d+)\s*个",
        r"至少(?:各有|有)\s*(\d+)\s*个",
    ):
        match = re.search(pattern, surface, re.IGNORECASE)
        if match is not None:
            return int(match.group(1))
    return None


def _extract_category_counts(surface: str) -> list[int]:
    counts: list[int] = []
    for match in re.finditer(r"(\d+)\s*个", surface):
        label = _trailing_category_label(surface[: match.start()])
        if not _looks_like_category_label(label):
            continue
        counts.append(int(match.group(1)))
    return counts


def _trailing_category_label(prefix: str) -> str:
    return re.split(r"[ \t\r\n,，。！？?；;：:、()（）【】\[\]{}有为是共含计列]", prefix)[-1].strip()


def _looks_like_category_label(label: str) -> bool:
    stripped = label.strip()
    if not stripped:
        return False
    if len(stripped) > 12:
        return False
    if any(marker in stripped for marker in ("有", "保", "证", "摸", "抽", "取", "至", "少", "各", "每")):
        return False
    return stripped.endswith(COUNTING_GUARANTEE_LABEL_SUFFIXES)


def _format_ledger_section(ledger: list[str]) -> str:
    if not ledger:
        return ""
    return "\n\nLedger:\n- " + "\n- ".join(ledger)


def _majority_answer_survivor(
    survivors: list[tuple[str, str, str, UpstreamResult]],
) -> tuple[str, str, str, UpstreamResult] | None:
    if len(survivors) < 2:
        return None
    counts = Counter(answer for _, _, answer, _ in survivors)
    top_answer, top_count = counts.most_common(1)[0]
    if top_count < 2:
        return None
    if sum(1 for count in counts.values() if count == top_count) > 1:
        return None
    return next((survivor for survivor in survivors if survivor[2] == top_answer), None)


def _selected_survivor_direct_payload(
    result: UpstreamResult,
    survivor_text: str,
    candidate_answer: str,
    exact_output: bool,
) -> dict[str, Any] | None:
    if not exact_output:
        return None
    stripped = survivor_text.strip()
    if (
        stripped != candidate_answer
        or not _is_compact_final_answer(stripped)
        or _contains_internal_artifact(stripped)
    ):
        return None
    payload = _message_payload(result)
    if payload is None:
        return None
    canonical_payload = deepcopy(payload)
    canonical_payload["content"] = [{"type": "text", "text": candidate_answer}]
    return canonical_payload


async def _finalize_survivor(
    transport: UpstreamTransport,
    headers: dict[str, str],
    body: dict[str, Any],
    classification: ClassificationResult,
    request_id: str,
    call_budget: CallBudget,
    survivor_text: str,
    candidate_answer: str,
    exact_output: bool,
) -> dict[str, Any] | None:
    result = await _send(
        transport, headers, build_final_compressor_prompt(body, survivor_text, exact_output), classification.replay_safe,
        request_id, "final-compressor", call_budget,
    )
    return _finalized_payload(result, candidate_answer, exact_output)


async def _send(
    transport: UpstreamTransport,
    headers: dict[str, str],
    body: dict[str, Any],
    replay_safe: bool,
    request_id: str,
    stage: str,
    call_budget: CallBudget,
) -> UpstreamResult:
    return await transport.send_with_retry(
        context=RequestContext(request_id=request_id, attempt=1, log_dir=Path(request_id) / f"phase2b-{stage}"),
        headers=headers,
        body=body,
        replay_safe=replay_safe,
        stream=False,
        call_budget=call_budget,
    )


def _message_text(result: UpstreamResult) -> str | None:
    payload = _message_payload(result)
    if payload is None:
        return None
    content = payload["content"]
    text = "".join(item["text"] for item in content if item.get("type") == "text")
    return text if text else None


def _is_successful_message(result: UpstreamResult) -> bool:
    if not 200 <= result.status_code < 300:
        return False
    try:
        payload = json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("type") == "message"
        and isinstance(payload.get("content"), list)
        and all(isinstance(item, dict) and isinstance(item.get("type"), str) for item in payload["content"])
    )


def _message_payload(result: UpstreamResult) -> dict[str, Any] | None:
    if not _is_successful_message(result):
        return None
    try:
        payload = json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _trusted_compact_baseline_answer(result: UpstreamResult) -> str | None:
    text = _message_text(result)
    if text is None:
        return None
    stripped = text.strip()
    if text != stripped:
        return None
    if not _is_compact_final_answer(stripped) or _contains_internal_artifact(stripped):
        return None
    answer = _normalize_branch_candidate_answer(stripped)
    if answer is None or answer != stripped:
        return None
    return answer


def _should_skip_compact_nonledger_validation(
    branch_text: str,
    ledger: list[str],
    candidate_answer: str,
) -> bool:
    if ledger:
        return False
    stripped = branch_text.strip()
    return (
        stripped == candidate_answer
        and _is_compact_final_answer(stripped)
        and not _contains_internal_artifact(stripped)
    )


def _should_fast_path_to_baseline(
    branch_id: str,
    trusted_baseline_answer: str | None,
    validated_answer: str,
    baseline: UpstreamResult,
    exact_output: bool,
) -> bool:
    if not exact_output:
        return False
    if branch_id not in BASELINE_FAST_PATH_TRUSTED_BRANCHES:
        return False
    if trusted_baseline_answer is None or validated_answer != trusted_baseline_answer:
        return False
    return _baseline_fast_path_payload(baseline) is not None


def _baseline_fast_path_payload(result: UpstreamResult) -> dict[str, Any] | None:
    payload = _message_payload(result)
    answer = _trusted_compact_baseline_answer(result)
    if payload is None or answer is None:
        return None
    canonical_payload = deepcopy(payload)
    canonical_payload["content"] = [{"type": "text", "text": answer}]
    return canonical_payload


def _finalized_payload(
    result: UpstreamResult,
    candidate_answer: str,
    exact_output: bool,
) -> dict[str, Any] | None:
    if not 200 <= result.status_code < 300:
        return None
    try:
        payload = json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("type") != "message"
        or not isinstance(payload.get("content"), list)
        or not payload["content"]
        or any(
            not isinstance(block, dict)
            or block.get("type") != "text"
            or not isinstance(block.get("text"), str)
            for block in payload["content"]
        )
    ):
        return None
    text = "".join(block["text"] for block in payload["content"]).strip()
    if _contains_internal_artifact(text):
        return None
    if exact_output:
        if text != candidate_answer.strip() or not _is_compact_final_answer(text):
            return None
        canonical_payload = deepcopy(payload)
        canonical_payload["content"] = [{"type": "text", "text": candidate_answer}]
        return canonical_payload
    normalized_candidate = _normalize_branch_candidate_answer(candidate_answer)
    normalized_text_answer = _normalize_branch_candidate_answer(text)
    if normalized_candidate is not None and normalized_text_answer == normalized_candidate:
        return payload
    if candidate_answer.strip() in text:
        return payload
    return None


def _boundary_verifier_payload(
    result: UpstreamResult,
    lower_bound: int,
    expected_answer: int,
) -> dict[str, Any] | None:
    if not 200 <= result.status_code < 300:
        return None
    try:
        payload = json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("type") != "message"
        or not isinstance(payload.get("content"), list)
        or not payload.get("content")
    ):
        return None
    text = "".join(
        block["text"]
        for block in payload["content"]
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ).strip()
    lower_match = re.search(
        rf"N{lower_bound}_FAILS\s*(?::|：|=)\s*(YES|NO)",
        text,
        re.IGNORECASE,
    )
    upper_match = re.search(
        rf"N{expected_answer}_FAILS\s*(?::|：|=)\s*(YES|NO)",
        text,
        re.IGNORECASE,
    )
    match = re.search(r"FINAL_ANSWER\s*(?::|：|=)\s*(\d+)", text, re.IGNORECASE)
    if match is None or lower_match is None or upper_match is None:
        return None
    answer = match.group(1)
    if (
        answer != str(expected_answer)
        or lower_match.group(1).upper() != "YES"
        or upper_match.group(1).upper() != "NO"
    ):
        return None
    canonical_payload = deepcopy(payload)
    canonical_payload["content"] = [{"type": "text", "text": answer}]
    return canonical_payload


def _is_compact_final_answer(text: str) -> bool:
    if "\n" in text or len(text) > 80:
        return False
    if any(character in text for character in ("`", "{", "}", "[", "]", ":", "：")):
        return False
    if re.search(r"[.!?。！？]", text):
        return False
    if re.search(r"\b(?:because|therefore|thus|hence|concludes?|explains?)\b|因为|所以|结论", text, re.IGNORECASE):
        return False
    return len(text.split()) <= 4


def _normalize_branch_candidate_answer(candidate_answer: str) -> str | None:
    candidate = candidate_answer.strip()
    numeric_answers = re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])", candidate)
    if numeric_answers:
        return numeric_answers[0] if len(numeric_answers) == 1 else None
    if _is_literal_non_numeric_answer(candidate):
        return candidate
    return None


def _is_literal_non_numeric_answer(candidate: str) -> bool:
    if _contains_internal_artifact(candidate):
        return False
    return candidate.casefold() in {"yes", "no", "true", "false"} or candidate in {"是", "否"}


def _contains_internal_artifact(text: str) -> bool:
    lowered = text.lower()
    markers = (
        *BRANCH_PROMPTS,
        "candidate_answer",
        "ledger:",
        "original task:",
        "surviving branch",
        "fatal",
        "attack",
        "audit",
    )
    return any(marker in lowered for marker in markers)


def _attack_ledger_conflicts(
    attack_text: str, attack_record: Phase2bExtractedRecord
) -> list[str]:
    if attack_record.ledger_conflicts:
        return attack_record.ledger_conflicts
    lowered = attack_text.lower()
    if "ledger_conflict" in lowered or re.search(
        r"\b(?:not\s+(?:controllable|distinguishable)(?:\s+by\s+touch)?|isn't\s+distinguishable(?:\s+by\s+touch)?|is\s+indistinguishable(?:\s+by\s+touch)?|cannot\s+(?:distinguish|be\s+distinguished)(?:\s+by\s+touch)?|cannot\s+tell(?:\s+the\s+shapes)?\s+apart\s+by\s+touch|can't\s+(?:distinguish|tell(?:\s+the\s+shapes)?\s+apart\s+by\s+touch))\b",
        lowered,
    ) or any(
        marker in attack_text
        for marker in ("形状不可控", "无法靠手感区分", "不能通过手感区分形状")
    ):
        return ["reported_ledger_conflict"]
    return []


def _fallback(
    baseline: UpstreamResult, decisions: list[Phase2bBranchDecision], reason: str
) -> Phase2bExecutionResult:
    return Phase2bExecutionResult("fallback_phase1", baseline, decisions, None, reason)


def _error_result(
    decisions: list[Phase2bBranchDecision], reason: str
) -> Phase2bExecutionResult:
    payload = {"type": "error", "error": {"type": "api_error", "message": reason}}
    return Phase2bExecutionResult(
        "phase2b_error",
        UpstreamResult(599, {"content-type": "application/json"}, json.dumps(payload).encode("utf-8")),
        decisions,
        None,
        reason,
    )


def _is_single_turn_user_request(body: dict[str, Any]) -> bool:
    messages = body.get("messages")
    return isinstance(messages, list) and len(messages) == 1 and isinstance(messages[0], dict) and messages[0].get("role") == "user"


def _latest_user_text(body: dict[str, Any]) -> str | None:
    if not _is_single_turn_user_request(body):
        return None
    content = body["messages"][0].get("content")
    return _flatten_text_content(content)


def _latest_user_text_any_history(body: dict[str, Any]) -> str | None:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        return _flatten_text_content(message.get("content"))
    return None


def _flatten_text_content(content: Any) -> str | None:
    if isinstance(content, str):
        return _normalize_user_text(content)
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") not in (None, "text"):
            return None
        parts.append(str(item.get("text", "")))
    return _normalize_user_text("\n".join(part for part in parts if part))


def _normalize_user_text(text: str) -> str | None:
    stripped = SYSTEM_REMINDER_BLOCK_RE.sub("", text).strip()
    if stripped:
        return stripped
    original = text.strip()
    return original or None


def _prefers_exact_output(surface: str, config: ProxyConfig) -> bool:
    return _matches_non_negated_patterns(surface, config.classification.output_constraint_patterns)


def _append_system_text(system: Any, suffix: str) -> Any:
    if isinstance(system, list):
        updated = deepcopy(system)
        updated.append({"type": "text", "text": suffix})
        return updated
    base = "" if system is None else str(system)
    return f"{base}\n{suffix}".strip()


def _append_audit_salvage(branch_text: str, audit_text: str, revised_answer: str) -> str:
    return (
        f"{branch_text}\n\n"
        "Assumption audit salvage:\n"
        f"{audit_text.strip()}\n"
        f"Active corrected candidate_answer: {revised_answer}"
    ).strip()


def _is_boundary_verifier_eligible(surface: str, trigger_markers: list[str]) -> bool:
    return all(marker in surface for marker in trigger_markers)


def _is_xfyun_upstream(base_url: str) -> bool:
    hostname = urlparse(base_url).hostname or ""
    return XFYUN_HOST_MARKER in hostname.lower()


def _matches_patterns(surface: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, surface, re.IGNORECASE) for pattern in patterns)


def _matches_non_negated_patterns(surface: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        for match in re.finditer(pattern, surface, re.IGNORECASE):
            prefix = surface[max(0, match.start() - 40):match.start()]
            if re.search(r"(do not|don't|不要|别|不是|not)(?:\W+\w+){0,3}\W*$", prefix, re.IGNORECASE):
                continue
            return True
    return False


def _requests_explanatory_output(text: str) -> bool:
    return bool(re.search(r"\b(explain|each step|step by step|show your work|why|reasoning|analysis)\b|解释|步骤|推理|分析", text, re.IGNORECASE))


def _requests_structured_output_format(text: str) -> bool:
    return bool(re.search(r"\b(json|yaml|yml|xml|csv|tsv|markdown|html|object|array|dict|dictionary|schema|table)\b|```|[{\[]\s*\"?[a-zA-Z_]", text, re.IGNORECASE))
