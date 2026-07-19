from __future__ import annotations

import json

import pytest

from proxy.rewrite import ClassificationResult
from proxy.upstream import UpstreamResult


class StubTransport:
    def __init__(self, responses: list[UpstreamResult]) -> None:
        self.responses = responses
        self.requests: list[dict[str, object]] = []

    async def send_with_retry(self, **kwargs: object) -> UpstreamResult:
        self.requests.append(kwargs)
        return self.responses.pop(0)


def _message(text: str) -> UpstreamResult:
    payload = {"type": "message", "content": [{"type": "text", "text": text}]}
    return UpstreamResult(200, {}, json.dumps(payload).encode())


def _surviving_branch_responses(final: UpstreamResult) -> list[UpstreamResult]:
    return [
        _message("14"),
        _message('{"candidate_answer":"21"}'),
        _message('{"candidate_answer":"22"}'),
        _message('{"candidate_answer":"23"}'),
        final,
    ]


def _phase2b_config_without_checks(config):
    phase2b_config = config.model_copy(deep=True)
    phase2b_config.phase2b.enabled = True
    phase2b_config.phase2b.enable_assumption_audit = False
    phase2b_config.phase2b.enable_worst_case_attack = False
    phase2b_config.phase2b.allow_tiebreak_round = False
    return phase2b_config


def _phase2b_config_with_audit_only(config):
    phase2b_config = config.model_copy(deep=True)
    phase2b_config.phase2b.enabled = True
    phase2b_config.phase2b.enable_assumption_audit = True
    phase2b_config.phase2b.enable_worst_case_attack = False
    phase2b_config.phase2b.allow_tiebreak_round = False
    return phase2b_config


def _phase2b_body() -> dict[str, object]:
    return {"model": "m", "max_tokens": 4096, "messages": [{"role": "user", "content": "最少 只输出"}]}


def _phase2b_classification() -> ClassificationResult:
    return ClassificationResult("rewrite", 5, True, None, "最少 只输出 保证")


def _candy_boundary_classification() -> ClassificationResult:
    return ClassificationResult(
        "rewrite",
        5,
        True,
        None,
        "在一个黑色的袋子里 形状靠手感可以分辨 苹果味 桃子味 西瓜味 圆形 7 9 8 五角星形 7 6 4 最少 只输出",
    )


def _triple_color_counting_classification() -> ClassificationResult:
    return ClassificationResult(
        "rewrite",
        5,
        True,
        None,
        "一个盒子里有红球8个、蓝球7个、绿球6个。随机摸球（不放回），至少要摸多少个，才能保证红球、蓝球、绿球三种颜色都至少各有2个？只输出最终答案。",
    )


def _triple_color_counting_body() -> dict[str, object]:
    return {
        "model": "m",
        "max_tokens": 4096,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": (
                    "一个盒子里有红球8个、蓝球7个、绿球6个。"
                    "随机摸球（不放回），至少要摸多少个，才能保证红球、蓝球、绿球三种颜色都至少各有2个？"
                    "只输出最终答案。"
                ),
            }
        ],
    }


def _phase2b_config_with_boundary_verifier(config):
    phase2b_config = _phase2b_config_with_audit_only(config)
    phase2b_config.upstream.base_url = "https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic"
    phase2b_config.phase2b.boundary_verifier.enabled = True
    phase2b_config.phase2b.boundary_verifier.lower_bound = 20
    phase2b_config.phase2b.boundary_verifier.upper_bound = 21
    phase2b_config.phase2b.boundary_verifier.trigger_markers = [
        "黑色的袋子",
        "手感可以分辨",
        "苹果味",
        "桃子味",
        "西瓜味",
        "圆形 7 9 8",
        "五角星形 7 6 4",
    ]
    phase2b_config.phase2b.max_total_upstream_calls = 14
    return phase2b_config


@pytest.mark.asyncio
async def test_run_phase2b_returns_baseline_when_all_branches_fail(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = config.model_copy(deep=True)
    phase2b_config.phase2b.enabled = True
    classification = ClassificationResult("rewrite", 5, True, None, "最少 只输出 手感 保证")
    responses = [
        UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"text","text":"14"}]}'),
        UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"text","text":"bad branch"}]}'),
        UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"text","text":"bad branch"}]}'),
        UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"text","text":"bad branch"}]}'),
    ]
    transport = StubTransport(responses)

    result = await run_phase2b(
        transport=transport,
        headers={},
        body={"model": "m", "max_tokens": 4096, "stream": False, "messages": [{"role": "user", "content": "最少 只输出"}]},
        classification=classification,
        config=phase2b_config,
        request_id="req-1",
    )

    assert result.mode == "fallback_phase1"
    assert result.fallback_reason == "no_surviving_branches"


