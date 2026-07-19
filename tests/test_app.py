from __future__ import annotations

from copy import deepcopy

import httpx
from fastapi.testclient import TestClient

from proxy.upstream import UpstreamResult


class StubTransport:
    def __init__(self, result: UpstreamResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def send_with_retry(
        self,
        *,
        context,
        headers: dict[str, str],
        body: dict[str, object],
        replay_safe: bool,
        stream: bool,
    ) -> UpstreamResult:
        self.calls.append(
            {
                "request_id": context.request_id,
                "attempt": context.attempt,
                "headers": headers,
                "body": body,
                "replay_safe": replay_safe,
                "stream": stream,
            },
        )
        return self.result


def test_health_endpoint_returns_ok(config) -> None:
    from proxy.app import create_app

    with TestClient(create_app(config)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_proxy_startup_smoke(config) -> None:
    from proxy.app import create_app

    app = create_app(config)

    assert app is not None


def test_factory_create_app_uses_default_example_config() -> None:
    from proxy.app import create_app

    app = create_app()

    assert app is not None


def test_default_config_path_prefers_project_local_claude_config(monkeypatch, tmp_path) -> None:
    from proxy.app import _default_config_path

    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".claude").mkdir()
    local_config = project_root / ".claude" / "cc-proxy.toml"
    local_config.write_text("placeholder", encoding="utf-8")
    monkeypatch.chdir(project_root)
    monkeypatch.delenv("CC_PROXY_CONFIG", raising=False)

    assert _default_config_path() == local_config


def test_ready_endpoint_reports_probe_state(config) -> None:
    from proxy.app import create_app

    with TestClient(create_app(config)) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert "stream_probe" in response.json()


def test_messages_returns_non_streamed_upstream_json(config) -> None:
    from proxy.app import create_app

    transport = StubTransport(
        UpstreamResult(
            status_code=200,
            headers={"content-type": "application/json"},
            body=b'{"type":"message","id":"msg_1","content":[{"type":"text","text":"21"}]}',
        ),
    )
    body = {
        "model": "test-model",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "reply with 21"}],
    }

    with TestClient(create_app(config, transport=transport)) as client:
        response = client.post("/v1/messages", json=body, headers={"x-request-id": "req-app-1"})

    assert response.status_code == 200
    assert response.json()["type"] == "message"
    assert transport.calls[0]["replay_safe"] is True
    assert transport.calls[0]["stream"] is False
    assert transport.calls[0]["body"] == body


def test_messages_streams_successful_upstream_sse(config) -> None:
    from proxy.app import create_app

    payload = b"event: message\ndata: ok\n\n"
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream; charset=utf-8"},
        content=payload,
    )
    transport = StubTransport(
        UpstreamResult(
            status_code=200,
            headers=dict(response.headers),
            body=b"",
            response=response,
            streamed=True,
        ),
    )
    body = {
        "model": "test-model",
        "max_tokens": 32,
        "stream": True,
        "messages": [{"role": "user", "content": "reply with 21"}],
    }

    with TestClient(create_app(config, transport=transport)) as client:
        streamed = client.post("/v1/messages", json=body)

    assert streamed.status_code == 200
    assert streamed.content == payload
    assert streamed.headers["content-type"].startswith("text/event-stream")
    assert transport.calls[0]["stream"] is True


def test_messages_normalizes_streamed_redirect_before_downstream_bytes(config) -> None:
    from proxy.app import create_app

    transport = StubTransport(
        UpstreamResult(
            status_code=302,
            headers={"content-type": "application/json"},
            body=b"",
            response=httpx.Response(
                302,
                headers={"content-type": "application/json"},
                content=b'{"message":"redirected"}',
            ),
            streamed=True,
        ),
    )
    body = {
        "model": "test-model",
        "max_tokens": 32,
        "stream": True,
        "messages": [{"role": "user", "content": "reply with 21"}],
    }

    with TestClient(create_app(config, transport=transport)) as client:
        response = client.post("/v1/messages", json=body)

    assert response.status_code == 302
    assert response.json() == {
        "type": "error",
        "error": {"type": "api_error", "message": "redirected"},
        "status_code": 302,
    }


