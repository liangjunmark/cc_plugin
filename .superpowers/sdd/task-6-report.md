# Task 6 Report

## Status

DONE

## Files Changed

- `tests/test_phase2.py`
- `proxy/phase2.py`
- `README.md`
- `.superpowers/sdd/task-6-report.md`

## Implementation Summary

- Added Phase 2 coverage for conflicting candidate summaries, a non-canonical parse-failure majority, and answer-only selected output.
- Added the missing non-canonical-majority aggregation branch, preserving the existing stream guard, raw baseline fallback, selectors, and routing behavior.
- Documented the real-provider validation workflow and required measurements.

## Test Commands And Results

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2.py -v` (RED): 1 failed, 10 passed; the failure exposed the missing `non_canonical_majority` branch.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2.py tests/test_app.py tests/test_evals.py -v`: 38 passed, 1 Starlette/httpx deprecation warning.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest --import-mode=importlib -v`: 103 passed, 1 Starlette/httpx deprecation warning.

## Concerns

- Real-provider validation was documented but not executed because it requires configured provider credentials and a live proxy target.

## Review Fixes

- Fixed `run_phase2(...)` to execute one bounded adjudication request when aggregation returns `run_adjudication` and the remaining call budget permits it. The request includes the parsed candidate summaries and requires JSON-only `final_answer`, `answer_type`, and `decision_summary`; only `final_answer` is emitted downstream.
- Invalid, non-JSON, non-canonical, or failed adjudication responses now return the retained phase-1 baseline result. The adjudication call runs inside the phase-2 total timeout and shares the four-call upstream budget.
- Corrected the exact-output regression to create a real two-candidate disagreement and verify the fourth adjudication request plus answer-only downstream content. Added invalid-adjudication fallback coverage.
- Renamed the three-candidate non-canonical case as generic aggregation-helper coverage and documented that production defaults to two candidates.

### Commands And Results

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2.py -v` (RED): 2 failed, 10 passed. Both new regressions observed only 3 calls, proving that `run_phase2(...)` did not execute adjudication.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2.py -v` (GREEN): 12 passed.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2.py tests/test_app.py tests/test_evals.py -v`: 39 passed, 1 existing Starlette/httpx deprecation warning.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest --import-mode=importlib -v`: 104 passed, 1 existing Starlette/httpx deprecation warning.

## Final Local Correction

- Wrapped adjudication transport exceptions so they degrade to retained-baseline fallback instead of escaping `run_phase2(...)`.
- Replaced the impossible three-candidate runtime override with a real two-candidate mixed parse-failure execution test, while keeping the helper-level non-canonical-majority unit coverage explicit.
- Verification:
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2.py -v`: 14 passed.
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest --import-mode=importlib -v`: 106 passed, 1 existing Starlette/httpx deprecation warning.

## Final Fix Wave

- Wrapped the adjudication execution boundary so transport and other non-timeout adjudication exceptions return the retained phase-1 baseline. Timeouts continue through the existing timeout fallback path.
- Added a candidate execution/parsing regression with two non-canonical numeric candidate responses and one canonical response, verifying that the real non-canonical-majority branch returns the retained baseline under the four-call budget.
- Added an adjudication transport-exception regression that raises `httpx.ConnectError` after the baseline and candidate disagreement exist, verifying no exception escapes `run_phase2(...)` and no selected downstream payload is emitted.

### Commands And Results

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2.py -v` (RED): 1 failed, 13 passed. The new adjudication transport-exception regression reproduced `httpx.ConnectError` escaping `run_phase2(...)`; the parse-failure-mix regression passed because its fallback behavior was already implemented.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2.py -v` (GREEN): 14 passed.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest --import-mode=importlib -v`: 106 passed, 1 existing Starlette/httpx deprecation warning.

## Final Adjudication Retry Fix

- Forced the single adjudication request to use `replay_safe=False`, preventing retryable status and transport failures from issuing another adjudication upstream attempt.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2.py -v` (RED): 1 failed, 14 passed; the new retryable-503 regression observed 2 adjudication attempts.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_phase2.py -v` (GREEN): 15 passed; the same regression observed exactly 1 attempt and one budget unit remaining.

## Final Full Regression Evidence

- Re-ran the full suite on the current worktree after the `replay_safe=False` adjudication fix.

### Commands And Results

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest --import-mode=importlib -v`: 107 passed, 1 existing Starlette/httpx deprecation warning.

## Task 3 Protocol Scoring Verification

- `score_run(...)` now evaluates protocol compatibility from the HTTP status code and decoded response payload, independently of correctness and format.
- Plaintext 503 responses and non-Anthropic JSON error bodies are protocol failures. A 2xx response must be an Anthropic message envelope with a content list; a non-2xx response must be an Anthropic error envelope with typed error details.
- TDD evidence: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py -k "protocol" -v` was red with 2 expected failures before implementation; `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_evals.py -v` was green with 15 passed.
- Fresh full verification: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest --import-mode=importlib -v` passed with 117 tests and 1 existing Starlette/httpx deprecation warning.

## Task 3 Malformed 2xx Fallback Verification

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_app.py -k "malformed_200_fallback or baseline_error" -v`: 2 passed, 1 existing Starlette/httpx deprecation warning.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest --import-mode=importlib -v`: 127 passed, 1 existing Starlette/httpx deprecation warning.
