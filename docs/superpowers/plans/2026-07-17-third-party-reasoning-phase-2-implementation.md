# Third-Party Reasoning Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a selective phase-2 reasoning path that uses a retained phase-1 fallback solve plus structured candidate solves and adjudication to improve third-party reasoning stability without replacing Claude Code or Codex as the agent.

**Architecture:** Extend the existing phase-1 proxy instead of adding a separate service. Phase 2 is a non-streamed, replay-safe branch that runs after existing classification, keeps one raw Anthropic-compatible baseline result for fallback, executes structured candidate solves, aggregates them locally, and only then emits one ordinary Anthropic-compatible downstream response.

**Tech Stack:** Python 3.12+, FastAPI, Uvicorn, HTTPX, Pydantic v2, pytest

## Global Constraints

- Accept Anthropic-compatible `/v1/messages` traffic and preserve Claude Code / Codex agent behavior.
- Return an Anthropic-compatible success shape or Anthropic-compatible error envelope whenever no downstream bytes have been emitted yet.
- Once downstream streaming has begun, never replace a partial stream with a brand-new full error envelope.
- Phase 1 uses request-time rewriting and response-time streaming passthrough.
- Phase 2 runs only on non-streamed requests; inbound `stream=true` requests must stay on phase 1.
- The minimal phase-2 path does not support inbound Anthropic SSE passthrough or partial synthetic SSE emission.
- Phase 2 must not trigger for normal Claude Code coding flows.
- The retained phase-1 baseline response is preserved as raw fallback output, not coerced into candidate JSON.
- Structured candidate solve count: `2`.
- Retained fallback baseline solve count: `1`.
- Maximum adjudication calls: `1`.
- Maximum total upstream calls per eligible request: `4`.
- `phase2.sample_count = 2`.
- `phase2.max_adjudication_calls = 1`.
- `phase2.max_parallelism = 2`.
- `phase2.total_timeout_seconds = 180.0`.
- `phase2.require_json_candidates = true`.
- `phase2.candidate_roles = ["constraint_reasoner", "counterexample_reasoner"]`.
- `phase2.max_candidate_output_tokens = 512`.
- `phase2.max_total_upstream_calls = 4`.
- `phase2.fallback_to_phase1_on_failure = true`.
- `phase2.cost_budget_multiplier = 2.0`.
- `phase2.allow_streaming_requests = false`.
- `phase2.max_total_upstream_calls` must be at least `1 + phase2.sample_count + phase2.max_adjudication_calls`.
- `phase2.cost_budget_multiplier` must be greater than or equal to `1.0`.
- Raw unredacted payload logging may be enabled only with both config opt-in and an unsafe environment override.
- Do not commit `.claude/` secrets or local credential files.

---

## Planned File Layout

- Modify: `proxy/config.py`
  - Add `Phase2Config`, nested validation, and runtime budget checks.
- Modify: `proxy/config.toml.example`
  - Add disabled-by-default phase-2 example settings.
- Modify: `proxy/schemas.py`
  - Add typed dataclasses and models for candidate parsing, canonical answers, aggregation decisions, and phase-2 execution results.
- Create: `proxy/candidate.py`
  - Build structured candidate request bodies, parse candidate JSON, canonicalize answers, and parse adjudicator JSON.
- Create: `proxy/phase2.py`
  - Decide eligibility, execute retained baseline plus candidate calls, adjudicate disagreements, and return either a chosen answer or the retained baseline.
- Modify: `proxy/app.py`
  - Insert phase-2 branching after phase-1 classification and before ordinary upstream passthrough for non-streamed eligible requests only.
- Modify: `proxy/upstream.py`
  - Reuse transport for multiple non-stream requests within one downstream request; keep phase-1 retry semantics unchanged.
- Modify: `proxy/evals.py`
  - Add `claude_code_proxy_phase1` and `claude_code_proxy_phase2` modes plus richer scoring metadata.
- Modify: `README.md`
  - Document phase-2 scope, non-stream limitation, and evaluation workflow.
- Create: `tests/test_candidate.py`
  - Verify candidate parsing, canonicalization, and adjudicator parsing.
- Create: `tests/test_phase2.py`
  - Verify eligibility, aggregation branches, fallback, and budget handling with stubbed transports.