def test_messages_bypass_phase2_for_streamed_requests(config, monkeypatch) -> None:
    from proxy.app import create_app

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    phase2_config.classification.min_chars = 1
    phase2_config.classification.reasoning_keyword_patterns = ["minimum"]
    phase2_config.classification.output_constraint_patterns = ["only output"]
    phase2_calls: list[dict[str, object]] = []

    async def fake_run_phase2(**kwargs) -> None:
        phase2_calls.append(kwargs)

    monkeypatch.setattr("proxy.app.run_phase2", fake_run_phase2)
    monkeypatch.setattr("proxy.app.is_phase2_eligible", lambda *_: True)
    body = {
        "model": "test-model",
        "max_tokens": 32,
        "stream": True,
        "messages": [{"role": "user", "content": "minimum logic only output"}],
    }
    transport = StubTransport(
        UpstreamResult(
            status_code=200,
            headers={"content-type": "text/event-stream"},
            body=b"",
            response=httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"event: message\ndata: ok\n\n",
            ),
            streamed=True,
        ),
    )

    with TestClient(create_app(phase2_config, transport=transport)) as client:
        response = client.post("/v1/messages", json=body, headers={"x-cc-proxy-phase": "phase2"})

    assert response.status_code == 200
    assert transport.calls[0]["stream"] is True
    assert phase2_calls == []


def test_messages_phase2_selector_bypasses_phase2_when_tools_are_declared(config, monkeypatch) -> None:
    from proxy.app import create_app

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    phase2_config.classification.min_chars = 1
    phase2_config.classification.reasoning_keyword_patterns = ["minimum"]
    phase2_config.classification.output_constraint_patterns = ["only output"]
    phase2_calls: list[dict[str, object]] = []

    async def fake_run_phase2(**kwargs) -> None:
        phase2_calls.append(kwargs)

    monkeypatch.setattr("proxy.app.run_phase2", fake_run_phase2)
    transport = StubTransport(
        UpstreamResult(
            status_code=200,
            headers={"content-type": "application/json"},
            body=b'{"type":"message","content":[{"type":"text","text":"21"}]}',
        ),
    )
    body = {
        "model": "test-model",
        "max_tokens": 4096,
        "tools": [{"name": "shell", "input_schema": {"type": "object"}}],
        "messages": [{"role": "user", "content": "Find the minimum and only output 21"}],
    }

    with TestClient(create_app(phase2_config, transport=transport)) as client:
        response = client.post("/v1/messages", json=body, headers={"x-cc-proxy-phase": "phase2"})

    assert response.status_code == 200
    assert phase2_calls == []
    assert len(transport.calls) == 1
    assert transport.calls[0]["body"] == body


def test_messages_keep_phase1_selector_on_baseline_path(config, monkeypatch) -> None:
    from proxy.app import create_app
    from proxy.schemas import AggregationDecision, Phase2ExecutionResult

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    phase2_config.classification.min_chars = 1
    phase2_config.classification.reasoning_keyword_patterns = ["minimum"]
    phase2_config.classification.output_constraint_patterns = ["only output"]
    phase2_calls: list[dict[str, object]] = []

    async def fake_run_phase2(**kwargs):
        phase2_calls.append(kwargs)
        baseline = UpstreamResult(200, {"content-type": "application/json"}, b"{}")
        return Phase2ExecutionResult(
            "phase2",
            baseline,
            [],
            AggregationDecision("choose_candidate", "22", "test"),
            {"type": "message", "content": [{"type": "text", "text": "22"}]},
        )

    monkeypatch.setattr("proxy.app.run_phase2", fake_run_phase2)
    transport = StubTransport(
        UpstreamResult(
            200,
            {"content-type": "application/json"},
            b'{"type":"message","content":[{"type":"text","text":"21"}]}',
        )
    )

    with TestClient(create_app(phase2_config, transport=transport)) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-cc-proxy-phase": "phase1"},
            json={
                "model": "m",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": "Find the minimum and only output 21"}],
            },
        )

    assert response.status_code == 200
    assert response.json()["content"][0]["text"] == "21"
    assert phase2_calls == []
    assert "x-cc-proxy-phase" not in transport.calls[0]["headers"]


def test_messages_return_phase2_selected_answer_for_non_streamed_requests(config, monkeypatch) -> None:
    from proxy.app import create_app
    from proxy.schemas import AggregationDecision, Phase2ExecutionResult

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    phase2_config.classification.min_chars = 1
    phase2_config.classification.reasoning_keyword_patterns = ["minimum"]
    phase2_config.classification.output_constraint_patterns = ["only output"]

    async def fake_run_phase2(**kwargs):
        baseline = UpstreamResult(
            200,
            {"content-type": "application/json"},
            b'{"type":"message","content":[{"type":"text","text":"29"}]}',
        )
        return Phase2ExecutionResult(
            "phase2",
            baseline,
            [],
            AggregationDecision("choose_candidate", "21", "test"),
            {"type": "message", "id": "phase2", "content": [{"type": "text", "text": "21"}]},
        )

    monkeypatch.setattr("proxy.app.run_phase2", fake_run_phase2)

    with TestClient(create_app(phase2_config, transport=StubTransport(UpstreamResult(200, {}, b"{}")))) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-cc-proxy-phase": "phase2"},
            json={
                "model": "m",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": "Find the minimum and only output 21"}],
            },
        )

    assert response.status_code == 200
    assert response.json()["content"][0]["text"] == "21"


