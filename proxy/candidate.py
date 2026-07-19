from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any

from proxy.evals import extract_response_text
from proxy.schemas import CandidateAnswer

ROLE_PROMPTS = {
    "constraint_reasoner": (
        "List explicit constraints, apply worst-case reasoning, and output strict JSON only with this schema: "
        '{"final_answer":"string","answer_type":"number|string|boolean|text","constraint_summary":["string"],'
        '"critical_steps":["string"],"counterexample_checked":true,"confidence":"low|medium|high"}.'
    ),
    "counterexample_reasoner": (
        "Search for a violating construction before finalizing the answer, and output strict JSON only with this schema: "
        '{"final_answer":"string","answer_type":"number|string|boolean|text","constraint_summary":["string"],'
        '"critical_steps":["string"],"counterexample_checked":true,"confidence":"low|medium|high"}.'
    ),
}
MAX_CANONICAL_NUMBER_LENGTH = 1_000_000
ANSWER_TYPES = {"number", "string", "boolean", "text"}
CONFIDENCE_VALUES = {"low", "medium", "high"}
GENERIC_REASONING_PLACEHOLDERS = {
    "shape",
    "step",
    "constraint",
    "constraints",
    "reasoning",
    "analysis",
    "must satisfy all constraints",
}
SURFACE_CUE_STOPWORDS = {
    "only",
    "output",
    "final",
    "answer",
    "with",
    "when",
    "that",
    "this",
    "from",
    "into",
    "before",
    "after",
    "find",
}
PREMISE_CONTROL_SURFACE_MARKERS = ("touch", "distinguish", "shape", "手感", "区分", "形状")
PREMISE_CONTROL_CANDIDATE_MARKERS = ("touch", "distinguish", "bucket", "quota", "手感", "区分")
MINIMUM_GUARANTEE_SURFACE_MARKERS = ("minimum", "worst case", "guarantee", "最少", "保证")
MINIMUM_GUARANTEE_CANDIDATE_MARKERS = ("worst", "bucket", "quota", "counterexample", "pigeonhole", "最少")


def canonicalize_answer(raw: str, answer_type: str) -> tuple[str | None, bool]:
    normalized = raw.strip()
    if answer_type == "number":
        try:
            value = Decimal(normalized)
        except InvalidOperation:
            return None, True
        if not value.is_finite():
            return None, True
        canonical = _canonicalize_finite_decimal(value)
        if canonical is None:
            return None, True
        return canonical, False
    if answer_type == "boolean":
        lowered = normalized.lower()
        if lowered in {"true", "false"}:
            return lowered, False
        return None, True
    if answer_type in {"string", "text"}:
        return normalized, False
    return None, True


def build_candidate_request(body: dict[str, Any], role: str, max_output_tokens: int) -> dict[str, Any]:
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
    request["system"] = _append_system_text(request.get("system"), ROLE_PROMPTS[role])
    return request


def parse_candidate_response(role: str, payload: dict[str, Any], task_surface: str | None = None) -> CandidateAnswer:
    parsed = _parse_json_text(payload, "candidate response must contain JSON text")
    answer_type = _require_choice(parsed["answer_type"], "answer_type", ANSWER_TYPES)
    raw_final_answer = _require_string(parsed["final_answer"], "final_answer")
    if answer_type in {"string", "text"} and not raw_final_answer.strip():
        raise ValueError("final_answer must not be empty")
    canonical, non_canonical = canonicalize_answer(raw_final_answer, answer_type)
    counterexample_checked = parsed["counterexample_checked"]
    if not isinstance(counterexample_checked, bool):
        raise ValueError("counterexample_checked must be a boolean")
    if not counterexample_checked:
        raise ValueError("counterexample_checked must be true")
    constraint_summary = _require_string_list(parsed["constraint_summary"], "constraint_summary")
    critical_steps = _require_string_list(parsed["critical_steps"], "critical_steps")
    if not _reasoning_artifacts_are_task_relevant(constraint_summary, critical_steps):
        raise ValueError("reasoning artifacts must be task-relevant enough to support compatibility checks")
    if task_surface is not None and not _reasoning_artifacts_match_task_surface(constraint_summary, critical_steps, task_surface):
        raise ValueError("reasoning artifacts must stay relevant to the task")
    return CandidateAnswer(
        role=role,
        raw_final_answer=raw_final_answer,
        canonical_final_answer=canonical,
        answer_type=answer_type,
        constraint_summary=constraint_summary,
        critical_steps=critical_steps,
        counterexample_checked=counterexample_checked,
        confidence=_require_choice(parsed["confidence"], "confidence", CONFIDENCE_VALUES),
        non_canonical=non_canonical,
    )


def _require_string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field_name} items must be strings")
    return value


def _reasoning_artifacts_are_task_relevant(
    constraint_summary: list[str],
    critical_steps: list[str],
) -> bool:
    return any(_is_specific_reasoning_item(item) for item in [*constraint_summary, *critical_steps])


