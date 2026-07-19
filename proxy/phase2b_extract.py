from __future__ import annotations

import json
import re

from proxy.schemas import Phase2bExtractedRecord


def strip_json_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def extract_numeric_answer(text: str) -> str | None:
    answer_matches = re.findall(
        r"(?:答案|answer)\s*(?:(?:是|为|is)\s*)?(?:[:：=]\s*)?(\d+)",
        text,
        flags=re.IGNORECASE,
    )
    if answer_matches:
        return answer_matches[-1]
    matches = re.findall(r"(?<!\d)(\d+)(?!\d)", text)
    return matches[-1] if matches else None


def extract_revised_numeric_answer(text: str) -> str | None:
    tagged_patterns = (
        r"(?:revised[_\s-]*answer|revised[_\s-]*candidate[_\s-]*answer)\s*(?::|：|=)\s*(\d+)",
        r"(?:corrected[_\s-]*candidate[_\s-]*answer|active corrected candidate[_\s-]*answer)\s*(?::|：|=)\s*(\d+)",
        r"(?:修正(?:后)?答案|修正后的candidate_answer|修正后candidate_answer)\s*(?::|：|=)\s*(\d+)",
    )
    for pattern in tagged_patterns:
        matches = re.findall(pattern, text, flags=re.IGNORECASE)
        if matches:
            return matches[-1]
    return None


def _extract_fallback_branch_record(cleaned: str) -> Phase2bExtractedRecord:
    return Phase2bExtractedRecord(
        candidate_answer=extract_numeric_answer(cleaned),
        target_interpretation=(
            cleaned if "guarantee" in cleaned.lower() or "保证" in cleaned else None
        ),
        worst_case_claim=cleaned if "worst" in cleaned.lower() or "最坏" in cleaned else None,
        controllable_premises=[
            segment
            for segment in ("shape is controllable", "形状可控", "可控条件", "手感可区分")
            if segment in cleaned
        ],
        survives=None,
        fatal_error_tags=[],
        ledger_conflicts=[],
        revised_answer_if_any=None,
        missing_fields=[],
    )


def extract_branch_record(raw_text: str) -> Phase2bExtractedRecord:
    cleaned = strip_json_fence(raw_text)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        return _extract_fallback_branch_record(cleaned)
    if not isinstance(payload, dict):
        return _extract_fallback_branch_record(cleaned)

    premises = payload.get("controllable_premises", [])
    if isinstance(premises, str):
        premises = [premises]
    elif not isinstance(premises, list):
        premises = []
    return Phase2bExtractedRecord(
        candidate_answer=(
            str(payload.get("candidate_answer"))
            if payload.get("candidate_answer") is not None
            else None
        ),
        target_interpretation=(
            str(payload.get("target_interpretation"))
            if payload.get("target_interpretation") is not None
            else None
        ),
        worst_case_claim=(
            str(payload.get("worst_case_claim"))
            if payload.get("worst_case_claim") is not None
            else None
        ),
        controllable_premises=[
            str(item)
            for item in premises
            if isinstance(item, str)
        ],
        survives=None,
        fatal_error_tags=[],
        ledger_conflicts=[],
        revised_answer_if_any=None,
        missing_fields=[],
    )


def _normalize_fatal_tags(text: str) -> list[str]:
    lowered = text.lower()
    tags: list[str] = []
    if "controllable" in lowered or "可控" in text:
        tags.append("controllable_shape_as_random")
    if "existence" in lowered or "存在" in text:
        tags.append("guarantee_to_existence_swap")
    if "target" in lowered or "目标" in text:
        tags.append("target_condition_misread")
    return tags


def _normalize_attack_tags(text: str) -> tuple[list[str], list[str]]:
    lowered = text.lower()
    fatal_tags: list[str] = []
    ledger_conflicts: list[str] = []
    if "worst-case" in lowered or "worst case" in lowered or "最坏情况" in text:
        fatal_tags.append("invalid_worst_case_construction")
    if (
        "ledger_conflict" in lowered
        or "shape controllable by touch" in lowered
        or "形状可控" in text
    ):
        ledger_conflicts.append("shape_controllable_by_touch")
    return fatal_tags, ledger_conflicts


def _extract_explicit_verdict(raw_text: str) -> bool | None:
    verdict_matches = re.findall(
        r"(?im)^\s*(?:result\s*[:：=]\s*)?(pass|fail|通过|不通过)\b",
        raw_text,
    )
    if not verdict_matches:
        return None
    verdict = verdict_matches[-1].lower()
    return verdict in {"pass", "通过"}


def extract_audit_record(raw_text: str) -> Phase2bExtractedRecord:
    verdict = _extract_explicit_verdict(raw_text)
    if verdict is None:
        lowered = raw_text.lower()
        survives = not any(marker in lowered for marker in ("fail", "不通过", "fatal"))
    else:
        survives = verdict
    return Phase2bExtractedRecord(
        candidate_answer=None,
        target_interpretation=None,
        worst_case_claim=None,
        controllable_premises=[],
        survives=survives,
        fatal_error_tags=[] if survives else _normalize_fatal_tags(raw_text),
        ledger_conflicts=[],
        revised_answer_if_any=extract_revised_numeric_answer(raw_text),
        missing_fields=[],
    )


def extract_attack_record(raw_text: str) -> Phase2bExtractedRecord:
    verdict = _extract_explicit_verdict(raw_text)
    if verdict is None:
        lowered = raw_text.lower()
        survives = not any(marker in lowered for marker in ("fail", "不通过", "fatal"))
    else:
        survives = verdict
    fatal_tags, ledger_conflicts = _normalize_attack_tags(raw_text)
    return Phase2bExtractedRecord(
        candidate_answer=None,
        target_interpretation=None,
        worst_case_claim=None,
        controllable_premises=[],
        survives=survives,
        fatal_error_tags=[] if survives else fatal_tags,
        ledger_conflicts=[] if survives else ledger_conflicts,
        revised_answer_if_any=extract_revised_numeric_answer(raw_text),
        missing_fields=[],
    )