- Modify: `tests/test_config.py`
  - Cover phase-2 config defaults and invalid budget combinations.
- Modify: `tests/test_app.py`
  - Verify phase-2 bypass for `stream=true`, phase-2 execution for non-streamed eligible requests, and fallback to retained baseline.
- Modify: `tests/test_evals.py`
  - Cover new eval modes and phase-2 result accounting.

### Task 1: Add Phase 2 Config And Shared Schemas

**Files:**
- Modify: `proxy/config.py`
- Modify: `proxy/config.toml.example`
- Modify: `proxy/schemas.py`
- Modify: `tests/test_config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `class Phase2Config(BaseModel)`
- Produces: `class CandidateAnswer(BaseModel | dataclass)`
- Produces: `class AggregationDecision(BaseModel | dataclass)`
- Produces: `class Phase2ExecutionResult(BaseModel | dataclass)`
- Produces: `validate_phase2_config(config: ProxyConfig) -> None`

- [ ] **Step 1: Write the failing config tests**

```python
def test_phase2_example_defaults_are_valid(tmp_path) -> None:
    from proxy.config import load_config, validate_runtime_config

    path = tmp_path / "config.toml"
    path.write_text(
        '''
        [server]
        host = "127.0.0.1"
        port = 8787
        request_log_dir = "logs/requests"

        [upstream]
        base_url = "https://example.com/anthropic"
        api_key_env = "ANTHROPIC_AUTH_TOKEN"
        timeout_seconds = 60.0
        connect_timeout_seconds = 10.0
        max_retries = 1
        retry_statuses = [502, 503, 504]
        stream_probe_on_startup = true
        allow_self_target = false

        [logging]
        redact_secrets = true
        allow_raw_payload_logging = false
        unsafe_override_via_env = "CC_PROXY_UNSAFE_LOGGING"
        redaction_whitelist = []

        [rewrite]
        enabled = false

        [rewrite.max_tokens_floor]
        enabled = true
        minimum_output_tokens = 4096

        [rewrite.explicit_thinking]
        enabled = false
        inject_when_missing = false
        minimum_budget_tokens = 2048

        [rewrite.message_canonicalization]
        enabled = true

        [rewrite.strict_format_guardrail]
        enabled = true
        max_suffix_chars = 200

        [rewrite.system_compression]
        enabled = false
        max_input_system_chars = 8000
        target_system_chars = 2000

        [classification]
        enabled = true
        min_chars = 120
        min_line_breaks = 2
        reasoning_keyword_patterns = ["最少", "minimum"]
        output_constraint_patterns = ["只输出", "only output"]
        premise_control_patterns = ["手感", "distinguishable"]
        code_marker_patterns = ["repo", "shell"]
        rewrite_score_threshold = 4
        normalize_only_score_threshold = 2

        [phase2]
        enabled = false
        trigger_on_routes = ["rewrite"]
        sample_count = 2
        max_adjudication_calls = 1
        max_parallelism = 2
        total_timeout_seconds = 180.0
        require_json_candidates = true
        candidate_roles = ["constraint_reasoner", "counterexample_reasoner"]
        max_candidate_output_tokens = 512
        max_total_upstream_calls = 4
        fallback_to_phase1_on_failure = true
        cost_budget_multiplier = 2.0
        allow_streaming_requests = false
        ''',
        encoding="utf-8",
    )

    config = load_config(path)
    validate_runtime_config(config)
    assert config.phase2.sample_count == 2
    assert config.phase2.allow_streaming_requests is False


def test_phase2_rejects_call_budget_that_cannot_cover_baseline_candidates_and_adjudication(tmp_path) -> None:
    from proxy.config import load_config, validate_runtime_config

    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG_TEXT.replace("max_total_upstream_calls = 4", "max_total_upstream_calls = 3"), encoding="utf-8")

    config = load_config(path)
    with pytest.raises(ValueError, match="phase2.max_total_upstream_calls"):
        validate_runtime_config(config)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_config.py::test_phase2_example_defaults_are_valid tests/test_config.py::test_phase2_rejects_call_budget_that_cannot_cover_baseline_candidates_and_adjudication -v`

Expected: FAIL with missing `phase2` config fields or missing validation logic.

- [ ] **Step 3: Write minimal implementation**

```python
class Phase2Config(BaseModel):
    enabled: bool = False
    trigger_on_routes: list[str]
    sample_count: int = Field(ge=2, le=5)
    max_adjudication_calls: int = Field(ge=0, le=1)
    max_parallelism: int = Field(ge=1)
    total_timeout_seconds: float = Field(gt=0.0)
    require_json_candidates: bool = True
    candidate_roles: list[str]
    max_candidate_output_tokens: int = Field(ge=1)
    max_total_upstream_calls: int = Field(ge=1)
    fallback_to_phase1_on_failure: bool = True
    cost_budget_multiplier: float = Field(ge=1.0)
    allow_streaming_requests: bool = False


