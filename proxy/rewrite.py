from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any

from proxy.config import ProxyConfig

STRICT_FORMAT_SUFFIX = "Return only the final answer when the user requests exact output."
PREMISE_BINDING_SUFFIX = (
    "If the prompt says an attribute is distinguishable or can be deliberately selected during the process, "
    "treat that attribute as controllable rather than as a blind random draw. "
    "Reason in separate observable buckets defined by that distinguishable attribute before combining cases. "
    "Choose and evaluate explicit draw quotas across those buckets before applying worst-case hidden-attribute assignments. "
    "Do not collapse the process into a single-bucket extreme unless the prompt explicitly forces that restriction. "
    "For minimum or guarantee questions, reason from the worst-case construction under that binding premise before answering."
)
MINIMUM_GUARANTEE_SUFFIX = (
    "For minimum or guarantee questions, explicitly identify the largest draw count that can still fail the target, "
    "then add one more draw to obtain the guarantee. If the prompt lists several categories, compare the failure case "
    "for each category and keep the maximum failing draw count."
)


@dataclass(slots=True)
class ClassificationResult:
    route: str
    score: int
    replay_safe: bool
    replay_safe_reason: str | None
    effective_prompt_surface: str


@dataclass(slots=True)
class RewriteResult:
    body: dict[str, Any]
    metadata: dict[str, Any]


def is_replay_safe(body: dict[str, Any]) -> tuple[bool, str | None]:
    if body.get("tools"):
        return False, "tooling_declared"
    if body.get("tool_choice") not in (None, "auto"):
        return False, "tooling_declared"
    text = str(body)
    if any(marker in text for marker in ("tool_use", "tool_result")):
        return False, "tool_state_present"
    if _has_side_effect_edit_intent(text):
        return False, "side_effect_intent"
    messages = body.get("messages")
    if not isinstance(messages, list):
        return False, "missing_message_history"
    latest_user_index = _latest_user_index(messages)
    if latest_user_index is None:
        return False, "latest_user_missing"
    for message in messages[latest_user_index + 1 :]:
        if not isinstance(message, dict):
            return False, "unknown_state_present"
        if message.get("role") == "assistant":
            return False, "assistant_state_present"
        return False, "unknown_state_present"
    for message in messages[: latest_user_index + 1]:
        if not isinstance(message, dict):
            return False, "unknown_state_present"
        role = message.get("role")
        if role not in {"user", "assistant"}:
            return False, "unknown_state_present"
        if role == "assistant" and _flatten_message_content(message.get("content")) is None:
            return False, "assistant_state_present"
    return True, None


def extract_effective_prompt_surface(body: dict[str, Any]) -> str:
    system = body.get("system", "")
    if isinstance(system, list):
        system_text = "\n".join(
            text
            for item in system
            if isinstance(item, dict)
            for text in [_flatten_content_block(item)]
            if text
        )
    else:
        system_text = str(system)
    parts = [system_text]
    messages = body.get("messages", [])
    latest_user_index = _latest_user_index(messages) if isinstance(messages, list) else None
    if latest_user_index is None:
        return "\n".join(part for part in parts if part).strip()
    for index, message in enumerate(messages[: latest_user_index + 1]):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        flattened = _flatten_message_content(message.get("content"))
        if not flattened:
            continue
        if role == "assistant":
            parts.append(flattened)
        elif role == "user" and index == latest_user_index:
            parts.append(flattened)
    return "\n".join(part for part in parts if part).strip()


def classify_request(body: dict[str, Any], config: ProxyConfig) -> ClassificationResult:
    replay_safe, reason = is_replay_safe(body)
    surface = extract_effective_prompt_surface(body)
    if not config.classification.enabled:
        return ClassificationResult("passthrough", 0, replay_safe, reason, surface)
    if not replay_safe:
        return ClassificationResult("passthrough", 0, False, reason, surface)
    code_patterns = _compile_patterns(config.classification.code_marker_patterns)
    if any(pattern.search(surface) for pattern in code_patterns):
        return ClassificationResult("passthrough", 0, True, None, surface)
    score = 0
    if _latest_user_text(body.get("messages", [])) is not None:
        score += 1
    if len(surface) >= config.classification.min_chars or surface.count("\n") >= config.classification.min_line_breaks:
        score += 1
    if _matches_patterns(surface, config.classification.reasoning_keyword_patterns):
        score += 1
    if _matches_patterns(surface, config.classification.output_constraint_patterns):
        score += 1
    if _matches_patterns(surface, config.classification.premise_control_patterns):
        score += 1
    if _looks_numeric_reasoning(surface):
        score += 1
    if score >= config.classification.rewrite_score_threshold:
        route = "rewrite"
    elif score >= config.classification.normalize_only_score_threshold:
        route = "normalize_only"
    else:
        route = "passthrough"
    return ClassificationResult(route, score, True, None, surface)


