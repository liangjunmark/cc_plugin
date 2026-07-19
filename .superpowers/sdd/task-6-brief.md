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