@pytest.mark.asyncio
async def test_run_phase2b_uses_configured_boundary_verifier_fast_path_when_it_returns_upper_bound(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_with_boundary_verifier(config)
    transport = StubTransport([
        _message("14"),
        _message("N20_FAILS: YES\nN21_FAILS: NO\nFINAL_ANSWER: 21"),
    ])

    result = await run_phase2b(
        transport=transport,
        headers={},
        body=_phase2b_body(),
        classification=_candy_boundary_classification(),
        config=phase2b_config,
        request_id="req-boundary-fast-path",
    )

    assert result.mode == "phase2b"
    assert result.branch_decisions == []
    assert result.downstream_payload["content"] == [{"type": "text", "text": "21"}]
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_run_phase2b_uses_boundary_verifier_fast_path_with_brief_explanation_for_non_exact_output(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_with_boundary_verifier(config)
    transport = StubTransport([
        _message("14"),
        _message("N20_FAILS: YES\nN21_FAILS: NO\nFINAL_ANSWER: 21"),
        _message("因为 20 仍可构造失败，而 21 已能保证成功，所以最少取 21 个。"),
    ])
    body = {
        "model": "m",
        "max_tokens": 4096,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": (
                    "在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。"
                    "现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果"
                    "才能保证手中同时拥有不同形状的苹果味和桃子味的糖？请简要说明关键理由，并给出最终答案。"
                ),
            }
        ],
    }
    classification = ClassificationResult(
        "rewrite",
        5,
        True,
        None,
        "在一个黑色的袋子里 形状靠手感可以分辨 苹果味 桃子味 西瓜味 圆形 7 9 8 五角星形 7 6 4 最少 请简要说明关键理由并给出最终答案",
    )

    result = await run_phase2b(
        transport=transport,
        headers={},
        body=body,
        classification=classification,
        config=phase2b_config,
        request_id="req-boundary-fast-path-brief",
    )

    assert result.mode == "phase2b"
    assert result.downstream_payload["content"] == [
        {"type": "text", "text": "因为 20 仍可构造失败，而 21 已能保证成功，所以最少取 21 个。"}
    ]
    assert len(transport.requests) == 3


@pytest.mark.asyncio
async def test_run_phase2b_uses_counting_guarantee_fast_path_for_category_guarantee_prompt(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_without_checks(config)
    transport = StubTransport([
        _message("13"),
    ])

    result = await run_phase2b(
        transport=transport,
        headers={},
        body=_triple_color_counting_body(),
        classification=_triple_color_counting_classification(),
        config=phase2b_config,
        request_id="req-counting-fast-path",
    )

    assert result.mode == "phase2b"
    assert result.downstream_payload["content"] == [{"type": "text", "text": "17"}]
    assert result.branch_decisions == []
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_run_phase2b_uses_counting_fast_path_with_brief_explanation_for_non_exact_output(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_without_checks(config)
    transport = StubTransport([
        _message("13"),
        _message("最坏情况是先把另外两类尽量摸完，再补足最少需要的那一类，因此答案是 17。"),
    ])
    body = {
        "model": "m",
        "max_tokens": 4096,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": (
                    "一个盒子里有红球8个、蓝球7个、绿球6个。随机摸球（不放回），至少要摸多少个，才能保证红球、蓝球、绿球三种颜色都至少各有2个？"
                    "请简要说明关键理由，并给出最终答案。"
                ),
            }
        ],
    }
    classification = ClassificationResult(
        "rewrite",
        5,
        True,
        None,
        "一个盒子里有红球8个 蓝球7个 绿球6个 至少要摸多少个 才能保证红球 蓝球 绿球三种颜色都至少各有2个 请简要说明关键理由并给出最终答案",
    )

    result = await run_phase2b(
        transport=transport,
        headers={},
        body=body,
        classification=classification,
        config=phase2b_config,
        request_id="req-counting-fast-path-brief",
    )

    assert result.mode == "phase2b"
    assert result.downstream_payload["content"] == [
        {"type": "text", "text": "最坏情况是先把另外两类尽量摸完，再补足最少需要的那一类，因此答案是 17。"}
    ]
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_run_phase2b_preserves_brief_reasoning_when_original_prompt_is_not_exact_output(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_without_checks(config)
    transport = StubTransport([
        _message("14"),
        _message('{"candidate_answer":"21"}'),
        _message("bad branch"),
        _message("bad branch"),
        _message("因为 20 仍可能失败，而 21 已经可以保证，所以最少是 21。"),
    ])
    body = {
        "model": "m",
        "max_tokens": 4096,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": "这是一个需要最少保证数量的问题。请简要说明关键理由，并给出最终答案。",
            }
        ],
    }
    classification = ClassificationResult(
        "rewrite",
        5,
        True,
        None,
        "这是一个需要最少保证数量的问题 请简要说明关键理由 并给出最终答案",
    )

    result = await run_phase2b(
        transport=transport,
        headers={},
        body=body,
        classification=classification,
        config=phase2b_config,
        request_id="req-brief-reasoning",
    )

    assert result.mode == "phase2b"
    assert result.downstream_payload["content"] == [
        {"type": "text", "text": "因为 20 仍可能失败，而 21 已经可以保证，所以最少是 21。"}
    ]


def test_counting_guarantee_parser_ignores_required_count_clause() -> None:
    from proxy.phase2b import _extract_category_counts

    surface = (
        "一个盒子里有红球8个、蓝球7个、绿球6个。随机摸球（不放回），至少要摸多少个，"
        "才能保证红球、蓝球、绿球三种颜色都至少各有2个？只输出最终答案。"
    )

    assert _extract_category_counts(surface) == [8, 7, 6]


