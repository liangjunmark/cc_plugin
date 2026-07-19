# Phase 2 Final Branch Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the remaining final-branch blockers around tool-flow safety, 2xx success-envelope validation, phase-2 subcall validity, evaluation correctness, and low-risk config/parser gaps.

**Architecture:** Keep the current phase-1/phase-2 split and tighten the boundary rules instead of redesigning orchestration. The fixes stay local to eligibility, response validation, candidate/adjudication request construction, eval scoring, and config validation, with regression tests for each bug class.

**Tech Stack:** Python 3.12, FastAPI, httpx, pytest

## Global Constraints

- Accept Anthropic-compatible `/v1/messages` traffic and preserve Claude Code / Codex agent behavior.
- Phase 2 must never intercept tool-enabled or tool-state-bearing requests.
- Return an Anthropic-compatible success shape or Anthropic-compatible error envelope whenever no downstream bytes have been emitted yet.
- A successful downstream `200` response must be an Anthropic message envelope, not just any JSON object.
- Once downstream streaming has begun, never replace a partial stream with a brand-new full error envelope.
- The minimal phase-2 path remains non-streamed only.
- Candidate/adjudication subcalls must never send structurally invalid `thinking`/`max_tokens` combinations.
- HTTP statuses outside `200 <= status < 300` must not be treated as successful baseline/candidate/adjudication solves.
- Evaluation must not count protocol-invalid responses as correct or non-regressed.
- Public config switches that the implementation does not support must not be silently accepted.

---

### Task 1: Block Tool-Enabled Requests From Phase 2

**Files:**
- Modify: `proxy/rewrite.py`
- Modify: `proxy/phase2.py`
- Modify: `tests/test_rewrite.py`
- Modify: `tests/test_phase2.py`
- Modify: `tests/test_app.py`

**Interfaces:**
- Consumes: `is_replay_safe(body: dict[str, Any]) -> tuple[bool, str | None]`
- Consumes: `is_phase2_eligible(body: dict[str, Any], classification: ClassificationResult, config: ProxyConfig) -> bool`
- Produces: tool-aware passthrough gating for both classification and explicit phase-2 selection

- [ ] **Step 1: Write the failing tests**

```python
def test_replay_safe_rejects_top_level_tools() -> None:
    body = {
        "model": "m",
        "max_tokens": 512,
        "tools": [{"name": "shell", "input_schema": {"type": "object"}}],
        "messages": [{"role": "user", "content": "Only answer 21"}],
    }

    replay_safe, reason = is_replay_safe(body)

    assert replay_safe is False
    assert reason == "tooling_declared"


def test_is_phase2_eligible_rejects_tool_enabled_requests(config) -> None:
    body = {
        "model": "m",
        "max_tokens": 512,
        "tools": [{"name": "shell", "input_schema": {"type": "object"}}],
        "messages": [{"role": "user", "content": "Only answer 21"}],
    }
    classification = classify_request(body, config)

    assert is_phase2_eligible(body, classification, config) is False


def test_messages_phase2_selector_bypasses_phase2_when_tools_are_declared(config, monkeypatch) -> None:
    ...
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_rewrite.py tests/test_phase2.py tests/test_app.py -k "tools or tooling_declared" -v`

Expected: FAIL because the current request path still treats top-level `tools` as replay-safe and phase-2 eligible.

- [ ] **Step 3: Implement the minimal gate**

```python
def is_replay_safe(body: dict[str, Any]) -> tuple[bool, str | None]:
    if body.get("tools"):
        return False, "tooling_declared"
    if body.get("tool_choice") not in (None, "auto"):
        return False, "tooling_declared"
    ...


def is_phase2_eligible(...):
    return (
        ...
        and not bool(body.get("tools"))
        and body.get("tool_choice") in (None, "auto")
        ...
    )
```

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_rewrite.py tests/test_phase2.py tests/test_app.py -k "tools or tooling_declared" -v`

Expected: PASS

### Task 2: Validate 2xx Success Envelopes Before Returning Downstream

**Files:**
- Modify: `proxy/app.py`
- Modify: `tests/test_app.py`

**Interfaces:**
- Consumes: `_parse_upstream_json(upstream: UpstreamResult, normalize_error: bool = False) -> tuple[int, dict[str, Any]]`
- Produces: strict `200`-success validation for Anthropic message envelopes

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_upstream_json_rejects_empty_200_object_payload() -> None:
    status_code, payload = _parse_upstream_json(UpstreamResult(200, {}, b"{}"))

    assert status_code == 502
    assert payload["error"]["message"] == "upstream returned an invalid success payload"


def test_parse_upstream_json_rejects_invalid_200_message_shape() -> None:
    status_code, payload = _parse_upstream_json(
        UpstreamResult(200, {}, b'{"type":"message","content":"invalid"}'),
    )

    assert status_code == 502
    assert payload["error"]["message"] == "upstream returned an invalid success payload"
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_app.py -k "invalid_success_payload or rejects_empty_200_object" -v`