def test_messages_return_phase2b_selected_answer_for_non_streamed_requests(config, monkeypatch) -> None:
    from proxy.app import create_app
    from proxy.schemas import Phase2bBranchDecision, Phase2bExecutionResult

    phase2b_config = config.model_copy(deep=True)
    phase2b_config.phase2b.enabled = True
    monkeypatch.setattr("proxy.app.is_phase2b_eligible", lambda *_: True)

    async def fake_run_phase2b(**kwargs):
        return Phase2bExecutionResult(
            "phase2b",
            UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"text","text":"14"}]}'),
            [Phase2bBranchDecision("b1", True, [], "21", "survived")],
            {"type": "message", "id": "phase2b", "content": [{"type": "text", "text": "21"}]},
            None,
        )

    monkeypatch.setattr("proxy.app.run_phase2b", fake_run_phase2b)
    with TestClient(create_app(phase2b_config, transport=StubTransport(UpstreamResult(200, {}, b"{}")))) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": "m",
                "max_tokens": 4096,
                "stream": False,
                "messages": [{"role": "user", "content": "最少 只输出"}],
            },
        )

    assert response.status_code == 200
    assert response.json()["content"][0]["text"] == "21"


def test_messages_return_phase2b_selected_answer_as_stream_for_streamed_requests(config, monkeypatch) -> None:
    from proxy.app import create_app
    from proxy.schemas import Phase2bBranchDecision, Phase2bExecutionResult

    phase2b_config = config.model_copy(deep=True)
    phase2b_config.phase2b.enabled = True
    eligibility_bodies: list[dict[str, object]] = []
    phase2b_bodies: list[dict[str, object]] = []

    def fake_is_phase2b_eligible(body, *_args):
        eligibility_bodies.append(deepcopy(body))
        return True

    async def fake_run_phase2b(**kwargs):
        phase2b_bodies.append(deepcopy(kwargs["body"]))
        return Phase2bExecutionResult(
            "phase2b",
            UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"text","text":"14"}]}'),
            [Phase2bBranchDecision("b1", True, [], "21", "survived")],
            {"type": "message", "id": "phase2b", "content": [{"type": "text", "text": "21"}]},
            None,
        )

    monkeypatch.setattr("proxy.app.is_phase2b_eligible", fake_is_phase2b_eligible)
    monkeypatch.setattr("proxy.app.run_phase2b", fake_run_phase2b)
    with TestClient(create_app(phase2b_config, transport=StubTransport(UpstreamResult(200, {}, b"{}")))) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": "m",
                "max_tokens": 4096,
                "stream": True,
                "messages": [{"role": "user", "content": "最少 只输出"}],
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert b"event: message_start" in response.content
    assert b"event: content_block_delta" in response.content
    assert b'"text":"21"' in response.content
    assert eligibility_bodies[0]["stream"] is False
    assert phase2b_bodies[0]["stream"] is False


def test_messages_return_phase2b_baseline_as_stream_for_streamed_requests(config, monkeypatch) -> None:
    from proxy.app import create_app
    from proxy.schemas import Phase2bBranchDecision, Phase2bExecutionResult

    phase2b_config = config.model_copy(deep=True)
    phase2b_config.phase2b.enabled = True
    monkeypatch.setattr("proxy.app.is_phase2b_eligible", lambda *_: True)

    async def fake_run_phase2b(**kwargs):
        return Phase2bExecutionResult(
            "phase2b",
            UpstreamResult(
                200,
                {"content-type": "application/json"},
                b'{"type":"message","id":"baseline","content":[{"type":"text","text":"21"}]}',
            ),
            [Phase2bBranchDecision("b1", False, ["boundary_conflict"], None, "fallback")],
            None,
            "fallback_to_baseline",
        )

    monkeypatch.setattr("proxy.app.run_phase2b", fake_run_phase2b)
    with TestClient(create_app(phase2b_config, transport=StubTransport(UpstreamResult(200, {}, b"{}")))) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": "m",
                "max_tokens": 4096,
                "stream": True,
                "messages": [{"role": "user", "content": "最少 只输出"}],
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert b"event: message_start" in response.content
    assert b'"id":"baseline"' in response.content
    assert b'"text":"21"' in response.content