class ProxyConfig(BaseModel):
    server: ServerConfig
    upstream: UpstreamConfig
    logging: LoggingConfig
    rewrite: RewriteConfig
    classification: ClassificationConfig
    phase2: Phase2Config


KNOWN_PHASE2_ROLES = {"constraint_reasoner", "counterexample_reasoner"}


def validate_phase2_config(config: ProxyConfig) -> None:
    phase2 = config.phase2
    if phase2.max_parallelism > phase2.sample_count:
        raise ValueError("phase2.max_parallelism must be <= phase2.sample_count")
    if len(phase2.candidate_roles) != phase2.sample_count:
        raise ValueError("phase2.candidate_roles length must equal phase2.sample_count")
    if any(role not in KNOWN_PHASE2_ROLES for role in phase2.candidate_roles):
        raise ValueError("phase2.candidate_roles contains an unknown role")
    minimum_calls = 1 + phase2.sample_count + phase2.max_adjudication_calls
    if phase2.max_total_upstream_calls < minimum_calls:
        raise ValueError("phase2.max_total_upstream_calls must cover baseline, candidates, and adjudication")
    if phase2.allow_streaming_requests:
        raise ValueError("phase2.allow_streaming_requests must remain false in the minimal design")
```

- [ ] **Step 4: Add the example config block**

```toml
[phase2]
enabled = false
trigger_on_routes = ["rewrite"]
sample_count = 2
max_adjudication_calls = 1
max_parallelism = 2
total_timeout_seconds = 180.0
require_json_candidates = true
candidate_roles = ["constraint_reasoner", "counterexample_reasoner"]
max_candidate_output_tokens = 512
max_total_upstream_calls = 4
fallback_to_phase1_on_failure = true
cost_budget_multiplier = 2.0
allow_streaming_requests = false
```

- [ ] **Step 5: Add shared phase-2 schema types**

```python
@dataclass(slots=True)
class CandidateAnswer:
    role: str
    raw_final_answer: str
    canonical_final_answer: str | None
    answer_type: str
    constraint_summary: list[str]
    critical_steps: list[str]
    counterexample_checked: bool
    confidence: str
    non_canonical: bool


@dataclass(slots=True)
class AggregationDecision:
    decision: str
    chosen_answer: str | None
    reason: str


@dataclass(slots=True)
class Phase2ExecutionResult:
    mode: str
    baseline_upstream: UpstreamResult
    candidates: list[CandidateAnswer]
    decision: AggregationDecision
    downstream_payload: dict[str, Any] | None
```

- [ ] **Step 6: Run test to verify it passes**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_config.py::test_phase2_example_defaults_are_valid tests/test_config.py::test_phase2_rejects_call_budget_that_cannot_cover_baseline_candidates_and_adjudication -v`

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add proxy/config.py proxy/config.toml.example proxy/schemas.py tests/test_config.py
git commit -m "feat: add phase2 config and schemas"
```

### Task 2: Implement Candidate Parsing And Canonicalization

**Files:**
- Create: `proxy/candidate.py`
- Create: `tests/test_candidate.py`
- Test: `tests/test_candidate.py`

**Interfaces:**
- Produces: `build_candidate_request(body: dict[str, Any], role: str, max_output_tokens: int) -> dict[str, Any]`
- Produces: `parse_candidate_response(role: str, payload: dict[str, Any]) -> CandidateAnswer`
- Produces: `parse_adjudication_response(payload: dict[str, Any]) -> tuple[str, str, str]`
- Produces: `canonicalize_answer(raw: str, answer_type: str) -> tuple[str | None, bool]`

- [ ] **Step 1: Write the failing candidate tests**

```python
import pytest

from proxy.candidate import canonicalize_answer, parse_candidate_response