Expected: FAIL because any JSON object currently passes through as a successful downstream message.

- [ ] **Step 3: Implement success-envelope validation**

```python
def _parse_upstream_json(...):
    ...
    if isinstance(parsed, dict):
        if 200 <= upstream.status_code < 300 and not _is_anthropic_message(parsed):
            return 502, anthropic_error(502, "upstream returned an invalid success payload")
        ...


def _is_anthropic_message(payload: dict[str, Any]) -> bool:
    content = payload.get("content")
    return (
        payload.get("type") == "message"
        and isinstance(content, list)
        and all(isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str) for item in content)
    )
```

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_app.py -k "invalid_success_payload or rejects_empty_200_object" -v`

Expected: PASS

### Task 3: Make Phase 2 Subcalls Valid And Treat Only 2xx As Successful

**Files:**
- Modify: `proxy/candidate.py`
- Modify: `proxy/phase2.py`
- Modify: `tests/test_candidate.py`
- Modify: `tests/test_phase2.py`

**Interfaces:**
- Consumes: `build_candidate_request(...) -> dict[str, Any]`
- Consumes: `_build_adjudication_request(...) -> dict[str, Any]`
- Produces: valid candidate/adjudication request bodies and strict success-status handling for baseline/candidates/adjudication

- [ ] **Step 1: Write the failing tests**

```python
def test_build_candidate_request_drops_invalid_thinking_budget_when_max_tokens_is_lower() -> None:
    request = build_candidate_request(
        {
            "model": "m",
            "max_tokens": 4096,
            "thinking": {"type": "enabled", "budget_tokens": 2048},
            "messages": [{"role": "user", "content": "Only answer 21"}],
        },
        "constraint_reasoner",
        512,
    )

    assert request["max_tokens"] == 512
    assert "thinking" not in request


def test_run_phase2_falls_back_when_candidate_returns_redirect(config) -> None:
    ...


def test_run_phase2_falls_back_when_baseline_returns_redirect(config) -> None:
    ...
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_candidate.py tests/test_phase2.py -k "thinking_budget or redirect" -v`

Expected: FAIL because candidate/adjudication bodies preserve invalid `thinking`, and phase-2 currently treats `302` as a successful solve.

- [ ] **Step 3: Implement the validity/status fixes**

```python
def build_candidate_request(body: dict[str, Any], role: str, max_output_tokens: int) -> dict[str, Any]:
    request = deepcopy(body)
    request["stream"] = False
    request["max_tokens"] = max_output_tokens
    thinking = request.get("thinking")
    if isinstance(thinking, dict):
        budget = thinking.get("budget_tokens")
        if not isinstance(budget, int) or budget >= max_output_tokens:
            request.pop("thinking", None)
    ...


def _build_adjudication_request(...):
    request["max_tokens"] = max_output_tokens
    if isinstance(request.get("thinking"), dict) and request["thinking"].get("budget_tokens") >= max_output_tokens:
        request.pop("thinking", None)
    ...


if not 200 <= baseline.status_code < 300:
    return _fallback_result(...)


if not 200 <= result.status_code < 300:
    return None
```

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_candidate.py tests/test_phase2.py -k "thinking_budget or redirect" -v`

Expected: PASS

### Task 4: Prevent Eval Scoring From Certifying Protocol Failures

**Files:**
- Modify: `proxy/evals.py`
- Modify: `tests/test_evals.py`

**Interfaces:**
- Consumes: `score_run(...) -> dict[str, bool]`
- Consumes: `summarize_regressions(...) -> dict[str, object]`
- Produces: protocol-aware correctness/format scoring and regression summaries

- [ ] **Step 1: Write the failing tests**

```python
def test_score_run_marks_protocol_invalid_exact_answer_as_incorrect() -> None:
    score = score_run(
        expected_answer="21",
        response_text="21",
        exact_output_required=True,
        status_code=503,
        response_payload={"type": "error", "error": {"type": "api_error", "message": "21"}},
    )

    assert score == {"correct": False, "format_ok": False, "protocol_ok": False}


def test_regression_summary_flags_protocol_only_drop_as_material() -> None:
    summary = summarize_regressions(
        [{"correct": True, "format_ok": True, "protocol_ok": True}],
        [{"correct": True, "format_ok": True, "protocol_ok": False}],
    )

    assert summary["material_regression"] is True
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py -k "protocol_invalid_exact_answer or protocol_only_drop" -v`

