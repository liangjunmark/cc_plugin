from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any

from proxy.config import ProxyConfig


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
    text = str(body)
    if "tool_use" in text or "tool_result" in text or "tool_choice" in text:
        return False, "tool_state_present"
    if "apply this patch now" in text or "run this command now" in text:
        return False, "side_effect_intent"
    return True, None


def extract_effective_prompt_surface(body: dict[str, Any]) -> str:
    system = body.get("system", "")
    if isinstance(system, list):
        system_text = "\n".join(str(item.get("text", "")) for item in system if isinstance(item, dict))
    else:
        system_text = str(system)
    parts = [system_text]
    for message in body.get("messages", []):
        if message.get("role") in {"assistant", "user"}:
            parts.append(str(message.get("content", "")))
    return "\n".join(part for part in parts if part).strip()


def classify_request(body: dict[str, Any], config: ProxyConfig) -> ClassificationResult:
    replay_safe, reason = is_replay_safe(body)
    surface = extract_effective_prompt_surface(body)
    if not replay_safe:
        return ClassificationResult("passthrough", 0, False, reason, surface)
    code_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in config.classification.code_marker_patterns]
    if any(pattern.search(surface) for pattern in code_patterns):
        return ClassificationResult("passthrough", 0, True, None, surface)
    score = 0
    latest_user_present = any(message.get("role") == "user" for message in body.get("messages", []))
    if latest_user_present:
        score += 1
    if len(surface.encode()) >= config.classification.min_chars or surface.count("\n") >= config.classification.min_line_breaks:
        score += 1
    if any(re.search(pattern, surface, re.IGNORECASE) for pattern in config.classification.reasoning_keyword_patterns):
        score += 1
    if any(re.search(pattern, surface, re.IGNORECASE) for pattern in config.classification.output_constraint_patterns):
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
    if rewritten.get("max_tokens", 0) < config.rewrite.max_tokens_floor.minimum_output_tokens:
        rewritten["max_tokens"] = config.rewrite.max_tokens_floor.minimum_output_tokens
        applied.append("max_tokens_floor")
    if config.rewrite.explicit_thinking.enabled and capability_flags.get("thinking") and "thinking" not in rewritten:
        rewritten["thinking"] = {
            "type": "enabled",
            "budget_tokens": config.rewrite.explicit_thinking.minimum_budget_tokens,
        }
        applied.append("explicit_thinking")
    if config.rewrite.strict_format_guardrail.enabled:
        rewritten["system"] = (
            f"{rewritten.get('system', '')}\n"
            "Return only the final answer when the user requests exact output."
        ).strip()
        applied.append("strict_format_guardrail")
    return RewriteResult(body=rewritten, metadata={"applied_rules": applied})
