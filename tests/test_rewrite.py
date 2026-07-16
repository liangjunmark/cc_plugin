import copy

import pytest

from proxy.rewrite import (
    apply_rewrites,
    classify_request,
    extract_effective_prompt_surface,
    is_replay_safe,
)


def test_replay_safe_rejects_tool_use_blocks() -> None:
    body = {"messages": [{"role": "assistant", "content": [{"type": "tool_use", "name": "shell"}]}]}
    replay_safe, reason = is_replay_safe(body)
    assert replay_safe is False
    assert reason == "tool_state_present"


def test_replay_safe_rejects_assistant_continuation_after_latest_user() -> None:
    body = {
        "messages": [
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "I will now continue the task."},
        ],
    }

    replay_safe, reason = is_replay_safe(body)

    assert replay_safe is False
    assert reason == "assistant_state_present"


def test_replay_safe_rejects_execution_intent_case_insensitively() -> None:
    body = {"messages": [{"role": "user", "content": "Run this command now: rm -rf tmp"}]}

    replay_safe, reason = is_replay_safe(body)

    assert replay_safe is False
    assert reason == "side_effect_intent"


def test_candy_prompt_reaches_rewrite_band(config) -> None:
    body = {
        "system": "You are a careful assistant.",
        "messages": [
            {
                "role": "user",
                "content": (
                    "在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状，不同形状可以靠手感区分。"
                    "已知苹果味、桃子味、西瓜味分别都有圆形与五角星形，并且数量如表所示。"
                    "参赛者必须在活动前决定最少取出多少个糖果，才能保证手中同时拥有不同形状的苹果味和桃子味糖。"
                    "请根据题意进行组合逻辑分析，注意这是保证题而不是概率题。\n"
                    "你必须考虑最坏情况，使用抽屉原理或等价的逻辑保证推导。\n"
                    "只输出一个数字。"
                ),
            }
        ],
    }
    result = classify_request(body, config)
    assert result.route == "rewrite"
    assert result.score >= config.classification.rewrite_score_threshold


def test_extract_effective_prompt_surface_flattens_latest_user_and_prior_assistant_constraints() -> None:
    body = {
        "system": [{"type": "text", "text": "System rule"}],
        "messages": [
            {"role": "assistant", "content": [{"type": "text", "text": "Only answer from the provided facts."}]},
            {"role": "user", "content": [{"type": "text", "text": "Earlier turn"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Ignore me, I am after the earlier user."}]},
            {"role": "user", "content": [{"type": "text", "text": "Latest user turn"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Post-user continuation must not be included."}]},
        ],
    }

    surface = extract_effective_prompt_surface(body)

    assert "System rule" in surface
    assert "Only answer from the provided facts." in surface
    assert "Ignore me, I am after the earlier user." in surface
    assert "Latest user turn" in surface
    assert "Earlier turn" not in surface
    assert "Post-user continuation" not in surface


def test_code_marker_forces_passthrough(config) -> None:
    body = {"messages": [{"role": "user", "content": "Read repo file and apply patch, then answer."}]}
    result = classify_request(body, config)
    assert result.route == "passthrough"


def test_classification_disabled_forces_passthrough(config) -> None:
    config.classification.enabled = False
    body = {"messages": [{"role": "user", "content": "最少取出多少个？只输出一个数字。"}]}

    result = classify_request(body, config)

    assert result.route == "passthrough"
    assert result.score == 0


def test_apply_rewrites_respects_disabled_rewrite_switch(config) -> None:
    body = {"messages": [{"role": "user", "content": "只输出一个数字 21"}], "max_tokens": 32}

    result = apply_rewrites(body, config, capability_flags={"thinking": False})

    assert result.body == body
    assert result.metadata["applied_rules"] == []


def test_apply_rewrites_keeps_thinking_disabled_without_support(config) -> None:
    config.rewrite.enabled = True
    config.rewrite.explicit_thinking.enabled = True
    config.rewrite.explicit_thinking.inject_when_missing = True
    body = {"messages": [{"role": "user", "content": "只输出一个数字 21"}], "max_tokens": 32}
    original = copy.deepcopy(body)

    result = apply_rewrites(body, config, capability_flags={"thinking": False})

    assert "thinking" not in result.body
    assert result.metadata["applied_rules"] == ["max_tokens_floor", "strict_format_guardrail"]
    assert body == original


def test_invalid_regex_config_fails_runtime_validation(tmp_path) -> None:
    from tests.test_config import valid_config_body, write_config
    from proxy.config import load_config, validate_runtime_config

    path = write_config(
        tmp_path,
        valid_config_body().replace('reasoning_keyword_patterns = ["最少", "minimum"]', 'reasoning_keyword_patterns = ["("]'),
    )

    with pytest.raises(ValueError, match="invalid regex"):
        validate_runtime_config(load_config(path))