Expected: FAIL because evals currently let protocol-invalid responses score as correct and non-regressed.

- [ ] **Step 3: Implement protocol-aware scoring**

```python
def score_run(...):
    protocol_ok = _is_anthropic_compatible_payload(status_code, response_payload)
    correct = protocol_ok and extracted == expected_answer
    format_ok = protocol_ok and (stripped == expected_answer if exact_output_required else bool(stripped))
    return {...}


def summarize_regressions(...):
    correctness_drop = candidate_correct < baseline_correct - 0.10
    format_drop = candidate_format < baseline_format - 0.10
    protocol_drop = candidate_protocol < baseline_protocol - 0.10
    return {..., "material_regression": correctness_drop or format_drop or protocol_drop}
```

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py -k "protocol_invalid_exact_answer or protocol_only_drop" -v`

Expected: PASS

### Task 5: Close Low-Risk Parser And Config Gaps

**Files:**
- Modify: `proxy/candidate.py`
- Modify: `proxy/config.py`
- Modify: `proxy/config.toml.example`
- Modify: `tests/test_candidate.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: `parse_candidate_response(...) -> CandidateAnswer`
- Consumes: `validate_phase2_config(config: ProxyConfig) -> None`
- Produces: rejection of empty text/string candidate answers and explicit validation for unsupported config switches

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_candidate_response_rejects_empty_text_answer() -> None:
    ...


def test_phase2_rejects_disabling_strict_json_candidates() -> None:
    ...


def test_phase2_rejects_disabling_phase1_failure_fallback() -> None:
    ...
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_candidate.py tests/test_config.py -k "empty_text_answer or strict_json_candidates or failure_fallback" -v`

Expected: FAIL because whitespace-only answers are still accepted, and unsupported config switches are validated but silently ignored.

- [ ] **Step 3: Implement the minimal validation**

```python
def parse_candidate_response(...):
    raw_final_answer = _require_string(parsed["final_answer"], "final_answer")
    if answer_type in {"string", "text"} and not raw_final_answer.strip():
        raise ValueError("final_answer must not be empty")
    ...


def validate_phase2_config(config: ProxyConfig) -> None:
    ...
    if not phase2.require_json_candidates:
        raise ValueError("phase2.require_json_candidates must remain true in the minimal design")
    if not phase2.fallback_to_phase1_on_failure:
        raise ValueError("phase2.fallback_to_phase1_on_failure must remain true in the minimal design")
```

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_candidate.py tests/test_config.py -k "empty_text_answer or strict_json_candidates or failure_fallback" -v`

Expected: PASS

### Task 6: Re-Verify And Re-Review The Branch

**Files:**
- Modify: `.superpowers/sdd/progress.md`
- Modify: `.superpowers/sdd/task-6-report.md` (if shared verification evidence changes)
- Modify: `.superpowers/sdd/phase2-final-fix-task-3-report.md` only if cross-task evidence needs a note

**Interfaces:**
- Consumes: current worktree after Tasks 1-5
- Produces: fresh verification evidence and updated progress ledger

- [ ] **Step 1: Run the targeted suites**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_rewrite.py tests/test_phase2.py tests/test_app.py tests/test_candidate.py tests/test_config.py tests/test_evals.py -v`

Expected: PASS

- [ ] **Step 2: Run the full suite**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest --import-mode=importlib -v`

Expected: PASS with a fresh total pass count and only the existing Starlette/httpx warning.

- [ ] **Step 3: Update the shared ledger**

```markdown
- Record the final-review fix wave completion in `.superpowers/sdd/progress.md`.
- Append the fresh full-suite result to `.superpowers/sdd/task-6-report.md` only if that report is still the shared verification source for the branch.
```

- [ ] **Step 4: Run one more broad branch review**

Run a final reviewer against the current phase-2 implementation scope and require findings-only output.

Expected: no remaining branch blockers in scope.

## Self-Review

- Spec coverage:
  - Tool-flow safety is covered in Task 1.
  - 2xx success-envelope validation is covered in Task 2.
  - Candidate/adjudication validity and 2xx-only success handling are covered in Task 3.
  - Eval hardening is covered in Task 4.
  - Low-risk parser/config cleanup is covered in Task 5.
  - Fresh verification and final review closure are covered in Task 6.
- Placeholder scan:
  - No `TODO`, `TBD`, or unresolved task placeholders remain.
- Type consistency:
  - The plan keeps the touched interfaces local to `is_replay_safe`, `is_phase2_eligible`, `_parse_upstream_json`, `build_candidate_request`, `_build_adjudication_request`, `score_run`, and `validate_phase2_config`.
