# Phase 2 Broad Final Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the remaining broad final-review blockers around ordinary-path error normalization, phase-2 eligibility/output narrowing, baseline validation, reasoning-artifact validation, request-validation compatibility, and phase-2 config/rewrite consistency.

**Architecture:** Keep the current proxy shape and phase-2 orchestration intact, but tighten protocol normalization and narrow the phase-2 feature surface to the exact-output reasoning lane it can actually preserve correctly. The fix wave stays local to `app`, `phase2`, `rewrite`, `candidate`, `config`, and their regression tests.

**Tech Stack:** Python 3.12, FastAPI, httpx, pytest

## Global Constraints

- The current date is Saturday, July 18, 2026.
- Accept Anthropic-compatible `/v1/messages` traffic and preserve Claude Code / Codex agent behavior.
- Return an Anthropic-compatible success shape or Anthropic-compatible error envelope whenever no downstream bytes have been emitted yet.
- Ordinary non-streamed upstream errors must be normalized the same way as phase-2 fallback errors.
- Phase 2 must remain non-streamed only and must stay out of tool, coding, patch, file-edit, and explanation-preserving flows.
- Successful phase 2 may emit answer-only output only for requests that explicitly require exact-output formatting.
- Phase 2 must not fan out from a malformed or non-Anthropic retained baseline success payload.
- Candidate reasoning artifacts must be non-empty and task-relevant enough to support compatibility checks.
- Minimal-design config toggles must either work or be rejected explicitly; impossible config ranges must not be advertised as valid.

---

### Task 1: Normalize Ordinary Errors And Inbound Validation Failures

**Files:**
- Modify: `proxy/app.py`
- Modify: `tests/test_app.py`

**Interfaces:**
- Consumes: `_parse_upstream_json(upstream: UpstreamResult, normalize_error: bool = False) -> tuple[int, dict[str, Any]]`
- Produces: consistent Anthropic error envelopes for ordinary non-phase2 errors and malformed inbound requests

- [ ] **Step 1: Write the failing tests**

```python
def test_messages_normalizes_non_phase2_json_error_body(config) -> None:
    ...


def test_messages_normalizes_non_phase2_redirect_json_body(config) -> None:
    ...


def test_messages_invalid_json_body_returns_anthropic_error_envelope(config) -> None:
    ...
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_app.py -k "non_phase2_json_error_body or redirect_json_body or invalid_json_body" -v`

Expected: FAIL because the ordinary path still returns raw JSON errors and FastAPI default request-validation responses.

- [ ] **Step 3: Implement the minimal normalization**

```python
status_code, parsed = _parse_upstream_json(upstream, normalize_error=True)


if normalize_error and not 200 <= upstream.status_code < 300 and not _is_anthropic_error(parsed):
    ...


@app.exception_handler(RequestValidationError)
async def handle_validation_error(...):
    return JSONResponse(status_code=400, content=anthropic_error(400, "invalid request payload"))
```

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_app.py -k "non_phase2_json_error_body or redirect_json_body or invalid_json_body" -v`

Expected: PASS

### Task 2: Narrow Phase 2 To Exact-Output Reasoning Requests And Validate Baselines

**Files:**
- Modify: `proxy/phase2.py`
- Modify: `proxy/rewrite.py`
- Modify: `proxy/config.toml.example`
- Modify: `tests/test_phase2.py`
- Modify: `tests/test_rewrite.py`
- Modify: `tests/test_app.py`

**Interfaces:**
- Consumes: `is_phase2_eligible(...) -> bool`
- Consumes: `run_phase2(...) -> Phase2ExecutionResult`
- Produces: exact-output-only phase-2 gating and retained-baseline validation before candidate fan-out

- [ ] **Step 1: Write the failing tests**

```python
def test_is_phase2_eligible_rejects_reasoning_request_without_exact_output_constraint(config) -> None:
    ...


def test_is_phase2_eligible_rejects_edit_or_patch_intent(config) -> None:
    ...


async def test_run_phase2_falls_back_when_baseline_200_payload_is_not_anthropic_message(config) -> None:
    ...
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2.py tests/test_rewrite.py tests/test_app.py -k "without_exact_output_constraint or edit_or_patch_intent or baseline_200_payload_is_not_anthropic_message" -v`

Expected: FAIL because phase 2 still admits explanation/file-edit flows and accepts any 2xx baseline.

- [ ] **Step 3: Implement the gate narrowing**

```python
def is_phase2_eligible(...):
    return (
        ...
        and _matches_patterns(classification.effective_prompt_surface, config.classification.output_constraint_patterns)
        and not _matches_patterns(classification.effective_prompt_surface, config.classification.code_marker_patterns)
        and _is_valid_baseline_message(...)
    )