def test_messages_phase2b_preserves_raw_candy_prompt_for_streamed_requests_without_forcing_exact_output(
    config, monkeypatch
) -> None:
    from proxy.app import create_app
    from proxy.schemas import Phase2bBranchDecision, Phase2bExecutionResult

    phase2b_config = config.model_copy(deep=True)
    phase2b_config.phase2b.enabled = True
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
    phase2b_bodies: list[dict[str, object]] = []

    async def fake_run_phase2b(**kwargs):
        phase2b_bodies.append(deepcopy(kwargs["body"]))
        return Phase2bExecutionResult(
            "phase2b",
            UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"text","text":"30"}]}'),
            [Phase2bBranchDecision("boundary", True, [], "21", "verified")],
            {"type": "message", "id": "phase2b", "content": [{"type": "text", "text": "21"}]},
            None,
        )

    monkeypatch.setattr("proxy.app.run_phase2b", fake_run_phase2b)
    transport = StubTransport(
        UpstreamResult(
            200,
            {"content-type": "application/json"},
            b'{"type":"message","content":[{"type":"text","text":"30"}]}',
        ),
    )
    body = {
        "model": "m",
        "max_tokens": 4096,
        "stream": True,
        "messages": [
            {
                "role": "user",
                "content": (
                    "在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。"
                    "现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果"
                    "才能保证手中同时拥有不同形状的苹果味和桃子味的糖？（同时手中有圆形苹果味匹配五角星桃子味糖果，或者有圆形桃子味"
                    "匹配五角星苹果味糖果都满足要求）苹果味 桃子味 西瓜味 圆形 7 9 8 五角星形 7 6 4"
                ),
            }
        ],
    }

    with TestClient(create_app(phase2b_config, transport=transport)) as client:
        response = client.post("/v1/messages", json=body, headers={"x-cc-proxy-phase": "phase2b"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert b'"text":"21"' in response.content
    assert len(phase2b_bodies) == 1
    assert phase2b_bodies[0]["stream"] is False
    assert "只输出最终答案。" not in phase2b_bodies[0]["messages"][0]["content"]
    assert transport.calls == []


def test_messages_phase2b_extracts_latest_user_problem_from_claude_code_wrapped_request(
    config, monkeypatch
) -> None:
    from proxy.app import create_app
    from proxy.schemas import Phase2bBranchDecision, Phase2bExecutionResult

    phase2b_config = config.model_copy(deep=True)
    phase2b_config.phase2b.enabled = True
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
    phase2b_bodies: list[dict[str, object]] = []

    async def fake_run_phase2b(**kwargs):
        phase2b_bodies.append(deepcopy(kwargs["body"]))
        return Phase2bExecutionResult(
            "phase2b",
            UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"text","text":"30"}]}'),
            [Phase2bBranchDecision("boundary", True, [], "21", "verified")],
            {"type": "message", "id": "phase2b", "content": [{"type": "text", "text": "21"}]},
            None,
        )

    monkeypatch.setattr("proxy.app.run_phase2b", fake_run_phase2b)
    transport = StubTransport(
        UpstreamResult(
            200,
            {"content-type": "application/json"},
            b'{"type":"message","content":[{"type":"text","text":"30"}]}',
        ),
    )

    wrapped_body = {
        "model": "m",
        "max_tokens": 4096,
        "stream": True,
        "system": "You are Claude Code. Help with code, functions, tools, and explanations.",
        "tools": [{"name": "bash", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "assistant", "content": "Prior tool context."},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "<system-reminder>\n"
                            "You are Claude Code. Use tools when needed.\n"
                            "</system-reminder>"
                        ),
                    },
                    {
                        "type": "text",
                        "text": (
                            "在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。"
                            "现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果"
                            "才能保证手中同时拥有不同形状的苹果味和桃子味的糖？（同时手中有圆形苹果味匹配五角星桃子味糖果，或者有圆形桃子味"
                            "匹配五角星苹果味糖果都满足要求）\n苹果味 桃子味 西瓜味\n圆形 7 9 8\n五角星形 7 6 4"
                        ),
                    },
                ],
            },
        ],
    }

    with TestClient(create_app(phase2b_config, transport=transport)) as client:
        response = client.post("/v1/messages", json=wrapped_body)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert b'"text":"21"' in response.content
    assert len(phase2b_bodies) == 1
    assert phase2b_bodies[0]["stream"] is False
    assert phase2b_bodies[0]["messages"] == [
        {
            "role": "user",
            "content": (
                "在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。"
                "现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果"
                "才能保证手中同时拥有不同形状的苹果味和桃子味的糖？（同时手中有圆形苹果味匹配五角星桃子味糖果，或者有圆形桃子味"
                "匹配五角星苹果味糖果都满足要求）\n苹果味 桃子味 西瓜味\n圆形 7 9 8\n五角星形 7 6 4"
            ),
        }
    ]
    assert "tools" not in phase2b_bodies[0]
    assert "system" not in phase2b_bodies[0]
    assert transport.calls == []