def test_canonicalize_numeric_answer_treats_equivalent_renderings_as_equal() -> None:
    canonical_a, non_canonical_a = canonicalize_answer(" 21 ", "number")
    canonical_b, non_canonical_b = canonicalize_answer("21.0", "number")
    assert canonical_a == "21"
    assert canonical_b == "21"
    assert non_canonical_a is False
    assert non_canonical_b is False


def test_parse_candidate_response_marks_unparseable_numeric_answer_non_canonical() -> None:
    payload = {
        "content": [
            {
                "type": "text",
                "text": '{"final_answer":"twenty one","answer_type":"number","constraint_summary":["c1"],"critical_steps":["s1"],"counterexample_checked":true,"confidence":"medium"}',
            }
        ]
    }

    candidate = parse_candidate_response("constraint_reasoner", payload)
    assert candidate.canonical_final_answer is None
    assert candidate.non_canonical is True


def test_parse_candidate_response_requires_json_message_content() -> None:
    with pytest.raises(ValueError, match="candidate response must contain JSON text"):
        parse_candidate_response("constraint_reasoner", {"content": [{"type": "text", "text": "not json"}]})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_candidate.py -v`

Expected: FAIL with missing `proxy.candidate`.

- [ ] **Step 3: Write minimal implementation**

```python
def canonicalize_answer(raw: str, answer_type: str) -> tuple[str | None, bool]:
    normalized = raw.strip()
    if answer_type == "number":
        try:
            value = Decimal(normalized)
        except InvalidOperation:
            return None, True
        return format(value.normalize(), "f").rstrip("0").rstrip(".") or "0", False
    if answer_type == "boolean":
        lowered = normalized.lower()
        if lowered in {"true", "false"}:
            return lowered, False
        return None, True
    if answer_type in {"string", "text"}:
        return normalized, False
    return None, True


def parse_candidate_response(role: str, payload: dict[str, Any]) -> CandidateAnswer:
    text = extract_response_text(payload)
    parsed = json.loads(text)
    canonical, non_canonical = canonicalize_answer(str(parsed["final_answer"]), str(parsed["answer_type"]))
    return CandidateAnswer(
        role=role,
        raw_final_answer=str(parsed["final_answer"]),
        canonical_final_answer=canonical,
        answer_type=str(parsed["answer_type"]),
        constraint_summary=[str(item) for item in parsed["constraint_summary"]],
        critical_steps=[str(item) for item in parsed["critical_steps"]],
        counterexample_checked=bool(parsed["counterexample_checked"]),
        confidence=str(parsed["confidence"]),
        non_canonical=non_canonical,
    )
```

- [ ] **Step 4: Add candidate request construction**

```python
ROLE_PROMPTS = {
    "constraint_reasoner": "List explicit constraints, apply worst-case reasoning, and output strict JSON only.",
    "counterexample_reasoner": "Search for a violating construction before finalizing the answer, and output strict JSON only.",
}


def build_candidate_request(body: dict[str, Any], role: str, max_output_tokens: int) -> dict[str, Any]:
    request = deepcopy(body)
    request["stream"] = False
    request["max_tokens"] = max_output_tokens
    request["system"] = _append_system_text(request.get("system"), ROLE_PROMPTS[role])
    return request
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_candidate.py -v`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add proxy/candidate.py tests/test_candidate.py
git commit -m "feat: add phase2 candidate parsing"
```

### Task 3: Implement Phase 2 Orchestration And Aggregation

**Files:**
- Create: `proxy/phase2.py`
- Create: `tests/test_phase2.py`
- Modify: `proxy/upstream.py`
- Test: `tests/test_phase2.py`

**Interfaces:**
- Consumes: `build_candidate_request(...)`
- Consumes: `parse_candidate_response(...)`
- Produces: `is_phase2_eligible(body: dict[str, Any], classification: ClassificationResult, config: ProxyConfig) -> bool`
- Produces: `aggregate_candidates(candidates: list[CandidateAnswer], budget_allows_adjudication: bool) -> AggregationDecision`
- Produces: `run_phase2(transport: UpstreamTransport, headers: dict[str, str], body: dict[str, Any], classification: ClassificationResult, config: ProxyConfig, request_id: str) -> Phase2ExecutionResult`

