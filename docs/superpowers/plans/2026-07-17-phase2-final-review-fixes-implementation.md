# Phase 2 Final Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the remaining final-branch blockers around phase-2 eligibility strictness, summary-conflict detection, strict candidate schema validation, malformed-success fallback status normalization, and total cost-budget interpretation.

**Architecture:** Keep the existing phase-2 design and tighten the boundary checks instead of changing the overall orchestration model. The fixes are limited to the existing eligibility/classification seam, candidate/adjudication parsing, fallback normalization, and their regression tests.

**Tech Stack:** Python 3.12, FastAPI, httpx, pytest

## Global Constraints

- Accept Anthropic-compatible `/v1/messages` traffic and preserve Claude Code / Codex agent behavior.
- Return an Anthropic-compatible success shape or Anthropic-compatible error envelope whenever no downstream bytes have been emitted yet.
- Once downstream streaming has begun, never replace a partial stream with a brand-new full error envelope.
- The minimal phase-2 path does not support inbound Anthropic SSE passthrough or partial synthetic SSE emission.
- Phase 2 remains non-streamed only.
- The retained phase-1 baseline must remain the raw fallback source rather than being coerced into candidate JSON.
- Phase 2 must not trigger for ordinary coding flows or assistant-history traffic.
- Candidate and adjudication parsing must honor the strict JSON schema rather than coercing invalid values into strings.
- `phase2.cost_budget_multiplier` must bound the retained baseline plus all candidate/adjudication output budgets.

---

### Task 1: Tighten Phase 2 Eligibility And Cost Semantics

**Files:**
- Modify: `proxy/phase2.py`
- Modify: `tests/test_phase2.py`
- Modify: `tests/test_app.py`

**Interfaces:**
- Consumes: `is_phase2_eligible(body: dict[str, Any], classification: ClassificationResult, config: ProxyConfig) -> bool`
- Produces: stricter phase-2 entry rules and total-budget-aware cost gating

- [ ] **Step 1: Write the failing eligibility tests**

```python
def test_is_phase2_eligible_rejects_multi_turn_history() -> None:
    ...


def test_is_phase2_eligible_rejects_assistant_history_before_latest_user() -> None:
    ...


def test_is_phase2_eligible_counts_baseline_in_total_cost_budget() -> None:
    ...
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2.py tests/test_app.py -k "eligible and (multi_turn or assistant_history or total_cost_budget)" -v`

Expected: FAIL because current eligibility accepts assistant-history traffic and excludes baseline output budget from the multiplier check.

- [ ] **Step 3: Implement the stricter gate**

```python
def is_phase2_eligible(...):
    return (
        config.phase2.enabled
        and classification.route in config.phase2.trigger_on_routes
        and classification.replay_safe
        and not bool(body.get("stream"))
        and _is_single_turn_user_request(body)
        and _within_phase2_cost_budget(body, config)
    )


def _within_phase2_cost_budget(...):
    baseline_budget = int(body.get("max_tokens", 0))
    phase2_budget = baseline_budget + (
        config.phase2.sample_count * config.phase2.max_candidate_output_tokens
        + config.phase2.max_adjudication_calls * config.phase2.max_candidate_output_tokens
    )
```

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2.py tests/test_app.py -k "eligible and (multi_turn or assistant_history or total_cost_budget)" -v`

Expected: PASS

### Task 2: Enforce Strict Schema Types And Broaden Conflict Detection

**Files:**
- Modify: `proxy/candidate.py`
- Modify: `proxy/phase2.py`
- Modify: `tests/test_candidate.py`
- Modify: `tests/test_phase2.py`

**Interfaces:**
- Consumes: `parse_candidate_response(...)`, `parse_adjudication_response(...)`, `_summaries_compatible(...)`
- Produces: typed schema validation and broader explicit-negation conflict detection

- [ ] **Step 1: Write the failing schema/conflict tests**

```python
def test_parse_candidate_response_rejects_null_final_answer() -> None:
    ...


def test_parse_candidate_response_rejects_unknown_confidence_value() -> None:
    ...


def test_parse_adjudication_response_rejects_non_string_fields() -> None:
    ...


def test_aggregate_candidates_runs_adjudication_when_distinguishable_and_cannot_be_distinguished_conflict() -> None:
    ...
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_candidate.py tests/test_phase2.py -k "null_final_answer or confidence or adjudication_response or distinguished_conflict" -v`

Expected: FAIL because current parsers coerce invalid values with `str(...)` and conflict detection is too literal.

- [ ] **Step 3: Implement the stricter validation**

```python
def _require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _require_choice(value: Any, field_name: str, allowed: set[str]) -> str:
    ...


def _normalize_summary_items(items: list[str]) -> list[str]:
    normalized = " ".join(item.casefold().split())
    normalized = normalized.replace("cannot be", "can not be")
    normalized = normalized.replace("is not", "is")
```

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_candidate.py tests/test_phase2.py -k "null_final_answer or confidence or adjudication_response or distinguished_conflict" -v`

Expected: PASS

### Task 3: Normalize Malformed 2xx Fallbacks And Re-Verify The Branch

**Files:**
- Modify: `proxy/app.py`
- Modify: `tests/test_app.py`
- Modify: `.superpowers/sdd/task-6-report.md`

**Interfaces:**
- Consumes: `_parse_upstream_json(upstream: UpstreamResult, normalize_error: bool = False) -> dict[str, Any]`
- Produces: non-2xx status normalization for malformed 2xx fallback bodies and refreshed branch verification evidence

- [ ] **Step 1: Write the failing malformed-200 fallback tests**

```python
def test_messages_phase2_malformed_200_fallback_becomes_502_error_envelope() -> None:
    ...
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_app.py -k "malformed_200_fallback" -v`

Expected: FAIL because current fallback returns `200` with an Anthropic error envelope.

- [ ] **Step 3: Implement the status normalization**

```python
def _parse_upstream_json(... ) -> tuple[int, dict[str, Any]]:
    ...
    if upstream.status_code < 400 and parsed_is_invalid:
        return 502, anthropic_error(502, "upstream returned an invalid success payload")
```

- [ ] **Step 4: Run the focused app tests**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_app.py -k "malformed_200_fallback or baseline_error" -v`

Expected: PASS

- [ ] **Step 5: Run the full regression suite**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest --import-mode=importlib -v`

Expected: PASS with a fresh total pass count and only the existing Starlette/httpx warning.

- [ ] **Step 6: Update shared verification evidence**

```markdown
- Append the final blocker-fix verification run to `.superpowers/sdd/task-6-report.md`.
```

## Self-Review

- Spec coverage:
  - Eligibility strictness and total budget semantics are covered in Task 1.
  - Strict schema typing and broader summary-conflict detection are covered in Task 2.
  - Malformed-success fallback normalization and fresh full verification are covered in Task 3.
- Placeholder scan:
  - No `TODO`, `TBD`, or unresolved task references remain.
- Type consistency:
  - The plan keeps `is_phase2_eligible`, `parse_candidate_response`, `parse_adjudication_response`, `_parse_upstream_json`, and `run_phase2` as the touched interfaces.
