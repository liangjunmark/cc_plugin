# Phase 2 Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the phase-2 reasoning path into line with the July 17 spec and remove the final branch review blockers around candidate schema, aggregation safety, Anthropic-compatible downstream behavior, budget gating, and evaluation protocol scoring.

**Architecture:** Keep the existing phase-2 modules and tighten behavior at the existing seams instead of introducing new orchestration layers. The fixes stay concentrated in `proxy/candidate.py`, `proxy/phase2.py`, `proxy/app.py`, `proxy/evals.py`, and their tests so the non-streamed phase-2 path remains small, explicit, and bounded.

**Tech Stack:** Python 3.12, FastAPI, httpx, pytest

## Global Constraints

- Accept Anthropic-compatible `/v1/messages` traffic and preserve Claude Code / Codex agent behavior.
- Return an Anthropic-compatible success shape or Anthropic-compatible error envelope whenever no downstream bytes have been emitted yet.
- Once downstream streaming has begun, never replace a partial stream with a brand-new full error envelope.
- The minimal phase-2 path does not support inbound Anthropic SSE passthrough or partial synthetic SSE emission.
- Phase 2 remains non-streamed only.
- The retained phase-1 baseline must remain the raw fallback source rather than being coerced into candidate JSON.
- Phase 2 runs two structured candidate solves and at most one adjudication call within the configured timeout and call budget.
- Exact-output adjudication must emit answer-only downstream content.
- `phase2.cost_budget_multiplier` must be enforced, not only validated.

---

### Task 1: Tighten Candidate Schema And Aggregation Safety

**Files:**
- Modify: `proxy/candidate.py`
- Modify: `proxy/phase2.py`
- Modify: `tests/test_candidate.py`
- Modify: `tests/test_phase2.py`

**Interfaces:**
- Consumes: `build_candidate_request(body: dict[str, Any], role: str, max_output_tokens: int) -> dict[str, Any]`
- Consumes: `parse_candidate_response(role: str, payload: dict[str, Any]) -> CandidateAnswer`
- Consumes: `aggregate_candidates(candidates: list[CandidateAnswer], budget_allows_adjudication: bool) -> AggregationDecision`
- Produces: schema-explicit candidate prompts, strict list validation for candidate summaries/steps, and conflict-aware summary compatibility

- [ ] **Step 1: Write the failing candidate and aggregation tests**

```python
def test_build_candidate_request_embeds_required_json_schema() -> None:
    request = build_candidate_request(_base_request(), "constraint_reasoner", 256)
    system_text = _flatten_system_text(request["system"])
    assert '"final_answer"' in system_text
    assert '"constraint_summary"' in system_text
    assert '"critical_steps"' in system_text
    assert '"counterexample_checked"' in system_text
    assert '"confidence"' in system_text


def test_parse_candidate_response_rejects_string_constraint_summary() -> None:
    payload = _candidate_payload(
        {
            "constraint_summary": "not-a-list",
        }
    )
    with pytest.raises(ValueError, match="constraint_summary must be a list"):
        parse_candidate_response("constraint_reasoner", payload)


def test_aggregate_candidates_runs_adjudication_when_summaries_share_generic_text_but_conflict_on_key_constraint() -> None:
    first = _candidate_answer(
        canonical_final_answer="21",
        constraint_summary=["must satisfy all constraints", "shape is distinguishable by touch"],
    )
    second = _candidate_answer(
        canonical_final_answer="21",
        constraint_summary=["must satisfy all constraints", "shape is not distinguishable by touch"],
    )
    decision = aggregate_candidates([first, second], budget_allows_adjudication=True)
    assert decision.decision == "run_adjudication"
    assert decision.reason == "summary_conflict"
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_candidate.py tests/test_phase2.py -k "schema or constraint_summary or summary_conflict" -v`

Expected: FAIL because the current prompt does not include the full schema, the parser accepts string-backed list fields, and summary compatibility is overly permissive.

- [ ] **Step 3: Implement the candidate-schema and compatibility fixes**

```python
ROLE_PROMPTS = {
    "constraint_reasoner": (
        "List explicit constraints, apply worst-case reasoning, and output strict JSON only with this schema: "
        '{"final_answer":"string","answer_type":"number|string|boolean|text","constraint_summary":["string"],'
        '"critical_steps":["string"],"counterexample_checked":true,"confidence":"low|medium|high"}.'
    ),
    ...
}


def _require_string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    if any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} items must be strings")
    return value


def _summaries_compatible(candidates: list[CandidateAnswer]) -> bool:
    normalized = [_normalize_summary_items(candidate.constraint_summary) for candidate in candidates]
    common = set.intersection(*(set(items) for items in normalized))
    if not common:
        return False
    return not _has_explicit_summary_conflict(normalized)
```

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_candidate.py tests/test_phase2.py -k "schema or constraint_summary or summary_conflict" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add proxy/candidate.py proxy/phase2.py tests/test_candidate.py tests/test_phase2.py
git commit -m "fix: tighten phase2 candidate schema and aggregation"
```

### Task 2: Restore Anthropic-Compatible Phase 2 Contracts And Budget Gating

**Files:**
- Modify: `proxy/phase2.py`
- Modify: `proxy/app.py`
- Modify: `proxy/schemas.py`
- Modify: `tests/test_app.py`
- Modify: `tests/test_phase2.py`

**Interfaces:**
- Consumes: `is_phase2_eligible(body: dict[str, Any], classification: ClassificationResult, config: ProxyConfig) -> bool`
- Consumes: `run_phase2(...) -> Phase2ExecutionResult`
- Produces: enforced `cost_budget_multiplier`, Anthropic-compatible synthetic success payloads, and Anthropic-compatible error fallback when the retained baseline itself is an error

- [ ] **Step 1: Write the failing phase-2 contract tests**

```python
def test_is_phase2_eligible_rejects_requests_that_exceed_cost_budget_multiplier() -> None:
    config = _phase2_config(cost_budget_multiplier=2.0)
    body = _base_body(max_tokens=16)
    assert not is_phase2_eligible(body, _rewrite_classification(), config)