def _is_specific_reasoning_item(item: str) -> bool:
    normalized = " ".join(item.casefold().split())
    if normalized in GENERIC_REASONING_PLACEHOLDERS:
        return False
    if re.search(r"[\u4e00-\u9fff]", normalized):
        return len(normalized.replace(" ", "")) >= 4
    words = [word for word in re.findall(r"[a-z0-9]+", normalized) if len(word) >= 3]
    return len(words) >= 2 or len(normalized.replace(" ", "")) >= 12


def _reasoning_artifacts_match_task_surface(
    constraint_summary: list[str],
    critical_steps: list[str],
    task_surface: str,
) -> bool:
    surface = " ".join(task_surface.casefold().split())
    candidate_text = " ".join([*constraint_summary, *critical_steps]).casefold()
    surface_words = {
        word
        for word in re.findall(r"[a-z0-9]+", surface)
        if len(word) >= 4 and word not in SURFACE_CUE_STOPWORDS
    }
    active_families = []
    if _surface_mentions_any(surface, PREMISE_CONTROL_SURFACE_MARKERS):
        active_families.append(PREMISE_CONTROL_CANDIDATE_MARKERS)
    if _surface_mentions_any(surface, MINIMUM_GUARANTEE_SURFACE_MARKERS):
        active_families.append(MINIMUM_GUARANTEE_CANDIDATE_MARKERS)
    if active_families:
        return all(_surface_mentions_any(candidate_text, markers) for markers in active_families)
    if len(surface_words) >= 2:
        overlap = sum(1 for word in surface_words if word in candidate_text)
        return overlap >= 2
    return True


def _has_useful_task_signals(surface: str) -> bool:
    surface_words = {
        word
        for word in re.findall(r"[a-z0-9]+", surface)
        if len(word) >= 4 and word not in SURFACE_CUE_STOPWORDS
    }
    return (
        len(surface_words) >= 2
        or _surface_mentions_any(surface, PREMISE_CONTROL_SURFACE_MARKERS)
        or _surface_mentions_any(surface, MINIMUM_GUARANTEE_SURFACE_MARKERS)
    )


def _surface_mentions_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _require_choice(value: Any, field_name: str, allowed: set[str]) -> str:
    choice = _require_string(value, field_name)
    if choice not in allowed:
        raise ValueError(f"{field_name} must be one of: {', '.join(sorted(allowed))}")
    return choice


def parse_adjudication_response(payload: dict[str, Any]) -> tuple[str, str, str]:
    parsed = _parse_json_text(payload, "adjudication response must contain JSON text")
    return (
        _require_string(parsed["final_answer"], "final_answer"),
        _require_choice(parsed["answer_type"], "answer_type", ANSWER_TYPES),
        _require_string(parsed["decision_summary"], "decision_summary"),
    )


def _parse_json_text(payload: dict[str, Any], error_message: str) -> dict[str, Any]:
    if payload.get("type") not in (None, "message"):
        raise ValueError(error_message)
    content = payload.get("content")
    if not isinstance(content, list):
        raise ValueError(error_message)
    text = extract_response_text(payload)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(error_message) from exc
    if not isinstance(parsed, dict):
        raise ValueError(error_message)
    return parsed


def _append_system_text(system: Any, suffix: str) -> Any:
    if isinstance(system, list):
        updated = deepcopy(system)
        updated.append({"type": "text", "text": suffix})
        return updated
    base = "" if system is None else str(system)
    return f"{base}\n{suffix}".strip()


def _canonicalize_finite_decimal(value: Decimal) -> str | None:
    if value.is_zero():
        return "0"

    sign, digits, exponent = value.as_tuple()
    digits_text = "".join(str(digit) for digit in digits) or "0"
    prefix = "-" if sign else ""
    canonical_length = _canonical_decimal_length(digits_text, exponent, len(prefix))
    if canonical_length > MAX_CANONICAL_NUMBER_LENGTH:
        return None

    if exponent >= 0:
        return f"{prefix}{digits_text}{'0' * exponent}"

    point_index = len(digits_text) + exponent
    if point_index > 0:
        integer_part = digits_text[:point_index]
        fractional_part = digits_text[point_index:]
    else:
        integer_part = "0"
        fractional_part = ("0" * (-point_index)) + digits_text

    integer_part = integer_part.lstrip("0") or "0"
    fractional_part = fractional_part.rstrip("0")
    if not fractional_part:
        return f"{prefix}{integer_part}"
    return f"{prefix}{integer_part}.{fractional_part}"


def _canonical_decimal_length(digits_text: str, exponent: int, prefix_length: int) -> int:
    digits_length = len(digits_text)
    if exponent >= 0:
        return prefix_length + digits_length + exponent

    point_index = digits_length + exponent
    if point_index > 0:
        fractional_length = len(digits_text[point_index:].rstrip("0"))
        integer_length = max(1, len((digits_text[:point_index]).lstrip("0")))
        if fractional_length == 0:
            return prefix_length + integer_length
        return prefix_length + integer_length + 1 + fractional_length

    return prefix_length + 2 + (-point_index) + digits_length