@pytest.mark.asyncio
async def test_run_phase2b_falls_through_when_configured_boundary_verifier_text_is_unusable(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_without_checks(config)
    phase2b_config.upstream.base_url = "https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic"
    phase2b_config.phase2b.boundary_verifier.enabled = True
    phase2b_config.phase2b.boundary_verifier.lower_bound = 20
    phase2b_config.phase2b.boundary_verifier.upper_bound = 21
    phase2b_config.phase2b.boundary_verifier.trigger_markers = [
        "黑色的袋子",
        "手感可以分辨",
        "苹果味",
        "桃子味",
        "西瓜味",
        "圆形 7 9 8",
        "五角星形 7 6 4",
    ]
    phase2b_config.phase2b.max_total_upstream_calls = 14
    transport = StubTransport([
        _message("14"),
        _message("I am not sure."),
        _message('{"candidate_answer":"21"}'),
        _message("bad branch"),
        _message("bad branch"),
        _message("21"),
    ])

    result = await run_phase2b(
        transport=transport,
        headers={},
        body=_phase2b_body(),
        classification=_candy_boundary_classification(),
        config=phase2b_config,
        request_id="req-boundary-fallback",
    )

    assert result.mode == "phase2b"
    assert result.branch_decisions[0].chosen_answer == "21"
    assert result.downstream_payload["content"] == [{"type": "text", "text": "21"}]
    assert len(transport.requests) == 6


@pytest.mark.asyncio
async def test_run_phase2b_skips_boundary_verifier_when_upstream_is_not_xfyun(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_without_checks(config)
    phase2b_config.phase2b.boundary_verifier.enabled = True
    phase2b_config.phase2b.boundary_verifier.lower_bound = 20
    phase2b_config.phase2b.boundary_verifier.upper_bound = 21
    phase2b_config.phase2b.boundary_verifier.trigger_markers = [
        "黑色的袋子",
        "手感可以分辨",
        "苹果味",
        "桃子味",
        "西瓜味",
        "圆形 7 9 8",
        "五角星形 7 6 4",
    ]
    phase2b_config.phase2b.max_total_upstream_calls = 14
    transport = StubTransport([
        _message("14"),
        _message('{"candidate_answer":"21"}'),
        _message("bad branch"),
        _message("bad branch"),
        _message("21"),
    ])

    result = await run_phase2b(
        transport=transport,
        headers={},
        body=_phase2b_body(),
        classification=_candy_boundary_classification(),
        config=phase2b_config,
        request_id="req-boundary-non-xfyun",
    )

    assert result.mode == "phase2b"
    assert result.downstream_payload["content"] == [{"type": "text", "text": "21"}]
    assert len(transport.requests) == 5


@pytest.mark.asyncio
async def test_run_phase2b_falls_through_when_boundary_verifier_verdicts_contradict_the_final_answer(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_without_checks(config)
    phase2b_config.upstream.base_url = "https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic"
    phase2b_config.phase2b.boundary_verifier.enabled = True
    phase2b_config.phase2b.boundary_verifier.lower_bound = 20
    phase2b_config.phase2b.boundary_verifier.upper_bound = 21
    phase2b_config.phase2b.boundary_verifier.trigger_markers = [
        "黑色的袋子",
        "手感可以分辨",
        "苹果味",
        "桃子味",
        "西瓜味",
        "圆形 7 9 8",
        "五角星形 7 6 4",
    ]
    phase2b_config.phase2b.max_total_upstream_calls = 14
    transport = StubTransport([
        _message("14"),
        _message("N20_FAILS: NO\nN21_FAILS: YES\nFINAL_ANSWER: 21"),
        _message('{"candidate_answer":"21"}'),
        _message("bad branch"),
        _message("bad branch"),
        _message("21"),
    ])

    result = await run_phase2b(
        transport=transport,
        headers={},
        body=_phase2b_body(),
        classification=_candy_boundary_classification(),
        config=phase2b_config,
        request_id="req-boundary-contradiction",
    )

    assert result.mode == "phase2b"
    assert result.branch_decisions[0].chosen_answer == "21"
    assert result.downstream_payload["content"] == [{"type": "text", "text": "21"}]
    assert len(transport.requests) == 6


@pytest.mark.asyncio
async def test_run_phase2b_uses_configured_boundary_verifier_bounds_and_markers(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_without_checks(config)
    phase2b_config.upstream.base_url = "https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic"
    phase2b_config.phase2b.boundary_verifier.enabled = True
    phase2b_config.phase2b.boundary_verifier.lower_bound = 10
    phase2b_config.phase2b.boundary_verifier.upper_bound = 11
    phase2b_config.phase2b.boundary_verifier.trigger_markers = ["marker-a", "marker-b"]
    phase2b_config.phase2b.max_total_upstream_calls = 14
    transport = StubTransport([
        _message("14"),
        _message("N10_FAILS: YES\nN11_FAILS: NO\nFINAL_ANSWER: 11"),
    ])
    classification = ClassificationResult("rewrite", 5, True, None, "marker-a marker-b 最少 只输出")

    result = await run_phase2b(
        transport=transport,
        headers={},
        body=_phase2b_body(),
        classification=classification,
        config=phase2b_config,
        request_id="req-boundary-configurable",
    )

    assert result.mode == "phase2b"
    assert result.downstream_payload["content"] == [{"type": "text", "text": "11"}]
    boundary_request = transport.requests[1]["body"]["messages"][0]["content"]
    assert "10 和 11" in boundary_request


def test_is_phase2b_eligible_rejects_non_exact_output_request(config) -> None:
    from proxy.phase2b import is_phase2b_eligible

    phase2b_config = config.model_copy(deep=True)
    phase2b_config.phase2b.enabled = True
    body = {"model": "m", "max_tokens": 4096, "messages": [{"role": "user", "content": "Find the minimum and explain why."}]}
    classification = ClassificationResult("rewrite", 5, True, None, "Find the minimum and explain why.")

    assert is_phase2b_eligible(body, classification, phase2b_config) is False


def test_is_phase2b_eligible_rejects_multimodal_user_content(config) -> None:
    from proxy.phase2b import is_phase2b_eligible

    phase2b_config = config.model_copy(deep=True)
    phase2b_config.phase2b.enabled = True
    body = {
        "model": "m",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Find the minimum and only output it."}, {"type": "image", "source": {"type": "base64"}}]}],
    }
    classification = ClassificationResult("rewrite", 5, True, None, "Find the minimum and only output it.")

    assert is_phase2b_eligible(body, classification, phase2b_config) is False


def test_is_phase2b_eligible_accepts_counting_guarantee_prompt_family(config) -> None:
    from proxy.phase2b import is_phase2b_eligible

    assert is_phase2b_eligible(
        _triple_color_counting_body(),
        _triple_color_counting_classification(),
        _phase2b_config_without_checks(config),
    ) is True


def test_is_phase2b_eligible_accepts_boundary_verifier_prompt_family(config) -> None:
    from proxy.phase2b import is_phase2b_eligible

    assert is_phase2b_eligible(
        _phase2b_body(),
        _candy_boundary_classification(),
        _phase2b_config_with_boundary_verifier(config),
    ) is True


def test_is_phase2b_eligible_accepts_boundary_verifier_prompt_family_with_brief_explanation_request(config) -> None:
    from proxy.phase2b import is_phase2b_eligible

    body = {
        "model": "m",
        "max_tokens": 4096,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": (
                    "在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。"
                    "现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果"
                    "才能保证手中同时拥有不同形状的苹果味和桃子味的糖？\n苹果味 桃子味 西瓜味\n圆形 7 9 8\n五角星形 7 6 4\n"
                    "请简要说明关键理由，并给出最终答案。"
                ),
            }
        ],
    }
    classification = ClassificationResult(
        "rewrite",
        5,
        True,
        None,
        "在一个黑色的袋子里 形状靠手感可以分辨 苹果味 桃子味 西瓜味 圆形 7 9 8 五角星形 7 6 4 最少 请简要说明关键理由并给出最终答案",
    )

    assert is_phase2b_eligible(
        body,
        classification,
        _phase2b_config_with_boundary_verifier(config),
    ) is True


def test_is_phase2b_eligible_rejects_unvalidated_modulo_exact_output_prompt(config) -> None:
    from proxy.phase2b import is_phase2b_eligible

    body = {
        "model": "m",
        "max_tokens": 4096,
        "stream": False,
        "messages": [{"role": "user", "content": "某个正整数除以 4 余 3，除以 5 余 4，除以 6 余 5。满足条件的最小正整数是多少？只输出最终答案。"}],
    }
    classification = ClassificationResult(
        "rewrite",
        5,
        True,
        None,
        "某个正整数除以 4 余 3，除以 5 余 4，除以 6 余 5。满足条件的最小正整数是多少？只输出最终答案。",
    )

    assert is_phase2b_eligible(body, classification, _phase2b_config_without_checks(config)) is False


def test_is_phase2b_eligible_rejects_digit_swap_until_it_is_revalidated(config) -> None:
    from proxy.phase2b import is_phase2b_eligible

    body = {
        "model": "m",
        "max_tokens": 4096,
        "stream": False,
        "messages": [{"role": "user", "content": "一个两位数，交换十位和个位后得到的新数比原数大 27，并且原数的两个数字之和是 11。只输出最终答案。"}],
    }
    classification = ClassificationResult(
        "rewrite",
        5,
        True,
        None,
        "一个两位数，交换十位和个位后得到的新数比原数大 27，并且原数的两个数字之和是 11。只输出最终答案。",
    )

    assert is_phase2b_eligible(body, classification, _phase2b_config_without_checks(config)) is False


def test_is_phase2b_eligible_rejects_unneeded_door_toggle_prompt(config) -> None:
    from proxy.phase2b import is_phase2b_eligible

    body = {
        "model": "m",
        "max_tokens": 4096,
        "stream": False,
        "messages": [{"role": "user", "content": "有 100 扇门，开始时都关着。第 1 轮把所有门切换一次，第 2 轮把编号为 2 的倍数的门切换一次，第 3 轮把编号为 3 的倍数的门切换一次，依此类推直到第 100 轮。最后开着的门有多少扇？只输出最终答案。"}],
    }
    classification = ClassificationResult(
        "rewrite",
        5,
        True,
        None,
        "有 100 扇门，开始时都关着。第 1 轮把所有门切换一次，第 2 轮把编号为 2 的倍数的门切换一次，第 3 轮把编号为 3 的倍数的门切换一次，依此类推直到第 100 轮。最后开着的门有多少扇？只输出最终答案。",
    )

    assert is_phase2b_eligible(body, classification, _phase2b_config_without_checks(config)) is False


def test_build_constraint_ledger_tracks_guarantee_touch_and_fruit_constraints() -> None:
    from proxy.phase2b import build_constraint_ledger

    assert build_constraint_ledger("保证苹果和桃子可以靠手感区分形状") == [
        "target is guarantee, not existence",
        "shape is controllable by touch",
        "success condition requires apple and peach across opposite shapes",
    ]


def test_build_constraint_ledger_skips_guarantee_marker_for_deterministic_exact_output_task() -> None:
    from proxy.phase2b import build_constraint_ledger

    assert build_constraint_ledger("有 100 扇门 最后开着的门有多少扇 只输出最终答案") == []


def test_prompt_builders_preserve_request_and_add_internal_instructions() -> None:
    from proxy.phase2b import (
        build_assumption_audit_prompt,
        build_branch_prompt,
        build_final_compressor_prompt,
        build_worst_case_attack_prompt,
    )

    body = {
        "model": "m",
        "stream": True,
        "system": "Original system.",
        "messages": [{"role": "user", "content": "Find the minimum."}],
    }
    branch = build_branch_prompt(body, "premise_first")
    audit = build_assumption_audit_prompt(body, "candidate_answer: 21", ["target is guarantee, not existence"])
    attack = build_worst_case_attack_prompt(body, "candidate_answer: 21", ["target is guarantee, not existence"])
    final = build_final_compressor_prompt(body, "21", exact_output=True)

    assert body["stream"] is True
    assert branch["stream"] is False
    assert "Internal branch family: premise_first." in branch["system"]
    assert audit["system"] == "You are an internal assumption auditor. Do not solve from scratch."
    assert "Check only for premise or target mistakes." in audit["messages"][0]["content"]
    assert attack["system"] == "You are an internal worst-case attacker. Do not solve from scratch unless the branch fails."
    assert "Attack only the branch's worst-case construction." in attack["messages"][0]["content"]
    assert final["stream"] is False
    assert "Return only the final answer" in final["system"]


def test_prompt_builders_omit_empty_ledger_section() -> None:
    from proxy.phase2b import build_assumption_audit_prompt, build_worst_case_attack_prompt

    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "有 100 扇门，最后开着的门有多少扇？"}],
    }

    audit = build_assumption_audit_prompt(body, "candidate_answer: 10", [])
    attack = build_worst_case_attack_prompt(body, "candidate_answer: 10", [])

    assert "\n\nLedger:\n" not in audit["messages"][0]["content"]
    assert "\n\nLedger:\n" not in attack["messages"][0]["content"]