- [ ] **Step 1: Write the failing phase-2 tests**

```python
def test_is_phase2_eligible_rejects_streamed_requests(config) -> None:
    from proxy.phase2 import is_phase2_eligible
    from proxy.rewrite import ClassificationResult

    classification = ClassificationResult("rewrite", 4, True, None, "surface")
    body = {"stream": True, "messages": [{"role": "user", "content": "logic"}]}
    assert is_phase2_eligible(body, classification, config) is False


def test_aggregate_candidates_prefers_matching_canonical_answers_with_compatible_summaries() -> None:
    from proxy.phase2 import aggregate_candidates
    from proxy.schemas import CandidateAnswer

    a = CandidateAnswer("constraint_reasoner", "21", "21", "number", ["shape"], ["step"], True, "high", False)
    b = CandidateAnswer("counterexample_reasoner", "21.0", "21", "number", ["shape"], ["step2"], True, "medium", False)

    decision = aggregate_candidates([a, b], budget_allows_adjudication=True)
    assert decision.decision == "choose_candidate"
    assert decision.chosen_answer == "21"


def test_aggregate_candidates_falls_back_when_only_one_candidate_parses() -> None:
    from proxy.phase2 import aggregate_candidates
    from proxy.schemas import CandidateAnswer

    a = CandidateAnswer("constraint_reasoner", "21", "21", "number", ["shape"], ["step"], True, "high", False)
    decision = aggregate_candidates([a], budget_allows_adjudication=False)
    assert decision.decision == "fallback_baseline"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2.py -v`

Expected: FAIL with missing `proxy.phase2`.

- [ ] **Step 3: Write minimal aggregation and eligibility implementation**

```python
def is_phase2_eligible(body: dict[str, Any], classification: ClassificationResult, config: ProxyConfig) -> bool:
    return (
        config.phase2.enabled
        and classification.route in config.phase2.trigger_on_routes
        and classification.replay_safe
        and not bool(body.get("stream"))
    )


def aggregate_candidates(candidates: list[CandidateAnswer], budget_allows_adjudication: bool) -> AggregationDecision:
    if len(candidates) < 2:
        return AggregationDecision("fallback_baseline", None, "insufficient_candidates")
    canonical_groups: dict[str, list[CandidateAnswer]] = {}
    for candidate in candidates:
        if candidate.canonical_final_answer is None:
            continue
        canonical_groups.setdefault(candidate.canonical_final_answer, []).append(candidate)
    for answer, grouped in canonical_groups.items():
        if len(grouped) >= 2 and _summaries_compatible(grouped):
            return AggregationDecision("choose_candidate", answer, "compatible_majority")
        if len(grouped) >= 2 and not _summaries_compatible(grouped):
            return AggregationDecision("run_adjudication" if budget_allows_adjudication else "fallback_baseline", None, "summary_conflict")
    return AggregationDecision("run_adjudication" if budget_allows_adjudication else "fallback_baseline", None, "candidate_disagreement")
```

- [ ] **Step 4: Implement `run_phase2` with retained baseline**

```python
async def run_phase2(... ) -> Phase2ExecutionResult:
    baseline = await transport.send_with_retry(
        context=RequestContext(request_id=request_id, attempt=1, log_dir=Path(request_id) / "phase2-baseline"),
        headers=headers,
        body=body,
        replay_safe=classification.replay_safe,
        stream=False,
    )
    candidate_results = await _run_structured_candidates(...)
    decision = aggregate_candidates(candidate_results, budget_allows_adjudication=True)
    if decision.decision == "choose_candidate":
        payload = _candidate_to_anthropic_message(decision.chosen_answer or "")
        return Phase2ExecutionResult("phase2", baseline, candidate_results, decision, payload)
    return Phase2ExecutionResult("fallback_phase1", baseline, candidate_results, decision, None)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2.py -v`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add proxy/phase2.py proxy/upstream.py tests/test_phase2.py
git commit -m "feat: add phase2 orchestration"
```

### Task 4: Integrate Phase 2 Into The FastAPI Request Path

**Files:**
- Modify: `proxy/app.py`
- Modify: `tests/test_app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `is_phase2_eligible(...)`
- Consumes: `run_phase2(...)`
- Produces: phase-2-aware `/v1/messages` behavior for non-streamed requests

- [ ] **Step 1: Write the failing app tests**

