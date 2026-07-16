from proxy.rewrite import apply_rewrites, classify_request, is_replay_safe


def test_replay_safe_rejects_tool_use_blocks() -> None:
    body = {"messages": [{"role": "assistant", "content": [{"type": "tool_use", "name": "shell"}]}]}
    replay_safe, reason = is_replay_safe(body)
    assert replay_safe is False
    assert reason == "tool_state_present"


def test_candy_prompt_reaches_rewrite_band(config) -> None:
    body = {
        "system": "You are a careful assistant.",
        "messages": [
            {
                "role": "user",
                "content": "在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状。最少取出多少个糖果才能保证同时拥有不同形状的苹果味和桃子味糖？只输出一个数字。",
            }
        ],
    }
    result = classify_request(body, config)
    assert result.route == "rewrite"
    assert result.score >= config.classification.rewrite_score_threshold


def test_code_marker_forces_passthrough(config) -> None:
    body = {"messages": [{"role": "user", "content": "Read repo file and apply patch, then answer."}]}
    result = classify_request(body, config)
    assert result.route == "passthrough"


def test_apply_rewrites_keeps_thinking_disabled_without_support(config) -> None:
    body = {"messages": [{"role": "user", "content": "只输出一个数字 21"}], "max_tokens": 32}
    result = apply_rewrites(body, config, capability_flags={"thinking": False})
    assert "thinking" not in result.body
    assert result.metadata["applied_rules"] == ["max_tokens_floor", "strict_format_guardrail"]