def is_replay_safe(...):
    if re.search(r"\b(edit|modify|update|rewrite|patch|refactor)\b", text, re.IGNORECASE):
        return False, "side_effect_intent"
```

- [ ] **Step 4: Update example/default code-marker patterns**

```toml
code_marker_patterns = ["repo", "shell", "patch", "edit", "modify", "file", "\\.py\\b"]
```

- [ ] **Step 5: Run the focused tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2.py tests/test_rewrite.py tests/test_app.py -k "without_exact_output_constraint or edit_or_patch_intent or baseline_200_payload_is_not_anthropic_message" -v`

Expected: PASS

### Task 3: Tighten Candidate Reasoning Artifacts And Rewrite/Config Boundaries

**Files:**
- Modify: `proxy/candidate.py`
- Modify: `proxy/config.py`
- Modify: `proxy/config.toml.example`
- Modify: `proxy/rewrite.py`
- Modify: `tests/test_candidate.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_rewrite.py`

**Interfaces:**
- Consumes: `_require_string_list(...)`
- Consumes: `RewriteConfig`, `validate_phase2_config(...)`, `apply_rewrites(...)`
- Produces: non-empty reasoning lists, independently toggleable premise-binding rule, and internally consistent phase2 config ranges

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_candidate_response_rejects_empty_reasoning_artifacts() -> None:
    ...


def test_apply_rewrites_respects_disabled_premise_binding_guardrail(config) -> None:
    ...


def test_phase2_accepts_highest_valid_total_call_budget_configuration(tmp_path: Path) -> None:
    ...
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_candidate.py tests/test_config.py tests/test_rewrite.py -k "empty_reasoning_artifacts or premise_binding_guardrail or highest_valid_total_call_budget" -v`

Expected: FAIL because empty reasoning lists still pass, premise binding has no independent toggle, and the current config range is internally inconsistent.

- [ ] **Step 3: Implement the minimal boundary fixes**

```python
class PremiseBindingGuardrailConfig(BaseModel):
    enabled: bool = True


class RewriteConfig(BaseModel):
    ...
    premise_binding_guardrail: PremiseBindingGuardrailConfig


if config.rewrite.premise_binding_guardrail.enabled and ...:
    ...


def _require_string_list(...):
    if not value or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(...)


class Phase2Config(BaseModel):
    max_total_upstream_calls: int = Field(ge=1, le=7)
```

- [ ] **Step 4: Narrow default premise-control patterns**

```toml
premise_control_patterns = ["手感", "靠手感", "touch", "distinguishable by touch", "区分形状"]
```

- [ ] **Step 5: Run the focused tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_candidate.py tests/test_config.py tests/test_rewrite.py -k "empty_reasoning_artifacts or premise_binding_guardrail or highest_valid_total_call_budget" -v`

Expected: PASS

### Task 4: Re-Verify And Re-Review The Branch

**Files:**
- Modify: `.superpowers/sdd/progress.md`
- Modify: `.superpowers/sdd/task-6-report.md` only if it remains the shared verification ledger

**Interfaces:**
- Consumes: current worktree after Tasks 1-3
- Produces: fresh branch verification evidence and updated progress tracking

- [ ] **Step 1: Run the focused changed-area suites**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_app.py tests/test_phase2.py tests/test_rewrite.py tests/test_candidate.py tests/test_config.py tests/test_evals.py -v`

Expected: PASS

- [ ] **Step 2: Run the full suite**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest --import-mode=importlib -v`

Expected: PASS with a fresh total pass count and only the existing Starlette/httpx warning.

- [ ] **Step 3: Update the progress ledger**

```markdown
- Record this broad-final-review fix wave completion in `.superpowers/sdd/progress.md`.
```

- [ ] **Step 4: Run one more broad final review**

Run one final reviewer against the current phase2 implementation scope with findings-only output.

Expected: no remaining broad final-review blockers in scope.

## Self-Review

- Spec coverage:
  - Ordinary-path error normalization and inbound validation compatibility are covered in Task 1.
  - Exact-output-only phase2 gating, coding-flow exclusion, and baseline validation are covered in Task 2.
  - Candidate reasoning-artifact validation, premise-binding isolation, and config-range consistency are covered in Task 3.
  - Fresh verification and final review closure are covered in Task 4.
- Placeholder scan:
  - No `TODO`, `TBD`, or unresolved placeholders remain.
- Type consistency:
  - The plan keeps touched interfaces local to `create_app`, `_parse_upstream_json`, `is_phase2_eligible`, `run_phase2`, `apply_rewrites`, `_require_string_list`, and `validate_phase2_config`.