@pytest.mark.asyncio
async def test_run_phase2b_without_tiebreak_finalizes_first_survivor_deterministically(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = config.model_copy(deep=True)
    phase2b_config.phase2b.enabled = True
    phase2b_config.phase2b.enable_assumption_audit = False
    phase2b_config.phase2b.enable_worst_case_attack = False
    phase2b_config.phase2b.allow_tiebreak_round = False
    transport = StubTransport([_message("14"), _message('{"candidate_answer":"21"}'), _message('{"candidate_answer":"22"}'), _message('{"candidate_answer":"23"}'), _message("21")])

    result = await run_phase2b(
        transport=transport,
        headers={},
        body={"model": "m", "max_tokens": 4096, "messages": [{"role": "user", "content": "最少 只输出"}]},
        classification=ClassificationResult("rewrite", 5, True, None, "最少 只输出 保证"),
        config=phase2b_config,
        request_id="req-success",
    )

    assert result.mode == "phase2b"
    assert result.downstream_payload == json.loads(_message("21").body)
    assert [decision.survived for decision in result.branch_decisions] == [True, True, True]
    assert "candidate_answer\":\"21" in transport.requests[-1]["body"]["system"]


@pytest.mark.asyncio
async def test_run_phase2b_does_not_early_exit_when_only_premise_first_matches_bad_baseline(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_with_audit_only(config)
    transport = StubTransport([
        _message("83"),
        _message('{"candidate_answer":"83"}'),
        _message("RESULT: fail\nFATAL: target mistake"),
        _message('{"candidate_answer":"47"}'),
        _message("PASS"),
        _message("bad branch"),
        _message("47"),
    ])

    result = await run_phase2b(
        transport=transport,
        headers={},
        body=_phase2b_body(),
        classification=_phase2b_classification(),
        config=phase2b_config,
        request_id="req-premise-first-baseline-regression",
    )

    assert result.mode == "phase2b"
    assert result.downstream_payload == json.loads(_message("47").body)
    assert len(transport.requests) == 7


@pytest.mark.asyncio
async def test_run_phase2b_returns_compact_baseline_early_when_independent_branch_matches_it(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_without_checks(config)
    transport = StubTransport([
        _message("59"),
        _message('{"candidate_answer":"29"}'),
        _message('{"candidate_answer":"59"}'),
    ])

    result = await run_phase2b(
        transport=transport,
        headers={},
        body=_phase2b_body(),
        classification=_phase2b_classification(),
        config=phase2b_config,
        request_id="req-baseline-early-exit",
    )

    assert result.mode == "phase2b"
    assert result.downstream_payload == json.loads(_message("59").body)
    assert len(transport.requests) == 3


@pytest.mark.asyncio
async def test_run_phase2b_keeps_compact_nonledger_numeric_branches_for_majority_selection(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = config.model_copy(deep=True)
    phase2b_config.phase2b.enabled = True
    phase2b_config.phase2b.allow_tiebreak_round = True
    transport = StubTransport([
        _message("83"),
        _message("83"),
        _message("47"),
        _message("47"),
        _message("47"),
    ])
    body = {
        "model": "m",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": "一个两位数，交换十位和个位后得到的新数比原数大 27，并且原数的两个数字之和是 11。原数是多少？只输出最终答案。"}],
    }
    classification = ClassificationResult("rewrite", 5, True, None, "一个两位数 交换十位和个位 只输出最终答案")

    result = await run_phase2b(
        transport=transport,
        headers={},
        body=body,
        classification=classification,
        config=phase2b_config,
        request_id="req-compact-numeric-branches",
    )

    assert result.mode == "phase2b"
    assert result.downstream_payload == json.loads(_message("47").body)
    assert len(transport.requests) == 4


@pytest.mark.asyncio
async def test_run_phase2b_skips_all_validation_for_compact_nonledger_baseline_fast_path(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = config.model_copy(deep=True)
    phase2b_config.phase2b.enabled = True
    transport = StubTransport([
        _message("10"),
        _message("10"),
        _message("10"),
    ])
    body = {
        "model": "m",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": "有 100 扇门，开始时都关着。最后开着的门有多少扇？只输出最终答案。"}],
    }
    classification = ClassificationResult("rewrite", 5, True, None, "有 100 扇门 最后开着的门有多少扇 只输出最终答案")

    result = await run_phase2b(
        transport=transport,
        headers={},
        body=body,
        classification=classification,
        config=phase2b_config,
        request_id="req-compact-fast-path-with-validation-enabled",
    )

    assert result.mode == "phase2b"
    assert result.downstream_payload == json.loads(_message("10").body)
    assert len(transport.requests) == 3


@pytest.mark.asyncio
async def test_run_phase2b_prefers_majority_survivor_answer_without_tiebreak_or_final_compressor(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_without_checks(config)
    phase2b_config.phase2b.allow_tiebreak_round = True
    transport = StubTransport([
        _message("14"),
        _message("83"),
        _message("47"),
        _message("47"),
        _message("47"),
    ])

    result = await run_phase2b(
        transport=transport,
        headers={},
        body=_phase2b_body(),
        classification=_phase2b_classification(),
        config=phase2b_config,
        request_id="req-majority-without-tiebreak",
    )

    assert result.mode == "phase2b"
    assert result.downstream_payload == json.loads(_message("47").body)
    assert len(transport.requests) == 4


@pytest.mark.asyncio
async def test_run_phase2b_does_not_early_exit_on_noncompact_baseline_even_if_a_branch_matches(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_without_checks(config)
    transport = StubTransport([
        _message("The answer is 59"),
        _message('{"candidate_answer":"59"}'),
        _message("bad branch"),
        _message("bad branch"),
        _message("59"),
    ])

    result = await run_phase2b(
        transport=transport,
        headers={},
        body=_phase2b_body(),
        classification=_phase2b_classification(),
        config=phase2b_config,
        request_id="req-noncompact-baseline",
    )

    assert result.mode == "phase2b"
    assert result.downstream_payload == json.loads(_message("59").body)
    assert len(transport.requests) == 5


@pytest.mark.asyncio
async def test_run_phase2b_uses_valid_tiebreak_branch_id(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = config.model_copy(deep=True)
    phase2b_config.phase2b.enabled = True
    phase2b_config.phase2b.enable_assumption_audit = False
    phase2b_config.phase2b.enable_worst_case_attack = False
    transport = StubTransport([
        _message("14"),
        _message('{"candidate_answer":"21"}'),
        _message('{"candidate_answer":"22"}'),
        _message('{"candidate_answer":"23"}'),
        _message("quota_first"),
        _message("22"),
    ])

    result = await run_phase2b(
        transport=transport,
        headers={},
        body={"model": "m", "max_tokens": 4096, "messages": [{"role": "user", "content": "\u6700\u5c11 \u53ea\u8f93\u51fa"}]},
        classification=ClassificationResult("rewrite", 5, True, None, "\u6700\u5c11 \u53ea\u8f93\u51fa \u4fdd\u8bc1"),
        config=phase2b_config,
        request_id="req-tiebreak",
    )

    assert result.mode == "phase2b"
    assert result.downstream_payload == json.loads(_message("22").body)
    assert "candidate_answer\":\"22" in transport.requests[-1]["body"]["system"]


@pytest.mark.asyncio
async def test_run_phase2b_falls_back_when_tiebreak_output_is_not_a_survivor(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = config.model_copy(deep=True)
    phase2b_config.phase2b.enabled = True
    phase2b_config.phase2b.enable_assumption_audit = False
    phase2b_config.phase2b.enable_worst_case_attack = False
    transport = StubTransport([
        _message("14"),
        _message('{"candidate_answer":"21"}'),
        _message('{"candidate_answer":"22"}'),
        _message('{"candidate_answer":"23"}'),
        _message("not-a-branch-id"),
    ])

    result = await run_phase2b(
        transport=transport,
        headers={},
        body={"model": "m", "max_tokens": 4096, "messages": [{"role": "user", "content": "\u6700\u5c11 \u53ea\u8f93\u51fa"}]},
        classification=ClassificationResult("rewrite", 5, True, None, "\u6700\u5c11 \u53ea\u8f93\u51fa \u4fdd\u8bc1"),
        config=phase2b_config,
        request_id="req-invalid-tiebreak",
    )

    assert result.mode == "fallback_phase1"
    assert result.fallback_reason == "tiebreak_failed"


@pytest.mark.asyncio
async def test_run_phase2b_falls_back_when_final_compressor_fails(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = config.model_copy(deep=True)
    phase2b_config.phase2b.enabled = True
    phase2b_config.phase2b.enable_assumption_audit = False
    phase2b_config.phase2b.enable_worst_case_attack = False
    phase2b_config.phase2b.allow_tiebreak_round = False
    transport = StubTransport([_message("14"), _message('{"candidate_answer":"21"}'), _message('{"candidate_answer":"21"}'), _message('{"candidate_answer":"21"}'), UpstreamResult(500, {}, b"failure")])

    result = await run_phase2b(
        transport=transport,
        headers={},
        body={"model": "m", "max_tokens": 4096, "messages": [{"role": "user", "content": "最少 只输出"}]},
        classification=ClassificationResult("rewrite", 5, True, None, "最少 只输出 保证"),
        config=phase2b_config,
        request_id="req-final-failure",
    )

    assert result.mode == "fallback_phase1"
    assert result.fallback_reason == "final_compression_failed"


@pytest.mark.asyncio
async def test_run_phase2b_normalizes_prose_numeric_candidate_before_final_binding(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_without_checks(config)
    transport = StubTransport([
        _message("14"),
        _message('{"candidate_answer":"The answer is 21"}'),
        _message("bad branch"),
        _message("bad branch"),
        _message("21"),
    ])

    result = await run_phase2b(
        transport=transport,
        headers={},
        body=_phase2b_body(),
        classification=_phase2b_classification(),
        config=phase2b_config,
        request_id="req-normalized-candidate",
    )

    assert result.mode == "phase2b"
    assert result.branch_decisions[0].chosen_answer == "21"
    assert result.downstream_payload == json.loads(_message("21").body)


@pytest.mark.asyncio
async def test_run_phase2b_salvages_revised_answer_from_failed_assumption_audit(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_with_audit_only(config)
    transport = StubTransport([
        _message("14"),
        _message('{"candidate_answer":"16"}'),
        _message("FAIL\nfatal_tag: target_mistake\nrevised_answer: 21"),
        _message("bad branch"),
        _message("bad branch"),
        _message("21"),
    ])

    result = await run_phase2b(
        transport=transport,
        headers={},
        body=_phase2b_body(),
        classification=_phase2b_classification(),
        config=phase2b_config,
        request_id="req-audit-salvage",
    )

    assert result.mode == "phase2b"
    assert result.branch_decisions[0].survived is True
    assert result.branch_decisions[0].chosen_answer == "21"
    assert result.branch_decisions[0].reason == "salvaged_from_assumption_audit"
    assert result.downstream_payload == json.loads(_message("21").body)


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_answer", ["The minimum is twenty-one", "答案为二十一", "twenty-one"])
async def test_run_phase2b_rejects_prose_nonnumeric_candidate_from_survivor_binding(config, candidate_answer: str) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_without_checks(config)
    result = await run_phase2b(
        transport=StubTransport([
            _message("14"),
            _message(json.dumps({"candidate_answer": candidate_answer})),
            _message("bad branch"),
            _message("bad branch"),
        ]),
        headers={},
        body=_phase2b_body(),
        classification=_phase2b_classification(),
        config=phase2b_config,
        request_id="req-prose-candidate",
    )

    assert result.mode == "fallback_phase1"
    assert result.fallback_reason == "no_surviving_branches"
    assert all(decision.reason == "branch_generation_failed" for decision in result.branch_decisions)


@pytest.mark.asyncio
@pytest.mark.parametrize("final_text", [" 21 ", "21\n"])
async def test_run_phase2b_canonicalizes_accepted_final_text(config, final_text: str) -> None:
    from proxy.phase2b import run_phase2b

    result = await run_phase2b(
        transport=StubTransport(_surviving_branch_responses(_message(final_text))),
        headers={},
        body=_phase2b_body(),
        classification=_phase2b_classification(),
        config=_phase2b_config_without_checks(config),
        request_id="req-final-padding",
    )

    assert result.mode == "phase2b"
    assert result.downstream_payload["content"] == [{"type": "text", "text": "21"}]


@pytest.mark.asyncio
async def test_run_phase2b_rejects_tool_use_final_compressor_payload(config) -> None:
    from proxy.phase2b import run_phase2b

    final = UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"tool_use","id":"tool-1","name":"shell","input":{}}]}')
    result = await run_phase2b(
        transport=StubTransport(_surviving_branch_responses(final)),
        headers={},
        body=_phase2b_body(),
        classification=_phase2b_classification(),
        config=_phase2b_config_without_checks(config),
        request_id="req-tool-final",
    )

    assert result.mode == "fallback_phase1"
    assert result.fallback_reason == "final_compression_failed"


@pytest.mark.asyncio
async def test_run_phase2b_rejects_internal_artifact_final_compressor_text(config) -> None:
    from proxy.phase2b import run_phase2b

    result = await run_phase2b(
        transport=StubTransport(_surviving_branch_responses(_message("Original task: candidate_answer: 21"))),
        headers={},
        body=_phase2b_body(),
        classification=_phase2b_classification(),
        config=_phase2b_config_without_checks(config),
        request_id="req-artifact-final",
    )

    assert result.mode == "fallback_phase1"
    assert result.fallback_reason == "final_compression_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "final_text",
    [
        "controllable_premises: shape is controllable by touch",
        "The branch concludes 21 because the premise is controllable.",
        "21 because controllable",
        "The answer is 21",
        "答案是21",
        "21, 22",
    ],
)
async def test_run_phase2b_rejects_non_answer_shaped_final_compressor_text(config, final_text: str) -> None:
    from proxy.phase2b import run_phase2b

    result = await run_phase2b(
        transport=StubTransport(_surviving_branch_responses(_message(final_text))),
        headers={},
        body=_phase2b_body(),
        classification=_phase2b_classification(),
        config=_phase2b_config_without_checks(config),
        request_id="req-structural-final",
    )

    assert result.mode == "fallback_phase1"
    assert result.fallback_reason == "final_compression_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), RuntimeError("transport failed")])
async def test_run_phase2b_returns_error_envelope_when_baseline_call_cannot_be_retained(config, failure: Exception) -> None:
    from proxy.phase2b import run_phase2b

    class FailingTransport:
        async def send_with_retry(self, **kwargs: object) -> UpstreamResult:
            raise failure

    result = await run_phase2b(
        transport=FailingTransport(),
        headers={},
        body=_phase2b_body(),
        classification=_phase2b_classification(),
        config=_phase2b_config_without_checks(config),
        request_id="req-baseline-error",
    )

    assert result.mode == "phase2b_error"
    assert result.fallback_reason in {"phase2b_timeout", "orchestration_failed"}
    assert json.loads(result.baseline_upstream.body) == {
        "type": "error",
        "error": {"type": "api_error", "message": result.fallback_reason},
    }


@pytest.mark.asyncio
async def test_run_phase2b_wraps_a_599_baseline_transport_result_in_an_error_envelope(config) -> None:
    from proxy.phase2b import run_phase2b

    result = await run_phase2b(
        transport=StubTransport([UpstreamResult(599, {}, b"connection failed")]),
        headers={},
        body=_phase2b_body(),
        classification=_phase2b_classification(),
        config=_phase2b_config_without_checks(config),
        request_id="req-baseline-599",
    )

    assert result.mode == "phase2b_error"
    assert result.fallback_reason == "baseline_failed"
    assert json.loads(result.baseline_upstream.body) == {
        "type": "error",
        "error": {"type": "api_error", "message": "baseline_failed"},
    }


@pytest.mark.asyncio
async def test_run_phase2b_rejects_ledger_conflicts_when_enabled(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_without_checks(config)
    phase2b_config.phase2b.enable_worst_case_attack = True
    phase2b_config.phase2b.enable_ledger_checks = True
    transport = StubTransport([
        _message("14"),
        _message('{"candidate_answer":"21"}'), _message("PASS\nLEDGER_CONFLICT: shape controllable by touch"),
        _message('{"candidate_answer":"22"}'), _message("PASS\nLEDGER_CONFLICT: shape controllable by touch"),
        _message('{"candidate_answer":"23"}'), _message("PASS\nLEDGER_CONFLICT: shape controllable by touch"),
    ])

    result = await run_phase2b(transport, {}, _phase2b_body(), _phase2b_classification(), phase2b_config, "req-ledger-on")

    assert result.mode == "fallback_phase1"
    assert result.fallback_reason == "no_surviving_branches"
    assert all(decision.reason == "ledger_conflict" for decision in result.branch_decisions)


@pytest.mark.asyncio
async def test_run_phase2b_allows_ledger_conflicts_when_disabled(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_without_checks(config)
    phase2b_config.phase2b.enable_worst_case_attack = True
    phase2b_config.phase2b.enable_ledger_checks = False
    transport = StubTransport([
        _message("14"),
        _message('{"candidate_answer":"21"}'), _message("PASS\nLEDGER_CONFLICT: shape controllable by touch"),
        _message('{"candidate_answer":"22"}'), _message("PASS\nLEDGER_CONFLICT: shape controllable by touch"),
        _message('{"candidate_answer":"23"}'), _message("PASS\nLEDGER_CONFLICT: shape controllable by touch"),
        _message("21"),
    ])

    result = await run_phase2b(transport, {}, _phase2b_body(), _phase2b_classification(), phase2b_config, "req-ledger-off")

    assert result.mode == "phase2b"
    assert all(decision.survived for decision in result.branch_decisions)
    assert "target is guarantee, not existence" not in transport.requests[2]["body"]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_run_phase2b_allows_pass_attack_that_confirms_the_ledger(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_without_checks(config)
    phase2b_config.phase2b.enable_worst_case_attack = True
    phase2b_config.phase2b.enable_ledger_checks = True
    transport = StubTransport([
        _message("14"),
        _message('{"candidate_answer":"21"}'), _message("PASS: shape is controllable by touch"),
        _message('{"candidate_answer":"22"}'), _message("PASS: shape is controllable by touch"),
        _message('{"candidate_answer":"23"}'), _message("PASS: shape is controllable by touch"),
        _message("21"),
    ])

    result = await run_phase2b(transport, {}, _phase2b_body(), _phase2b_classification(), phase2b_config, "req-ledger-phrase")

    assert result.mode == "phase2b"
    assert all(decision.survived for decision in result.branch_decisions)


@pytest.mark.asyncio
async def test_run_phase2b_rejects_pass_attack_that_negates_the_ledger(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_without_checks(config)
    phase2b_config.phase2b.enable_worst_case_attack = True
    phase2b_config.phase2b.enable_ledger_checks = True
    transport = StubTransport([
        _message("14"),
        _message('{"candidate_answer":"21"}'), _message("PASS: shape is not controllable by touch"),
        _message('{"candidate_answer":"22"}'), _message("PASS: shape is not controllable by touch"),
        _message('{"candidate_answer":"23"}'), _message("PASS: shape is not controllable by touch"),
    ])

    result = await run_phase2b(transport, {}, _phase2b_body(), _phase2b_classification(), phase2b_config, "req-ledger-negated")

    assert result.mode == "fallback_phase1"
    assert all(decision.reason == "ledger_conflict" for decision in result.branch_decisions)


@pytest.mark.asyncio
async def test_run_phase2b_rejects_pass_attack_that_negates_distinguishability(config) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_without_checks(config)
    phase2b_config.phase2b.enable_worst_case_attack = True
    phase2b_config.phase2b.enable_ledger_checks = True
    transport = StubTransport([
        _message("14"),
        _message('{"candidate_answer":"21"}'), _message("PASS: shape is not distinguishable by touch"),
        _message('{"candidate_answer":"22"}'), _message("PASS: shape is not distinguishable by touch"),
        _message('{"candidate_answer":"23"}'), _message("PASS: shape is not distinguishable by touch"),
    ])

    result = await run_phase2b(transport, {}, _phase2b_body(), _phase2b_classification(), phase2b_config, "req-ledger-not-distinguishable")

    assert result.mode == "fallback_phase1"
    assert all(decision.reason == "ledger_conflict" for decision in result.branch_decisions)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attack_text",
    [
        "PASS: shape isn't distinguishable by touch",
        "PASS: shape is indistinguishable by touch",
        "PASS: cannot tell the shapes apart by touch",
        "PASS: can't tell the shapes apart by touch",
        "PASS: 不能通过手感区分形状",
    ],
)
async def test_run_phase2b_rejects_normal_negated_distinguishability_phrasings(config, attack_text: str) -> None:
    from proxy.phase2b import run_phase2b

    phase2b_config = _phase2b_config_without_checks(config)
    phase2b_config.phase2b.enable_worst_case_attack = True
    phase2b_config.phase2b.enable_ledger_checks = True
    transport = StubTransport([
        _message("14"),
        _message('{"candidate_answer":"21"}'), _message(attack_text),
        _message('{"candidate_answer":"22"}'), _message(attack_text),
        _message('{"candidate_answer":"23"}'), _message(attack_text),
    ])

    result = await run_phase2b(transport, {}, _phase2b_body(), _phase2b_classification(), phase2b_config, "req-ledger-normal-negation")

    assert result.mode == "fallback_phase1"
    assert all(decision.reason == "ledger_conflict" for decision in result.branch_decisions)