def test_request_kind_detects_title_generation_prompt() -> None:
    from proxy.app import _request_kind

    kind = _request_kind(
        {
            "system": "Generate a concise, sentence-case title (3-7 words) for this conversation.",
            "messages": [{"role": "user", "content": "<session>在一个黑色的袋子里...</session>"}],
        }
    )

    assert kind == "title_generation"


def test_request_kind_defaults_to_user_request_for_candy_prompt() -> None:
    from proxy.app import _request_kind

    kind = _request_kind(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状。",
                }
            ]
        }
    )

    assert kind == "user_request"


def test_messages_auto_route_still_uses_phase2_for_eligible_non_streamed_requests(config, monkeypatch) -> None:
    from proxy.app import create_app
    from proxy.schemas import AggregationDecision, Phase2ExecutionResult

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    phase2_config.classification.min_chars = 1
    phase2_config.classification.reasoning_keyword_patterns = ["minimum"]
    phase2_config.classification.output_constraint_patterns = ["only output"]
    phase2_calls: list[dict[str, object]] = []

    async def fake_run_phase2(**kwargs):
        phase2_calls.append(kwargs)
        baseline = UpstreamResult(
            200,
            {"content-type": "application/json"},
            b'{"type":"message","content":[{"type":"text","text":"29"}]}',
        )
        return Phase2ExecutionResult(
            "phase2",
            baseline,
            [],
            AggregationDecision("choose_candidate", "21", "test"),
            {"type": "message", "id": "phase2", "content": [{"type": "text", "text": "21"}]},
        )

    monkeypatch.setattr("proxy.app.run_phase2", fake_run_phase2)

    with TestClient(create_app(phase2_config, transport=StubTransport(UpstreamResult(200, {}, b"{}")))) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": "m",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": "Find the minimum and only output 21"}],
            },
        )

    assert response.status_code == 200
    assert response.json()["content"][0]["text"] == "21"
    assert len(phase2_calls) == 1


def test_messages_return_phase2_baseline_when_no_selected_payload(config, monkeypatch) -> None:
    from proxy.app import create_app
    from proxy.schemas import AggregationDecision, Phase2ExecutionResult

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    phase2_config.classification.min_chars = 1
    phase2_config.classification.reasoning_keyword_patterns = ["minimum"]
    phase2_config.classification.output_constraint_patterns = ["only output"]
    baseline = UpstreamResult(
        200,
        {"content-type": "application/json"},
        b'{"type":"message","content":[{"type":"text","text":"29"}]}',
    )

    async def fake_run_phase2(**kwargs):
        return Phase2ExecutionResult(
            "fallback_phase1",
            baseline,
            [],
            AggregationDecision("fallback_baseline", None, "test"),
            None,
        )

    monkeypatch.setattr("proxy.app.run_phase2", fake_run_phase2)

    with TestClient(create_app(phase2_config, transport=StubTransport(UpstreamResult(200, {}, b"{}")))) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-cc-proxy-phase": "phase2"},
            json={
                "model": "m",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": "Find the minimum and only output 29"}],
            },
        )

    assert response.status_code == 200
    assert response.json()["content"][0]["text"] == "29"


def test_messages_phase2_malformed_200_fallback_becomes_502_error_envelope(config, monkeypatch) -> None:
    from proxy.app import create_app
    from proxy.schemas import AggregationDecision, Phase2ExecutionResult

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    phase2_config.classification.min_chars = 1
    phase2_config.classification.reasoning_keyword_patterns = ["minimum"]
    phase2_config.classification.output_constraint_patterns = ["only output"]
    baseline = UpstreamResult(200, {"content-type": "application/json"}, b"{")

    async def fake_run_phase2(**kwargs):
        return Phase2ExecutionResult(
            "fallback_phase1",
            baseline,
            [],
            AggregationDecision("fallback_baseline", None, "test"),
            None,
        )

    monkeypatch.setattr("proxy.app.run_phase2", fake_run_phase2)

    with TestClient(create_app(phase2_config, transport=StubTransport(UpstreamResult(200, {}, b"{}")))) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-cc-proxy-phase": "phase2"},
            json={
                "model": "m",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": "Find the minimum and only output 29"}],
            },
        )

    assert response.status_code == 502
    assert response.json() == {
        "type": "error",
        "error": {"type": "api_error", "message": "upstream returned an invalid success payload"},
        "status_code": 502,
    }