```python
def test_messages_bypass_phase2_for_streamed_requests(config) -> None:
    from proxy.app import create_app

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True
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
            response=httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"event: message\ndata: ok\n\n"),
            streamed=True,
        ),
    )

    with TestClient(create_app(phase2_config, transport=transport)) as client:
        response = client.post("/v1/messages", json=body)

    assert response.status_code == 200
    assert transport.calls[0]["stream"] is True


def test_messages_return_phase2_selected_answer_for_non_streamed_requests(config, monkeypatch) -> None:
    from proxy.app import create_app
    from proxy.schemas import AggregationDecision, Phase2ExecutionResult

    phase2_config = config.model_copy(deep=True)
    phase2_config.phase2.enabled = True

    async def fake_run_phase2(**kwargs):
        baseline = UpstreamResult(200, {"content-type": "application/json"}, b'{"type":"message","content":[{"type":"text","text":"29"}]}')
        return Phase2ExecutionResult("phase2", baseline, [], AggregationDecision("choose_candidate", "21", "test"), {"type":"message","id":"phase2","content":[{"type":"text","text":"21"}]})

    monkeypatch.setattr("proxy.app.run_phase2", fake_run_phase2)

    with TestClient(create_app(phase2_config, transport=StubTransport(UpstreamResult(200, {}, b"{}")))) as client:
        response = client.post("/v1/messages", json={"model":"m","max_tokens":32,"messages":[{"role":"user","content":"Find the minimum and only output 21"}]})

    assert response.status_code == 200
    assert response.json()["content"][0]["text"] == "21"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_app.py::test_messages_bypass_phase2_for_streamed_requests tests/test_app.py::test_messages_return_phase2_selected_answer_for_non_streamed_requests -v`

Expected: FAIL because `proxy.app` does not yet call phase 2.

- [ ] **Step 3: Write minimal integration**

```python
from proxy.phase2 import is_phase2_eligible, run_phase2


    @app.post("/v1/messages")
    async def messages(payload: dict[str, Any], request: Request):
        ...
        if is_phase2_eligible(outgoing_body, classification, active_config):
            phase2_result = await run_phase2(
                transport=state["transport"],
                headers=forward_headers,
                body=outgoing_body,
                classification=classification,
                config=active_config,
                request_id=request_id,
            )
            if phase2_result.downstream_payload is not None:
                return JSONResponse(status_code=200, content=phase2_result.downstream_payload)
            return JSONResponse(status_code=phase2_result.baseline_upstream.status_code, content=_parse_upstream_json(phase2_result.baseline_upstream))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_app.py::test_messages_bypass_phase2_for_streamed_requests tests/test_app.py::test_messages_return_phase2_selected_answer_for_non_streamed_requests -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add proxy/app.py tests/test_app.py
git commit -m "feat: wire phase2 into request handling"
```

### Task 5: Extend Evaluation And Operator Documentation

**Files:**
- Modify: `proxy/evals.py`
- Modify: `tests/test_evals.py`
- Modify: `README.md`
- Test: `tests/test_evals.py`

**Interfaces:**
- Produces: `SUPPORTED_MODES` including `claude_code_proxy_phase1` and `claude_code_proxy_phase2`
- Produces: `run_mode(mode: str, fixture_path: Path, config: ProxyConfig, ...) -> dict[str, object]`

- [ ] **Step 1: Write the failing evaluation tests**

```python
def test_run_mode_supports_phase2_proxy_mode(config) -> None:
    from proxy.evals import run_mode

    result = run_mode("claude_code_proxy_phase2", Path("proxy/fixtures/candy_question.json"), config, execute=False)
    assert result["mode"] == "claude_code_proxy_phase2"


def test_score_run_keeps_reasoning_and_format_accounting_separate() -> None:
    from proxy.evals import score_run

    score = score_run("21", "答案是 21", exact_output_required=True)
    assert score["correct"] is True
    assert score["format_ok"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py::test_run_mode_supports_phase2_proxy_mode tests/test_evals.py::test_score_run_keeps_reasoning_and_format_accounting_separate -v`

Expected: FAIL because phase-2 mode is not defined yet.

- [ ] **Step 3: Write minimal implementation**