def apply_rewrites(
    body: dict[str, Any],
    config: ProxyConfig,
    capability_flags: dict[str, bool],
) -> RewriteResult:
    rewritten = deepcopy(body)
    applied: list[str] = []
    if not config.rewrite.enabled:
        return RewriteResult(body=rewritten, metadata={"applied_rules": applied})
    if (
        config.rewrite.max_tokens_floor.enabled
        and rewritten.get("max_tokens", 0) < config.rewrite.max_tokens_floor.minimum_output_tokens
    ):
        rewritten["max_tokens"] = config.rewrite.max_tokens_floor.minimum_output_tokens
        applied.append("max_tokens_floor")
    if (
        config.rewrite.explicit_thinking.enabled
        and config.rewrite.explicit_thinking.inject_when_missing
        and capability_flags.get("thinking")
        and "thinking" not in rewritten
    ):
        rewritten["thinking"] = {
            "type": "enabled",
            "budget_tokens": config.rewrite.explicit_thinking.minimum_budget_tokens,
        }
        applied.append("explicit_thinking")
    surface = extract_effective_prompt_surface(rewritten)
    if (
        config.rewrite.premise_binding_guardrail.enabled
        and
        _matches_patterns(surface, config.classification.reasoning_keyword_patterns)
        and _matches_patterns(surface, config.classification.premise_control_patterns)
    ):
        rewritten["system"] = _append_system_suffix(
            rewritten.get("system"),
            PREMISE_BINDING_SUFFIX,
        )
        applied.append("premise_binding_guardrail")
    if (
        config.rewrite.premise_binding_guardrail.enabled
        and _matches_patterns(surface, tuple(MINIMUM_GUARANTEE_SURFACE_MARKERS))
    ):
        rewritten["system"] = _append_system_suffix(
            rewritten.get("system"),
            MINIMUM_GUARANTEE_SUFFIX,
        )
        applied.append("minimum_guarantee_guardrail")
    if (
        config.rewrite.strict_format_guardrail.enabled
        and _matches_patterns(surface, config.classification.output_constraint_patterns)
    ):
        rewritten["system"] = _append_system_suffix(
            rewritten.get("system"),
            STRICT_FORMAT_SUFFIX[: config.rewrite.strict_format_guardrail.max_suffix_chars],
        )
        applied.append("strict_format_guardrail")
    return RewriteResult(body=rewritten, metadata={"applied_rules": applied})


def _latest_user_index(messages: Any) -> int | None:
    if not isinstance(messages, list):
        return None
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "user":
            return index
    return None


def _latest_user_text(messages: Any) -> str | None:
    index = _latest_user_index(messages)
    if index is None or not isinstance(messages, list):
        return None
    message = messages[index]
    if not isinstance(message, dict):
        return None
    return _flatten_message_content(message.get("content"))


def _flatten_message_content(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            return None
        text = _flatten_content_block(item)
        if text is None:
            return None
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _flatten_content_block(block: dict[str, Any]) -> str | None:
    block_type = block.get("type")
    if block_type is None or block_type == "text":
        return str(block.get("text", ""))
    return None


def _compile_patterns(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


def _matches_patterns(surface: str, patterns: list[str]) -> bool:
    return any(pattern.search(surface) for pattern in _compile_patterns(patterns))


def _append_system_suffix(system: Any, suffix: str) -> Any:
    if isinstance(system, list):
        updated = deepcopy(system)
        updated.append({"type": "text", "text": suffix})
        return updated
    base = "" if system is None else str(system)
    return f"{base}\n{suffix}".strip()


MINIMUM_GUARANTEE_SURFACE_MARKERS = ("minimum", "worst case", "guarantee", "最少", "至少", "保证")


def _has_side_effect_edit_intent(text: str) -> bool:
    normalized = " ".join(text.split())
    if re.search(r"\b(apply this patch now|run this command now)\b", normalized, re.IGNORECASE):
        return True
    return bool(
        re.search(
            r"\b(edit|modify|update|rewrite|patch|refactor)\b(?:\s+\w+){0,6}\s+\b(file|code|repo|function|class|module|script|text|prompt)\b",
            normalized,
            re.IGNORECASE,
        )
    )


def _looks_numeric_reasoning(surface: str) -> bool:
    return len(re.findall(r"\d+", surface)) >= 2