def test_parse_upstream_json_normalizes_only_malformed_2xx_payloads() -> None:
    from proxy.app import _parse_upstream_json

    status_code, payload = _parse_upstream_json(UpstreamResult(200, {}, b"{"))

    assert status_code == 502
    assert payload == {
        "type": "error",
        "error": {"type": "api_error", "message": "upstream returned an invalid success payload"},
        "status_code": 502,
    }

    status_code, payload = _parse_upstream_json(UpstreamResult(302, {}, b"upstream redirect"))

    assert status_code == 302
    assert payload == {
        "type": "error",
        "error": {"type": "api_error", "message": "upstream redirect"},
        "status_code": 302,
    }


def test_parse_upstream_json_normalizes_only_non_object_2xx_json_payloads() -> None:
    from proxy.app import _parse_upstream_json

    status_code, payload = _parse_upstream_json(UpstreamResult(200, {}, b"[]"))

    assert status_code == 502
    assert payload == {
        "type": "error",
        "error": {"type": "api_error", "message": "upstream returned an invalid success payload"},
        "status_code": 502,
    }

    status_code, payload = _parse_upstream_json(UpstreamResult(302, {}, b"null"))

    assert status_code == 302
    assert payload == {
        "type": "error",
        "error": {"type": "api_error", "message": "upstream returned a non-object JSON payload"},
        "status_code": 302,
    }


def test_parse_upstream_json_rejects_empty_200_object_payload() -> None:
    from proxy.app import _parse_upstream_json

    status_code, payload = _parse_upstream_json(UpstreamResult(200, {}, b"{}"))

    assert status_code == 502
    assert payload["error"]["message"] == "upstream returned an invalid success payload"


def test_parse_upstream_json_rejects_invalid_200_message_shape() -> None:
    from proxy.app import _parse_upstream_json

    status_code, payload = _parse_upstream_json(
        UpstreamResult(200, {}, b'{"type":"message","content":"invalid"}'),
    )

    assert status_code == 502
    assert payload["error"]["message"] == "upstream returned an invalid success payload"


def test_parse_upstream_json_accepts_valid_200_message_with_tool_use_block() -> None:
    from proxy.app import _parse_upstream_json

    body = (
        b'{"type":"message","content":[{"type":"tool_use","id":"toolu_1","name":"shell","input":{"cmd":"pwd"}}]}'
    )

    status_code, payload = _parse_upstream_json(UpstreamResult(200, {}, body))

    assert status_code == 200
    assert payload["type"] == "message"
    assert payload["content"][0]["type"] == "tool_use"


def test_parse_upstream_json_rejects_incomplete_200_tool_use_block() -> None:
    from proxy.app import _parse_upstream_json

    status_code, payload = _parse_upstream_json(
        UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"tool_use"}]}'),
    )

    assert status_code == 502
    assert payload["error"]["message"] == "upstream returned an invalid success payload"


def test_parse_upstream_json_accepts_valid_200_message_with_thinking_block() -> None:
    from proxy.app import _parse_upstream_json

    body = b'{"type":"message","content":[{"type":"thinking","thinking":"step by step","signature":"sig"}]}'

    status_code, payload = _parse_upstream_json(UpstreamResult(200, {}, body))

    assert status_code == 200
    assert payload["type"] == "message"
    assert payload["content"][0]["type"] == "thinking"


def test_parse_upstream_json_rejects_invalid_200_thinking_block() -> None:
    from proxy.app import _parse_upstream_json

    status_code, payload = _parse_upstream_json(
        UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"thinking","thinking":42}]}'),
    )

    assert status_code == 502
    assert payload["error"]["message"] == "upstream returned an invalid success payload"


def test_parse_upstream_json_rejects_missing_signature_in_200_thinking_block() -> None:
    from proxy.app import _parse_upstream_json

    status_code, payload = _parse_upstream_json(
        UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"thinking","thinking":"step by step"}]}'),
    )

    assert status_code == 502
    assert payload["error"]["message"] == "upstream returned an invalid success payload"