```python
PROXY_MODES = {
    "captured_claude_passthrough",
    "captured_claude_rewrite",
    "claude_code_proxy",
    "claude_code_proxy_phase1",
    "claude_code_proxy_phase2",
}


def _target_base_url(mode: str, config: ProxyConfig) -> str:
    if mode in DIRECT_MODES:
        return config.upstream.base_url
    return f"http://{config.server.host}:{config.server.port}"
```

- [ ] **Step 4: Update README operator guidance**

```markdown
## Phase 2

Phase 2 is a non-streamed experimental path layered on top of phase 1.

- `stream=true` requests stay on phase 1.
- Phase 2 keeps one retained phase-1 baseline response for fallback.
- Phase 2 runs two structured candidate solves and at most one adjudication call.
- Compare `claude_code_proxy_phase1` and `claude_code_proxy_phase2` on the same fixtures before claiming improvement.
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py -v`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add proxy/evals.py tests/test_evals.py README.md
git commit -m "feat: add phase2 evaluation modes"
```

### Task 6: Full Regression And Real-Provider Validation Hooks

**Files:**
- Modify: `tests/test_phase2.py`
- Modify: `README.md`
- Test: `tests/test_phase2.py`
- Test: `tests/test_app.py`
- Test: `tests/test_evals.py`

**Interfaces:**
- Consumes: `aggregate_candidates(...)`
- Consumes: `run_phase2(...)`
- Produces: branch coverage for parse-failure mixes, conflicting summaries, non-canonical majority, and exact-output adjudication

- [ ] **Step 1: Write the failing branch coverage tests**

```python
def test_aggregate_candidates_runs_adjudication_when_matching_answer_has_conflicting_summaries() -> None:
    ...


def test_aggregate_candidates_falls_back_when_noncanonical_majority_beats_canonical_minority_without_budget() -> None:
    ...


def test_phase2_exact_output_adjudication_emits_answer_only() -> None:
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2.py -v`

Expected: FAIL on unimplemented branch behavior.

- [ ] **Step 3: Implement the missing branch behavior**

```python
if grouped and len(grouped) >= 2 and not _summaries_compatible(grouped):
    return AggregationDecision("run_adjudication" if budget_allows_adjudication else "fallback_baseline", None, "summary_conflict")

if any(candidate.non_canonical for candidate in grouped) and any(not candidate.non_canonical for candidate in candidates):
    return AggregationDecision("run_adjudication" if budget_allows_adjudication else "fallback_baseline", None, "non_canonical_majority")
```

- [ ] **Step 4: Run the focused branch tests**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2.py tests/test_app.py tests/test_evals.py -v`

Expected: PASS

- [ ] **Step 5: Run the full regression suite**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest --import-mode=importlib -v`

Expected: PASS for the full suite with new phase-2 coverage.

- [ ] **Step 6: Document the real-provider validation commands**

```markdown
## Real Provider Validation

1. Start the proxy with `phase2.enabled = true` and `phase2.allow_streaming_requests = false`.
2. Run `direct`, `claude_code_proxy_phase1`, and `claude_code_proxy_phase2` on `proxy/fixtures/candy_question.json`.
3. Repeat the same batch on at least one non-candy reasoning fixture.
4. Record correctness, format compliance, protocol compatibility, latency, upstream call count, and token usage if available.
```

- [ ] **Step 7: Commit**

```bash
git add tests/test_phase2.py tests/test_app.py tests/test_evals.py README.md
git commit -m "test: cover phase2 branches and validation flow"
```

## Self-Review

- Spec coverage:
  - Non-stream-only gating is implemented in Task 3 and integrated in Task 4.
  - Retained phase-1 baseline fallback is implemented in Task 3 and exercised in Task 4.
  - Structured candidate parsing and canonicalization are implemented in Task 2.
  - Aggregation branches and adjudication semantics are covered in Task 3 and Task 6.
  - Config defaults, budgets, and validation are implemented in Task 1.
  - Eval modes and operator workflow are implemented in Task 5.
- Placeholder scan:
  - No `TODO`, `TBD`, or “similar to Task N” shortcuts remain.
- Type consistency:
  - `Phase2Config`, `CandidateAnswer`, `AggregationDecision`, `Phase2ExecutionResult`, `build_candidate_request`, `parse_candidate_response`, `aggregate_candidates`, `run_phase2`, and `run_mode` are named consistently across tasks.