def test_messages_wraps_phase2_baseline_error_as_anthropic_error_envelope() -> None:
    response = client.post("/v1/messages", json=_eligible_request())
    assert response.status_code == 599
    assert response.json()["type"] == "error"
    assert response.json()["error"]["type"] == "api_error"


def test_phase2_selected_answer_payload_is_anthropic_compatible() -> None:
    result = await run_phase2(...)
    payload = result.downstream_payload
    assert payload["type"] == "message"
    assert payload["role"] == "assistant"
    assert "model" in payload
    assert isinstance(payload["usage"]["input_tokens"], int)
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_app.py tests/test_phase2.py -k "cost_budget_multiplier or anthropic_compatible or baseline_error" -v`

Expected: FAIL because the current phase-2 gate does not enforce estimated token cost, baseline errors are returned as raw bytes, and the synthetic success payload is incomplete.

- [ ] **Step 3: Implement the contract and gating fixes**

```python
def is_phase2_eligible(...):
    if not _within_phase2_cost_budget(body, config):
        return False
    ...


def _within_phase2_cost_budget(body: dict[str, Any], config: ProxyConfig) -> bool:
    baseline_budget = int(body.get("max_tokens", 0))
    phase2_budget = (
        config.phase2.sample_count * config.phase2.max_candidate_output_tokens
        + config.phase2.max_adjudication_calls * config.phase2.max_candidate_output_tokens
    )
    return phase2_budget <= int(baseline_budget * config.phase2.cost_budget_multiplier)


def _candidate_to_anthropic_message(answer: str, model: str) -> dict[str, Any]:
    return {
        "id": "phase2",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": answer}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


if phase2_result.downstream_payload is not None:
    return JSONResponse(status_code=200, content=phase2_result.downstream_payload)
return JSONResponse(
    status_code=phase2_result.baseline_upstream.status_code,
    content=_parse_upstream_json(phase2_result.baseline_upstream),
)
```

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_app.py tests/test_phase2.py -k "cost_budget_multiplier or anthropic_compatible or baseline_error" -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add proxy/phase2.py proxy/app.py proxy/schemas.py tests/test_app.py tests/test_phase2.py
git commit -m "fix: enforce phase2 contracts and budget gates"
```

### Task 3: Correct Eval Protocol Scoring And Re-Verify The Branch

**Files:**
- Modify: `proxy/evals.py`
- Modify: `tests/test_evals.py`
- Modify: `README.md`
- Modify: `.superpowers/sdd/task-6-report.md`

**Interfaces:**
- Consumes: `score_run(expected_answer: str, response_text: str, exact_output_required: bool) -> dict[str, bool]`
- Produces: protocol-sensitive evaluation results and refreshed verification evidence for the repaired phase-2 branch

- [ ] **Step 1: Write the failing eval tests**

```python
def test_score_run_marks_plaintext_503_as_protocol_failure() -> None:
    score = score_run(expected_answer="21", response_text="upstream failure", exact_output_required=False)
    assert score["protocol_ok"] is False


def test_run_mode_marks_non_anthropic_json_error_as_protocol_failure() -> None:
    ...
```

- [ ] **Step 2: Run the focused eval tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py -k "protocol" -v`

Expected: FAIL because protocol scoring is currently hard-coded to `True`.

- [ ] **Step 3: Implement protocol-aware scoring and doc updates**

```python
def score_run(
    expected_answer: str,
    response_text: str,
    exact_output_required: bool,
    *,
    status_code: int,
    response_payload: dict[str, Any] | None,
) -> dict[str, bool]:
    ...
    protocol_ok = _is_anthropic_compatible_payload(status_code, response_payload)
```

- [ ] **Step 4: Run the targeted eval tests**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py -v`

Expected: PASS

- [ ] **Step 5: Run the full regression suite**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest --import-mode=importlib -v`

Expected: PASS with a fresh total pass count and only the existing Starlette/httpx warning.

- [ ] **Step 6: Update the task report and operator notes**

```markdown
- Record the final blocker-fix verification run in `.superpowers/sdd/task-6-report.md`.
- Update `README.md` if the Anthropic-compatible error-envelope behavior or evaluation interpretation needs clarification.
```

- [ ] **Step 7: Commit**

```bash
git add proxy/evals.py tests/test_evals.py README.md .superpowers/sdd/task-6-report.md
git commit -m "test: verify phase2 protocol scoring and regression state"
```

## Self-Review

- Spec coverage:
  - Candidate JSON schema and typed parsing are covered in Task 1.
  - Conflict-aware aggregation is covered in Task 1.
  - Anthropic-compatible success/error shapes and cost gating are covered in Task 2.
  - Protocol-aware evaluation and fresh branch verification are covered in Task 3.
- Placeholder scan:
  - No `TODO`, `TBD`, or unresolved task references remain.
- Type consistency:
  - The plan keeps `Phase2ExecutionResult`, `AggregationDecision`, `build_candidate_request`, `parse_candidate_response`, `is_phase2_eligible`, `run_phase2`, and `score_run` as the touched interfaces.