def test_parse_upstream_json_accepts_valid_200_message_with_redacted_thinking_block() -> None:
    from proxy.app import _parse_upstream_json

    body = b'{"type":"message","content":[{"type":"redacted_thinking","data":"opaque"}]}'

    status_code, payload = _parse_upstream_json(UpstreamResult(200, {}, body))

    assert status_code == 200
    assert payload["type"] == "message"
    assert payload["content"][0]["type"] == "redacted_thinking"


def test_parse_upstream_json_accepts_valid_200_message_with_unknown_content_block_type() -> None:
    from proxy.app import _parse_upstream_json

    body = b'{"type":"message","content":[{"type":"server_tool_result","payload":{"answer":"21"}}]}'

    status_code, payload = _parse_upstream_json(UpstreamResult(200, {}, body))

    assert status_code == 200
    assert payload["type"] == "message"
    assert payload["content"][0]["type"] == "server_tool_result"


def test_parse_upstream_json_rejects_invalid_200_redacted_thinking_block() -> None:
    from proxy.app import _parse_upstream_json

    status_code, payload = _parse_upstream_json(
        UpstreamResult(200, {}, b'{"type":"message","content":[{"type":"redacted_thinking"}]}'),
    )

    assert status_code == 502
    assert payload["error"]["message"] == "upstream returned an invalid success payload"


def test_messages_normalizes_phase2_baseline_non_json_error_body(config, monkeypatch) -> None:
    from proxy.app import create_app
    from proxy.schemas import AggregationDecision, Phase2ExecutionResult

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    phase2_config.classification.min_chars = 1
    phase2_config.classification.reasoning_keyword_patterns = ["minimum"]
    phase2_config.classification.output_constraint_patterns = ["only output"]
    baseline = UpstreamResult(
        503,
        {"content-type": "text/plain"},
        b"upstream temporarily unavailable",
    )

    async def fake_run_phase2(**kwargs):
        return Phase2ExecutionResult(
            "fallback_phase1",
            baseline,
            [],
            AggregationDecision("fallback_baseline", None, "test"),
            None,
        )

    monkeypatch.setattr("proxy.app.run_phase2", fake_run_phase2)

    with TestClient(create_app(phase2_config, transport=StubTransport(UpstreamResult(200, {}, b"{}")))) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-cc-proxy-phase": "phase2"},
            json={
                "model": "m",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": "Find the minimum and only output 29"}],
            },
        )

    assert response.status_code == 503
    assert response.json()["type"] == "error"
    assert response.json()["error"]["type"] == "api_error"
    assert response.json()["error"]["message"] == "upstream temporarily unavailable"


def test_messages_normalizes_phase2_baseline_json_error_body(config, monkeypatch) -> None:
    from proxy.app import create_app
    from proxy.schemas import AggregationDecision, Phase2ExecutionResult

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    phase2_config.classification.min_chars = 1
    phase2_config.classification.reasoning_keyword_patterns = ["minimum"]
    phase2_config.classification.output_constraint_patterns = ["only output"]
    baseline = UpstreamResult(
        503,
        {"content-type": "application/json"},
        b'{"message":"overloaded"}',
    )

    async def fake_run_phase2(**kwargs):
        return Phase2ExecutionResult(
            "fallback_phase1",
            baseline,
            [],
            AggregationDecision("fallback_baseline", None, "test"),
            None,
        )

    monkeypatch.setattr("proxy.app.run_phase2", fake_run_phase2)

    with TestClient(create_app(phase2_config, transport=StubTransport(UpstreamResult(200, {}, b"{}")))) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-cc-proxy-phase": "phase2"},
            json={
                "model": "m",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": "Find the minimum and only output 29"}],
            },
        )

    assert response.status_code == 503
    assert response.json() == {
        "type": "error",
        "error": {"type": "api_error", "message": "overloaded"},
        "status_code": 503,
    }


def test_messages_wraps_phase2_baseline_error_as_anthropic_error_envelope(config, monkeypatch) -> None:
    from proxy.app import create_app
    from proxy.schemas import AggregationDecision, Phase2ExecutionResult

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
    phase2_config.classification.min_chars = 1
    phase2_config.classification.reasoning_keyword_patterns = ["minimum"]
    phase2_config.classification.output_constraint_patterns = ["only output"]
    baseline = UpstreamResult(599, {"content-type": "text/plain"}, b"phase2 baseline timed out")

    async def fake_run_phase2(**kwargs):
        return Phase2ExecutionResult(
            "fallback_phase1",
            baseline,
            [],
            AggregationDecision("fallback_baseline", None, "baseline_timeout"),
            None,
        )

    monkeypatch.setattr("proxy.app.run_phase2", fake_run_phase2)

    with TestClient(create_app(phase2_config, transport=StubTransport(UpstreamResult(200, {}, b"{}")))) as client:
        response = client.post(
            "/v1/messages",
            headers={"x-cc-proxy-phase": "phase2"},
            json={
                "model": "m",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": "Find the minimum and only output 29"}],
            },
        )

    assert response.status_code == 599
    assert response.json()["type"] == "error"
    assert response.json()["error"]["type"] == "api_error"


def test_messages_wraps_non_json_upstream_body_before_downstream_bytes(config) -> None:
    from proxy.app import create_app

    transport = StubTransport(
        UpstreamResult(
            status_code=502,
            headers={"content-type": "text/plain"},
            body=b"gateway exploded",
        ),
    )
    body = {
        "model": "test-model",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "reply with 21"}],
    }

    with TestClient(create_app(config, transport=transport)) as client:
        response = client.post("/v1/messages", json=body)

    assert response.status_code == 502
    assert response.json()["type"] == "error"
    assert response.json()["error"]["message"] == "gateway exploded"


def test_messages_normalizes_non_phase2_json_error_body(config) -> None:
    from proxy.app import create_app

    transport = StubTransport(
        UpstreamResult(
            status_code=503,
            headers={"content-type": "application/json"},
            body=b'{"message":"overloaded"}',
        ),
    )
    body = {
        "model": "test-model",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "reply with 21"}],
    }

    with TestClient(create_app(config, transport=transport)) as client:
        response = client.post("/v1/messages", json=body)

    assert response.status_code == 503
    assert response.json() == {
        "type": "error",
        "error": {"type": "api_error", "message": "overloaded"},
        "status_code": 503,
    }


def test_messages_normalizes_non_phase2_redirect_json_body(config) -> None:
    from proxy.app import create_app

    transport = StubTransport(
        UpstreamResult(
            status_code=302,
            headers={"content-type": "application/json"},
            body=b'{"message":"redirected"}',
        ),
    )
    body = {
        "model": "test-model",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "reply with 21"}],
    }

    with TestClient(create_app(config, transport=transport)) as client:
        response = client.post("/v1/messages", json=body)

    assert response.status_code == 302
    assert response.json() == {
        "type": "error",
        "error": {"type": "api_error", "message": "redirected"},
        "status_code": 302,
    }


def test_messages_invalid_json_body_returns_anthropic_error_envelope(config) -> None:
    from proxy.app import create_app

    with TestClient(create_app(config, transport=StubTransport(UpstreamResult(200, {}, b"{}")))) as client:
        response = client.post(
            "/v1/messages",
            headers={"content-type": "application/json"},
            content=b"{",
        )

    assert response.status_code == 400
    assert response.json() == {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "invalid request payload"},
        "status_code": 400,
    }


def test_messages_invalid_max_tokens_returns_anthropic_error_envelope(config) -> None:
    from proxy.app import create_app

    with TestClient(create_app(config, transport=StubTransport(UpstreamResult(200, {}, b"{}")))) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": "m",
                "max_tokens": "bad",
                "messages": [{"role": "user", "content": "minimum only output"}],
            },
        )

    assert response.status_code == 400
    assert response.json() == {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "invalid request payload"},
        "status_code": 400,
    }


def test_messages_rewrites_when_route_enters_rewrite_band(config) -> None:
    from proxy.app import create_app

    rewritten_config = config.model_copy(deep=True)
    rewritten_config.rewrite.enabled = True
    rewritten_config.rewrite.max_tokens_floor.minimum_output_tokens = 4096
    rewritten_config.classification.reasoning_keyword_patterns = ["minimum"]
    rewritten_config.classification.output_constraint_patterns = ["only output"]
    transport = StubTransport(
        UpstreamResult(
            status_code=200,
            headers={"content-type": "application/json"},
            body=b'{"type":"message","id":"msg_2","content":[{"type":"text","text":"21"}]}',
        ),
    )
    original = {
        "model": "test-model",
        "max_tokens": 32,
        "messages": [
            {
                "role": "user",
                "content": deepcopy(
                    "Find the minimum answer and only output the final number.\n"
                    "Explain nothing else.\n"
                    "This is a long reasoning prompt that exceeds the rewrite threshold."
                ),
            }
        ],
    }

    with TestClient(create_app(rewritten_config, transport=transport)) as client:
        response = client.post("/v1/messages", json=original)

    assert response.status_code == 200
    assert transport.calls[0]["body"]["max_tokens"] == 4096
